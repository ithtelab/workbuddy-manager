"""加账号弹窗：登录完成必须尽快反映到界面，**两种**节流场景都要过。

场景一（issue #34，标签页切走）：/auth/poll 被浏览器把定时器压到约 1 次/分钟，
用户切回来要等最久一整分钟。修法是 visibilitychange 时立即补一次。

场景二（用户反馈 2026-09-26，窗口**被遮挡**）：Chrome 对**被遮挡**（不是「隐藏」）
的窗口同样节流定时器 —— 实测 2 秒的 setInterval 在 64 秒里只跑了 2 次 —— 但
`document.hidden` 仍是 false、visibilitychange 一次都不触发，场景一那条兜底
完全失效。修法有两半：服务端自己盯着这张码（前端问不问都不影响检测），
前端再挂「人一操作就立即补一次」（focus / pointerdown / keydown）。

这个脚本在真浏览器里把两条路径都走一遍：

    python dev/verify_login_poll_visibility.py --check

场景二的做法：把页面里 2000ms 的 setInterval 换成不触发的（等价于被节流到
一小时一次的极端），并统计前端真的打了几次 /api/auth/poll。授权完成由模拟
上游翻牌，先证明「没人操作时界面不动」（否则说明模拟没生效），再点一下鼠标，
断言 2 秒内界面就更新 —— 被遮挡窗口里用户回来的第一个动作必然是这类交互。

数据落在 dev/.login-poll/（已 gitignore），截图输出到 dev/.login-shots/。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.login-poll'
SHOTS = REPO / 'dev' / '.login-shots'
UPSTREAM_PORT = 7903
MANAGER_PORT = 7904
ADMIN_PW = 'login-poll-pass'

# 授权完成由脚本通过 /control/ready 翻牌（不再靠「第 N 次轮询」猜时序：
# 现在还有服务端自己的轮询在打同一个接口，次数不再是前端行为的证据）
_login_done = {'v': False}


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('content-type', 'application/json; charset=utf-8')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/control/ready':
            _login_done['v'] = True
            self._json({'ok': True})
        elif path == '/control/reset':
            _login_done['v'] = False
            self._json({'ok': True})
        elif path == '/control/state':
            # 管理端进程里的假 poll_login 通过它读「授权完成了没」——
            # 模拟上游与面板是两个进程，没法共享内存
            self._json({'ready': _login_done['v']})
        elif path == '/healthz':
            self._json({'healthy': 1, 'total': 1})
        elif path == '/status':
            self._json({'accounts': [], 'total': 0, 'healthy': 0, 'cooling': 0,
                        'disabled': 0, 'in_flight_full': 0, 'redis_mode': 'noop',
                        'sticky_sessions': 0,
                        'realm_totals': {
                            'cn': {'total': 0, 'healthy': 0, 'cooling': 0,
                                   'disabled': 0, 'in_flight_full': 0},
                            'global': {'total': 0, 'healthy': 0, 'cooling': 0,
                                       'disabled': 0, 'in_flight_full': 0}}})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        self._json({'ok': True})


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)
    (DATA / 'auths').mkdir(parents=True, exist_ok=True)

    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'PYTHONUTF8': '1',
    }

    # 启动管理端；同时在**管理端进程里**打补丁：让 tencent.start_login /
    # poll_login 返回可控结果（真实腾讯无法在测试里走完授权）。
    launcher = f'''
import server.config as c
from server.services import tencent

async def fake_start(realm='cn'):
    return {{'state': 'probe-state-1',
             'authUrl': 'https://example.invalid/authorize?state=probe-state-1',
             'realm': realm}}

async def fake_poll(state, realm=None):
    # 授权完成与否由控制端点翻牌 —— 现在服务端也在打这个接口，
    # 「第几次调用」不再等于「前端问了几次」。
    if _done():
        return {{'status': 'ready', 'uid': 'u-probe', 'nickname': '探针账号',
                 'enterprise_id': '', 'access_token': 'tok', 'refresh_token': 'rt',
                 'expires_at': 9999999999, 'domain': 'copilot.tencent.com', 'realm': 'cn'}}
    return {{'status': 'waiting'}}

def _done():
    import json, urllib.request
    try:
        with urllib.request.urlopen('http://127.0.0.1:{UPSTREAM_PORT}/control/state',
                                    timeout=1) as r:
            return bool(json.loads(r.read()).get('ready'))
    except Exception:
        return False

tencent.start_login = fake_start
tencent.poll_login = fake_poll
import server.routers.accounts as acc
acc.tencent.start_login = fake_start
acc.tencent.poll_login = fake_poll

import uvicorn, server.main
uvicorn.run(server.main.app, host="127.0.0.1", port={MANAGER_PORT}, log_level="warning")
'''
    proc = subprocess.Popen([sys.executable, '-c', launcher], cwd=str(REPO), env=env)
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}  (admin / {ADMIN_PW})')
    try:
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{MANAGER_PORT}/api/healthz', timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        return run_check(env)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.shutdown()


def run_check(env: dict) -> int:
    code = r'''
const BASE = process.env.BASE, USER = 'admin', PASS = process.env.PASS, OUT = process.env.OUT;
const CTRL = process.env.CTRL;
(async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const {pathToFileURL} = await import('node:url');
  const c = path.join(process.env.TEMP || '/tmp', 'wb-i18n-verify', 'node_modules', 'playwright-core', 'index.js');
  const mod = await import(fs.existsSync(c) ? pathToFileURL(c).href : 'playwright-core');
  const chromium = mod.chromium ?? mod.default?.chromium;
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  const dir = fs.existsSync(root) ? fs.readdirSync(root).filter(d => d.startsWith('chromium-') && !d.includes('headless_shell')).sort().pop() : null;
  const exe = dir ? path.join(root, dir, 'chrome-win64', 'chrome.exe') : undefined;
  const browser = await chromium.launch({executablePath: exe});
  const ctx = await browser.newContext({viewport: {width: 1400, height: 950}, deviceScaleFactor: 2});
  const findings = [];
  const step = (ok, label, detail = '') => {
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ` -- ${detail}` : ''}`);
    if (!ok) findings.push(label + (detail ? ': ' + detail : ''));
  };
  const ctl = (what) => fetch(`${CTRL}/control/${what}`).then(r => r.text());
  // 断言**只看弹窗**：账号一旦在服务端落盘，身后的列表很快就会出现这个账号名，
  // 用整页文本判断会把「列表里有名字」当成「弹窗更新了」（第一版就栽在这上面）。
  const dlgText = (page) => page.locator('[role=dialog]').first().innerText();
  const waiting = async (page) => /等待/.test(await dlgText(page));
  const done = async (page) => /授权成功|探针账号/.test(await dlgText(page));

  // ══ 场景一：标签页切走再切回（issue #34） ══════════════════════════
  console.log('\n场景一：切走标签页 → 切回，界面要立即更新');
  await ctl('reset');
  const page = await ctx.newPage();
  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.fill('#username', USER);
  await page.fill('#password', PASS);
  await Promise.all([page.waitForURL(/dashboard/).catch(() => {}), page.click('button[type=submit]')]);

  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  await page.waitForTimeout(2000);
  await page.click('text=添加账号');
  await page.waitForTimeout(2500);
  step(await waiting(page), '弹窗进入等待状态（轮询已启动）');

  // 用 CDP 让页面进入 hidden（等价于用户切到另一个标签页）：
  // 直接派发 visibilitychange 并覆写 document.hidden，因为无头浏览器不会
  // 因为"切标签"真的改变它。
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
    Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
    document.dispatchEvent(new Event('visibilitychange'));
  });
  // 在"后台"停留并把授权翻成完成 —— 期间界面不应更新（tick 里判了 hidden）
  await page.waitForTimeout(1500);
  await ctl('ready');
  await page.waitForTimeout(4000);
  step(await waiting(page), '隐藏期间界面保持不动（不该偷偷刷）');

  // ── 用户登录完成，切回页面 ──
  const t0 = Date.now();
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
    Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});
    document.dispatchEvent(new Event('visibilitychange'));
  });
  let updated = false;
  for (let i = 0; i < 50; i++) {
    if (await done(page)) { updated = true; break; }
    await page.waitForTimeout(100);
  }
  const elapsed = Date.now() - t0;
  step(updated, `切回后界面立即更新（${elapsed}ms，上限 5000ms）`,
       updated ? '' : '界面仍未更新 —— 说明 visibilitychange 没生效');
  step(elapsed < 5000, `更新耗时远小于被节流后的约 60s（${elapsed}ms）`);
  await page.screenshot({path: `${OUT}/login-done.png`, fullPage: true});
  await page.close();

  // ══ 场景二：窗口被遮挡（定时器被节流，且没有任何可见性事件）═══════
  console.log('\n场景二：窗口被遮挡（2s 定时器完全不触发）→ 点一下，界面要立即更新');
  await ctl('reset');
  const page2 = await ctx.newPage();
  // 关键模拟：让 2000ms 的 setInterval 一次都不跑（等价于被节流到一小时一次），
  // 并统计前端真的打了几次 /api/auth/poll（axios 走 XHR，两条都挂上）。
  await page2.addInitScript(() => {
    window.__pollCount = 0;
    const bump = (u) => { if (String(u).includes('/api/auth/poll')) window.__pollCount++; };
    const proto = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
    if (proto && proto.open) {
      const open = proto.open;
      proto.open = function (m, u, ...rest) { bump(u); return open.call(this, m, u, ...rest); };
    }
    const of = window.fetch;
    if (of) window.fetch = function (input, ...rest) {
      bump(typeof input === 'string' ? input : (input && input.url)); return of.call(this, input, ...rest);
    };
    const set = window.setInterval;
    window.setInterval = function (fn, ms, ...rest) {
      if (ms === 2000) return 0;            // 被遮挡窗口：这个定时器几乎不触发
      return set(fn, ms, ...rest);
    };
  });
  await page2.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  await page2.waitForTimeout(2000);
  await page2.click('text=添加账号');
  await page2.waitForTimeout(1500);
  // 遮挡 ≠ 隐藏：document.hidden 仍是 false，visibilitychange 一次都不触发
  await page2.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
    Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});
  });
  step(await waiting(page2), '弹窗进入等待状态');
  const polls0 = await page2.evaluate(() => window.__pollCount);
  step(polls0 === 0, `定时器确实死了：前端一次都没问过（${polls0} 次）`,
       polls0 === 0 ? '' : '模拟没生效，本场景的结论不可信');

  // 授权在服务端侧完成 —— 检测不该依赖前端
  await ctl('ready');
  await page2.waitForTimeout(8000);
  const polls1 = await page2.evaluate(() => window.__pollCount);
  step(await waiting(page2), '没人操作时界面不动（8s 里前端没有轮询，也没有可见性事件）');
  step(polls1 === 0, `这段时间前端依然没问过（${polls1} 次）`);

  // ── 用户回来了：第一个动作必然是点一下/敲一下/窗口获得焦点 ──
  const t1 = Date.now();
  const dlg = await page2.locator('[role=dialog]').first().boundingBox();
  await page2.mouse.click(dlg.x + dlg.width / 2, dlg.y + 24);   // 标题行，无副作用
  let updated2 = false;
  for (let i = 0; i < 25; i++) {
    if (await done(page2)) { updated2 = true; break; }
    await page2.waitForTimeout(100);
  }
  const elapsed2 = Date.now() - t1;
  const polls2 = await page2.evaluate(() => window.__pollCount);
  step(polls2 > 0, `点击后前端立即补了一次轮询（${polls2} 次）`);
  step(updated2, `点击后界面立即更新（${elapsed2}ms，上限 2500ms）`,
       updated2 ? '' : '界面仍停在等待 —— 唤醒事件（focus/pointerdown/keydown）没生效');
  await page2.screenshot({path: `${OUT}/login-occluded.png`, fullPage: true});

  await browser.close();
  if (findings.length) { console.log('\nFAILURES:\n' + findings.join('\n')); process.exit(1); }
  console.log('\nALL CHECKS PASSED');
})().catch(e => { console.error('harness error:', e); process.exit(2); });
'''
    SHOTS.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', '-e', code],
                       env={**env, 'BASE': f'http://127.0.0.1:{MANAGER_PORT}',
                            'PASS': ADMIN_PW, 'OUT': str(SHOTS),
                            'CTRL': f'http://127.0.0.1:{UPSTREAM_PORT}'},
                       cwd=str(REPO / 'web'), text=True, timeout=600)
    print(r.stdout or '')
    if r.stderr:
        print(r.stderr[:2000])
    print(f'截图: {SHOTS}')
    return r.returncode


if __name__ == '__main__':
    raise SystemExit(main())

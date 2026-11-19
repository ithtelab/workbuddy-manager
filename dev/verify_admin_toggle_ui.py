"""设置页「账号管理接口」开关的界面验收（issue #45 追加反馈）。

用户反馈原话：「我更新了最新版本 v1.0.58，点击临时停用，显示（回退提示）」。
根因不是 bug 而是**没有开关**——上游那组管理接口默认不注册，用户只能手改
config.json；于是「临时停用」永远走回退路径，issue #45 的诉求实际没被满足。

这个脚本从浏览器侧确认：
  1. 设置页**存在**这个开关，且默认关闭（上游默认值）；
  2. 文案写清了「开与不开分别会怎样」——用户在账号页看到的提示会随它变化；
  3. 打开并保存后，配置真的落到上游 config.json；
  4. 账号页的回退提示里带了**可操作的下一步**（去哪儿开）。

    python dev/verify_admin_toggle_ui.py

数据落在 dev/.admin-toggle/（已 gitignore），截图输出到 dev/.shots-admin/。
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
DATA = REPO / 'dev' / '.admin-toggle'
SHOTS = REPO / 'dev' / '.shots-admin'
UPSTREAM_PORT = 7905
MANAGER_PORT = 7906
ADMIN_PW = 'admin-toggle-pass'

UID = 'dddd4444-0000-0000-0000-000000000004'


class Upstream(BaseHTTPRequestHandler):
    """假上游：**故意不注册** /admin/*，模拟用户的实际处境。"""
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
        if path == '/healthz':
            self._json({'healthy': 1, 'total': 1})
        elif path == '/status':
            self._json({
                'accounts': [{'uid': UID, 'nickname': '演示号', 'credits': 500,
                              'disabled': False, 'cooling': False,
                              'success_count': 3, 'in_flight': 0}],
                'total': 1, 'healthy': 1, 'cooling': 0, 'disabled': 0,
                'in_flight_full': 0, 'redis_mode': 'noop', 'sticky_sessions': 0,
                'realm_totals': {
                    'cn': {'total': 1, 'healthy': 1, 'cooling': 0,
                           'disabled': 0, 'in_flight_full': 0},
                    'global': {'total': 0, 'healthy': 0, 'cooling': 0,
                               'disabled': 0, 'in_flight_full': 0}},
            })
        elif path == '/v1/models':
            self._json({'object': 'list', 'data': [{'id': 'glm-5.2'}]})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        # 关键：管理接口**不存在** → 纯文本 404（net/http 兜底形态）
        if '/admin/accounts/' in self.path:
            body = b'404 page not found\n'
            self.send_response(404)
            self.send_header('content-type', 'text/plain; charset=utf-8')
            self.send_header('content-length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({'ok': True})


def _jwt(uid: str) -> str:
    import base64
    h = base64.urlsafe_b64encode(
        json.dumps({'alg': 'none', 'typ': 'JWT'}).encode()).decode().rstrip('=')
    p = base64.urlsafe_b64encode(json.dumps({
        'iat': int(time.time()), 'exp': int(time.time()) + 60 * 86400,
        'uid': uid}).encode()).decode().rstrip('=')
    return f'{h}.{p}.sig'


def seed() -> None:
    sys.path.insert(0, str(REPO))
    from server import config, db
    config.DB_PATH = DATA / 'manager.db'
    db._conn = None
    db.connect()
    auths = DATA / 'auths'
    auths.mkdir(parents=True, exist_ok=True)
    (auths / f'workbuddy-{UID}.json').write_text(json.dumps({
        'account': {'uid': UID, 'enterpriseId': 'e', 'nickname': '演示号'},
        'auth': {'accessToken': _jwt(UID), 'refreshToken': 'r',
                 'expiresAt': int(time.time()) + 60 * 86400,
                 'domain': 'copilot.tencent.com', 'realm': 'cn'},
    }, ensure_ascii=False), encoding='utf-8')
    db._conn.close()
    db._conn = None

    # 上游 config.json：admin.enabled 默认 false（与上游默认一致）
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / 'config.json').write_text(json.dumps({
        'listen': '127.0.0.1:7863', 'api_key': 'upstream-key-for-test',
        'admin': {'enabled': False},
    }, ensure_ascii=False, indent=2), encoding='utf-8')


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)

    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB2API_KEY': 'upstream-key-for-test',
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_UPSTREAM_CONFIG': str(DATA / 'config.json'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'PYTHONUTF8': '1',
    }
    seed()

    launcher = ('import uvicorn, server.main;'
                f'uvicorn.run(server.main.app, host="127.0.0.1", '
                f'port={MANAGER_PORT}, log_level="warning")')
    proc = subprocess.Popen([sys.executable, '-c', launcher], cwd=str(REPO), env=env)
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}  (admin / {ADMIN_PW})')
    try:
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(
                    f'http://127.0.0.1:{MANAGER_PORT}/api/healthz', timeout=1)
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
    SHOTS.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', '-e', JS],
                       env={**env, 'BASE': f'http://127.0.0.1:{MANAGER_PORT}',
                            'PASS': ADMIN_PW, 'OUT': str(SHOTS),
                            'CFG': str(DATA / 'config.json')},
                       cwd=str(REPO / 'web'), text=True, timeout=300)
    print(r.stdout or '')
    if r.stderr:
        print(r.stderr[:2000])
    print(f'截图: {SHOTS}')
    return r.returncode


JS = r'''
const BASE = process.env.BASE, OUT = process.env.OUT, CFG = process.env.CFG;
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
  const ctx = await browser.newContext({viewport: {width: 1500, height: 1100}, deviceScaleFactor: 2});
  const page = await ctx.newPage();
  const findings = [];
  const step = (ok, label, extra = '') => {
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? ` -- ${extra}` : ''}`);
    if (!ok) findings.push(label + (extra ? ': ' + extra : ''));
  };

  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.fill('#username', 'admin');
  await page.fill('#password', process.env.PASS);
  await Promise.all([page.waitForURL(/dashboard/).catch(() => {}), page.click('button[type=submit]')]);

  // ── 1. 设置页有「账号管理接口」分组 ──
  await page.goto(`${BASE}/settings`, {waitUntil: 'load'});
  await page.waitForTimeout(4000);
  let body = await page.locator('body').innerText();
  step(/账号管理接口/.test(body), '设置页出现「账号管理接口」分组');
  step(/启用账号管理接口/.test(body), '开关本身在页面上');

  // 文案要讲清两条路的差别（用户在账号页看到的提示会随它变化）
  const grp = page.locator('text=账户管理接口, text=账号管理接口').first();
  step(/签到|保活/.test(body), '文案说明了开启后签到/保活照常');
  step(/改文件名|完全退出账号池/.test(body), '文案说明了不开启时的代价');
  await page.screenshot({path: `${OUT}/settings-admin.png`, fullPage: true});

  // ── 1b. 多语言：切到其它语言后，这个分组必须显示**译文**而不是中文原文 ──
  // 短语表是按「渲染后的整串」查的，拼接少一个字符就静默退回中文原文
  // （界面表现为漏翻）。这里在真实浏览器里确认译文真的生效。
  const LOCALE_KEY = 'workbuddy-manager:locale';
  for (const [code, expect, han] of [['en', /account management api/i, null],
                                     ['ja', /アカウント管理/, null]]) {
    await page.evaluate(([k, v]) => localStorage.setItem(k, v), [LOCALE_KEY, code]);
    await page.reload({waitUntil: 'load'});
    await page.waitForTimeout(3500);
    const txt = await page.locator('body').innerText();
    step(expect.test(txt), `切到 ${code} 后分组标题已翻译`, (txt.match(/account management api|アカウント管理/g) || [''])[0]);
  }
  // 切回中文，避免影响后续断言
  await page.evaluate(([k, v]) => localStorage.setItem(k, v), [LOCALE_KEY, 'zh-CN']);
  await page.reload({waitUntil: 'load'});
  await page.waitForTimeout(3500);
  body = await page.locator('body').innerText();

  // ── 2. 打开并保存，确认落到上游 config.json ──
  const before = JSON.parse(fs.readFileSync(CFG, 'utf8'));
  step(before.admin?.enabled === false, '前置：上游 config 里 admin.enabled 初始为 false');

  // 字段行的结构是「label+desc 的 div」与 Switch 并排，所以先定位到含该文案的
  // 那一行，再取行内唯一的 switch（用 xpath 从文案向上找最近的 flex 行）
  const labelNode = page.locator('div:text-is("启用账号管理接口")').first();
  const target = labelNode.locator('xpath=ancestor::div[contains(@class,"justify-between")][1]//button[@role="switch"]').first();
  const fallback = page.locator('button[role=switch]').last();
  const sw = (await target.count()) ? target : fallback;
  await sw.click();
  await page.waitForTimeout(700);
  // 该分组的保存按钮：从标题元素向上找到卡片容器，再取卡片内的「保存」。
  // （不能用 div:hasText 匹配卡片——卡片里包含整段说明文案，正则锚定 ^ 匹配不到。）
  const titleNode = page.locator('div:text-is("账号管理接口")').first();
  const groupCard = titleNode.locator(
    'xpath=ancestor::div[contains(@class,"rounded-[20px]")][1]').first();
  let saveBtn = groupCard.locator('button', {hasText: /保存/}).first();
  if (!(await saveBtn.count())) {
    saveBtn = page.locator('button', {hasText: /保存/}).last();
  }
  await saveBtn.click();
  await page.waitForTimeout(4000);
  const after = JSON.parse(fs.readFileSync(CFG, 'utf8'));
  step(after.admin?.enabled === true, '开启后配置真的写到了上游 config.json',
       `admin.enabled=${after.admin?.enabled}`);

  // 把开关**关回**去，再验账号页的回退提示 —— 这条提示是随开关状态变的：
  // 开着但上游没提供接口时，界面说的是「已开启（…），但运行中的上游没有提供它，
  // 改完配置需要重启上游容器」（另一条正确但不同的文案，见 issue #45 的追问）。
  // 本脚本第 3 步要验的是**没开这个开关**的那条路径，所以必须先把状态复位。
  // 这里原先只写了注释、没写复位代码，于是自 2026-09-21 起了新文案之后这条一直
  // 是红的（验到的不是它以为的那件事），而不是产品坏了。
  await sw.click();
  await page.waitForTimeout(700);
  await saveBtn.click();
  await page.waitForTimeout(4000);
  const reset = JSON.parse(fs.readFileSync(CFG, 'utf8'));
  step(reset.admin?.enabled === false, '开关已关回（第 3 步的前提）',
       `admin.enabled=${reset.admin?.enabled}`);

  // ── 3. 账号页回退提示要带可操作的下一步 ──
  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  await page.waitForTimeout(4000);
  // 桌面表格与移动卡片**都在 DOM 里**（靠 md:hidden / hidden md:block 切换），
  // 所以不能取 .first()——那会命中当前视口下不可见的那个，点击会一直等可见。
  // 用 Playwright 的 :visible 过滤，只点真正显示的那个。
  const pauseBtn = page.locator('button[title*="停用"]:visible').first();
  if (await pauseBtn.count()) {
    await pauseBtn.click();
    await page.waitForTimeout(3500);
    body = await page.locator('body').innerText();
    const m = body.match(/[^\n]*未启用管理接口[^\n]*/);
    step(!!m, '停用时给出了回退提示');
    if (m) {
      step(/设置/.test(m[0]), '提示里指明了去哪儿开启', m[0].slice(0, 90));
      step(/重启/.test(m[0]) || /设置/.test(m[0]), '提示里给了可操作的下一步');
    }
  } else {
    step(false, '找不到停用按钮');
  }
  await page.screenshot({path: `${OUT}/accounts-fallback.png`, fullPage: true});

  await browser.close();
  if (findings.length) { console.log('\nFAILURES:\n' + findings.join('\n')); process.exit(1); }
  console.log('\nALL CHECKS PASSED');
})().catch(e => { console.error('harness error:', e); process.exit(2); });
'''


if __name__ == '__main__':
    raise SystemExit(main())

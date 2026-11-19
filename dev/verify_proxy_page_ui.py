"""「设置 → 代理」页面的浏览器验收（真渲染 + 真落盘）。

为什么值得单独跑：这一页的价值全在交互上 —— 列表要脱敏显示、新增要走弹窗并写进
上游 config.json、绑定数要能看出「删了会影响到谁」、全局代理保存后要立刻显示
「当前由面板设置生效」。这些都要真渲染出来才看得见。

    python dev/verify_proxy_page_ui.py

数据落在 dev/.proxy-ui/（已 gitignore），截图输出到 dev/.shots-proxy/。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.proxy-ui'
SHOTS = REPO / 'dev' / '.shots-proxy'
UPSTREAM_PORT = 7941
PANEL_PORT = 7942
ADMIN_PW = 'proxy-ui-pass'


class Upstream(BaseHTTPRequestHandler):
    """最小 mock 上游：这一页不依赖它，但面板启动后会探测，给个 200 免得刷警告。"""

    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/healthz'):
            self._json({'healthy': 1})
        elif self.path.startswith('/v1/models'):
            self._json({'object': 'list', 'data': [{'id': 'glm-5.2'}]})
        else:
            self._json({})


def seed() -> None:
    sys.path.insert(0, str(REPO))
    from server import config, db

    config.DB_PATH = DATA / 'manager.db'
    db._conn = None
    db.connect()
    db._conn.close()
    db._conn = None


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    (DATA / 'auths').mkdir(parents=True)
    # 线路表：一条绑着账号（看「绑定 N 台」与删除提示会影响到谁），其余没绑
    (DATA / 'config.json').write_text(json.dumps({
        'api_key': 'wbk_keep-me',
        'proxies': {
            'px-dead': 'socks5://user:supersecret@127.0.0.1:1',
            'px-free': 'http://user2:pw2@proxy.example:8080',
            # 住宅代理的真实形态：国家与会话写在用户名里，换会话名就是换出口 IP
            'px-shanghai': 'socks5://demo_c_CN_city_Shanghai_s_wb01:pw@gateway.example.dev:10000',
            # 对象形态（带权重、已停用）：老键名是纯字符串，两种都要能读
            'px-tokyo': {'url': 'socks5://demo_c_JP_city_Tokyo_s_wb02:pw@gateway.example.dev:10000',
                         'weight': 30, 'enabled': False},
        },
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    # 一个已绑定、一个未绑定 —— 自动分配要有人可分，而绑定数要看得见
    (DATA / 'auths' / 'workbuddy-demo.json').write_text(json.dumps({
        'account': {'uid': 'demo-uid', 'nickname': '演示号'},
        'auth': {'accessToken': 'x'},
        'proxy': 'px-dead',
    }, ensure_ascii=False), encoding='utf-8')
    (DATA / 'auths' / 'workbuddy-demo2.json').write_text(json.dumps({
        'account': {'uid': 'demo2-uid', 'nickname': '备用号'},
        'auth': {'accessToken': 'y'},
    }, ensure_ascii=False), encoding='utf-8')
    seed()

    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_UPSTREAM_CONFIG': str(DATA / 'config.json'),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_DATA_DIR': str(DATA),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(PANEL_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'WB_SECURE_COOKIE': 'false',
        'PYTHONUTF8': '1',
        'PYTHONPATH': os.environ.get('PYTHONPATH', ''),
    }
    launcher = ('import uvicorn, server.main;'
                f'uvicorn.run(server.main.app, host="127.0.0.1", '
                f'port={PANEL_PORT}, log_level="warning")')
    proc = subprocess.Popen([sys.executable, '-c', launcher], cwd=str(REPO), env=env)
    print(f'管理端: http://127.0.0.1:{PANEL_PORT}')
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{PANEL_PORT}/api/healthz', timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        return run_checks(env)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.shutdown()


def run_checks(env: dict) -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', '-e', JS], env={**env, 'BASE': f'http://127.0.0.1:{PANEL_PORT}',
                                                'PASS': ADMIN_PW, 'OUT': str(SHOTS)},
                       cwd=str(REPO / 'web'), text=True, timeout=420, capture_output=True)
    print(r.stdout or '')
    if r.stderr:
        print(r.stderr[:2000])
    print(f'截图: {SHOTS}')

    findings = []
    if r.returncode != 0:
        findings.append(f'浏览器验收脚本自身失败（退出码 {r.returncode}）')
    if 'ALL CHECKS PASSED' not in (r.stdout or ''):
        findings.append('浏览器验收没有跑到结束（未见 ALL CHECKS PASSED）')
    findings += [ln.strip()[6:] for ln in (r.stdout or '').splitlines()
                 if ln.strip().startswith('FAIL  ')]

    # 落盘核对：浏览器里做的操作必须真的写进配置文件
    cfg = json.loads((DATA / 'config.json').read_text(encoding='utf-8'))
    proxies = cfg.get('proxies') or {}
    if 'px-hk' not in proxies:
        findings.append('浏览器里新增的线路没有写进上游 config.json')
    if proxies.get('px-import') != 'http://u:pw@imp.example:3128':
        findings.append(f'导入的线路没落盘或地址不对：{proxies.get("px-import")}')
    if cfg.get('api_key') != 'wbk_keep-me':
        findings.append('写线路把 config.json 里别的键弄丢了')
    if (proxies.get('px-tokyo') or {}).get('enabled') is not False:
        findings.append('对象形态的线路（权重/停用）没被原样保留')
    account = json.loads((DATA / 'auths' / 'workbuddy-demo.json').read_text(encoding='utf-8'))
    if account.get('proxy') != 'px-shanghai':
        findings.append(f'绑定管理里选的线路没写进账号文件：{account.get("proxy")!r}')
    if 'proxy_pool' in account or 'proxy_session' in account:
        findings.append('上一版的 proxy_pool / proxy_session 没被清掉')
    spare = json.loads((DATA / 'auths' / 'workbuddy-demo2.json').read_text(encoding='utf-8'))
    if spare.get('proxy') != 'px-hk':
        findings.append(f'自动分配的线路没写进账号文件：{spare.get("proxy")!r}')

    import sqlite3
    conn = sqlite3.connect(DATA / 'manager.db')
    row = conn.execute("SELECT value FROM settings WHERE key = 'http_proxy'").fetchone()
    conn.close()
    stored = json.loads(row[0]) if row and row[0] else ''
    if stored != 'socks5://global:pw@g.example.dev:1080':
        findings.append(f'面板设置里的出口代理不对：{stored!r}')

    if findings:
        print('\nFAILURES:\n' + '\n'.join(findings))
        return 1
    print('\nALL CHECKS PASSED（含落盘核对）')
    return 0


JS = r'''
const BASE = process.env.BASE, OUT = process.env.OUT;
(async () => {
  const fs = await import('node:fs'); const path = await import('node:path');
  const {pathToFileURL} = await import('node:url');
  const c = path.join(process.env.TEMP||'/tmp','wb-i18n-verify','node_modules','playwright-core','index.js');
  const mod = await import(fs.existsSync(c)?pathToFileURL(c).href:'playwright-core');
  const chromium = mod.chromium ?? mod.default?.chromium;
  const root = path.join(process.env.LOCALAPPDATA,'ms-playwright');
  const dir = fs.readdirSync(root).filter(d=>d.startsWith('chromium-')&&!d.includes('headless_shell')).sort().pop();
  const b = await chromium.launch({executablePath: path.join(root,dir,'chrome-win64','chrome.exe')});
  const ctx = await b.newContext({viewport:{width:1440,height:1000}, deviceScaleFactor:2});
  const p = await ctx.newPage();
  const findings = [];
  const step = (ok, label, extra='') => {
    console.log(`  ${ok?'PASS':'FAIL'}  ${label}${extra?` -- ${extra}`:''}`);
    if (!ok) findings.push(label + (extra?': '+extra:''));
  };
  const body = () => p.evaluate(() => document.body.innerText);

  await p.goto(`${BASE}/login`,{waitUntil:'domcontentloaded'});
  // 首次访问会弹「菜单栏引导」气泡，正好盖在下面要截图的卡片上：先标成已看过
  await p.addInitScript(() => {
    try { window.localStorage.setItem('workbuddy-manager:dock-tip-dismissed', 'true'); } catch {}
  });
  await p.fill('#username','admin'); await p.fill('#password', process.env.PASS);
  await Promise.all([p.waitForURL(/dashboard/).catch(()=>{}), p.click('button[type=submit]')]);
  await p.goto(`${BASE}/settings/proxy`,{waitUntil:'load'});
  await p.waitForTimeout(2500);
  // mock 上游没有 docker，页面顶部会挂一条「上游重载失败」——截图前关掉它
  const notice = p.locator('button[aria-label="关闭提示"]').first();
  if (await notice.count()) await notice.click().catch(() => {});
  await p.waitForTimeout(300);

  let text = await body();
  step(text.includes('代理线路'), '页面渲染出「代理线路」卡片');
  step(text.includes('优先级') || text.includes('轮询'), '池策略控件在（下拉）');
  step(text.includes('池内启用'), '显示池内启用线路数');
  step(text.includes('px-dead') && text.includes('px-free'), '两条线路都在列表里');
  step(text.includes('user:***@127.0.0.1:1'), '列表里的地址是脱敏的');
  step(!text.includes('supersecret'), '密码没有出现在页面文本里');
  const tokyoText = await p.locator('tr', {hasText: 'px-tokyo'}).first().innerText();
  step(tokyoText.includes('30') && tokyoText.includes('已停用'),
       '对象形态线路的权重与停用状态都读出来了', tokyoText.replace(/\s+/g,' ').slice(0, 60));
  await p.screenshot({path:`${OUT}/proxy-list.png`, fullPage:true});

  // 新增一条线路（弹窗里改名 / 地址 / 权重 / 是否入池）
  await p.locator('button', {hasText: '添加线路'}).first().click();
  await p.waitForTimeout(600);
  const dlg = p.locator('[role=dialog]').first();
  await dlg.locator('input').nth(0).fill('px-hk');
  await dlg.locator('input').nth(1).fill('socks5://demo:pw@hk.example.dev:10000');
  await dlg.screenshot({path:`${OUT}/proxy-add.png`});
  await dlg.locator('button', {hasText: '保存'}).first().click();
  await p.waitForTimeout(1800);
  text = await body();
  step(text.includes('px-hk'), '新增的线路出现在列表里');

  // 批量导入
  await p.locator('button', {hasText: '导入'}).first().click();
  await p.waitForTimeout(600);
  const imp = p.locator('[role=dialog]').first();
  await imp.locator('textarea').fill('px-import=http://u:pw@imp.example:3128');
  await imp.locator('button', {hasText: '开始导入'}).first().click();
  await p.waitForTimeout(2000);

  // 绑定管理：逐行「默认（跟随池策略）/ 某条线路」
  await p.locator('button', {hasText: '绑定账号'}).first().click();
  await p.waitForTimeout(1500);
  const bind = p.locator('[role=dialog]').last();
  const bindText = await bind.innerText();
  step(bindText.includes('演示号') && bindText.includes('备用号'), '绑定管理里列出了账号');
  step(bindText.includes('默认（跟随池策略）'), '提供「默认（跟随池策略）」选项');
  await bind.screenshot({path:`${OUT}/proxy-bindings.png`});
  // 把演示号从 px-dead 改绑到 px-shanghai（按行定位，顺序变了也不会挑错人）
  const demoRow = bind.locator('div.flex.items-center.gap-3').filter({hasText: '演示号'}).first();
  await demoRow.locator('button[role=combobox]').first().click();
  await p.waitForTimeout(400);
  await p.locator('[role=option]', {hasText: 'px-shanghai'}).first().click();
  await p.waitForTimeout(1500);
  await bind.locator('button', {hasText: '关闭'}).first().click();
  await p.waitForTimeout(500);

  // 自动分配：给 px-hk 分 1 台（未绑的「备用号」应当被挑中）
  const row = p.locator('tr', {hasText: 'px-hk'}).first();
  await row.locator('button[title="自动分配"]').first().click();
  await p.waitForTimeout(600);
  const auto = p.locator('[role=dialog]').last();
  await auto.locator('input.tabular-nums').first().fill('1');
  await auto.locator('button', {hasText: '开始分配'}).first().click();
  await p.waitForTimeout(1800);
  const hkText = await p.locator('tr', {hasText: 'px-hk'}).first().innerText();
  step(hkText.includes('备用号'), '自动分配把未绑定的账号挂到线路上', hkText.replace(/\s+/g,' ').slice(0, 60));

  // 全局上游代理：开关 + 地址 + 保存
  await p.locator('input[placeholder*="socks5"]').first()
         .fill('socks5://global:pw@g.example.dev:1080');
  await p.locator('button', {hasText: '保存'}).first().click();
  await p.waitForTimeout(4200);          // 等「已保存」提示自己消失，截图干净
  text = await body();
  step(/由面板设置生效|global:pw/.test(text), '全局代理保存后显示生效来源', text.slice(0, 60));
  if (await notice.count()) await notice.click().catch(() => {});   // 保存又会触发重载提醒
  await p.waitForTimeout(400);
  await p.screenshot({path:`${OUT}/proxy-global.png`, fullPage:true});

  // 不可达线路的测试按钮不能报「出口正常」
  await p.locator('tr', {hasText: 'px-dead'}).first()
         .locator('button[title="测试"]').first().click();
  await p.waitForTimeout(9000);
  text = await body();
  step(!/出口正常/.test(text), '不可达的线路没有报「出口正常」');

  await b.close();
  if (findings.length) { console.log('\\nFAILURES:\\n'+findings.join('\\n')); process.exit(1); }
  console.log('\\nALL CHECKS PASSED');
})().catch(e=>{console.error('harness error:',e);process.exit(2)});
'''


if __name__ == "__main__":
    raise SystemExit(main())

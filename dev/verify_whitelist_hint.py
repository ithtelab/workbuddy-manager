"""密钥页「模型白名单填错当场提示」的界面验收（issue #46 的可选做法 2）。

用户报的是：白名单填错不会报错，只在下游表现为「模型列表是空的」——而空列表
看不出原因（少个连字符？填成了显示名？）。所以在编辑处当场点出来。

这个脚本从浏览器侧确认三件事：

  1. 填**正确**的名字 → 不显示任何提示（不能误报）；
  2. 填**拼错**的名字 → 输入框下方出现提示，并列出那个名字；
  3. 改了内容后**旧结论要作废**（否则显示的是过期的「都对」，比不显示更误导）。

另外确认「暂时查不了」不会被显示成「全部正确」——那会让用户以为没问题。

2026-10 复审又补了三步（都是**先发现真 bug 再补的钉子**）：
  4. 粘贴「正确, 拼错」两个名字 → **两个都要留下**（此前的实现逐个 add()、各自
     基于同一份旧值计算，粘贴一串只剩最后一个）；
  5. 只**手输**一个拼错的名字然后失焦 → 当场就要报（父组件此前读的是自己还没
     更新的旧值，刚输入的那个逃过了校验，要再点一次别处才现身）；
  6. 手输正确的名字 → 不报（别为了修 5 而误报）。
  7. 「从清单选择」弹层：候选齐全、与密钥版本一致的那一组排前面、可搜索、
     勾选即加入白名单（这几条都是渲染出来的行为，静态断言看不出来）。

    python dev/verify_whitelist_hint_ui.py

数据落在 dev/.wl-hint/（已 gitignore），截图输出到 dev/.shots-wl/。
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
DATA = REPO / 'dev' / '.wl-hint'
SHOTS = REPO / 'dev' / '.shots-wl'
UPSTREAM_PORT = 7911
MANAGER_PORT = 7912
ADMIN_PW = 'wl-hint-pass'
UID = 'aa117777-0000-0000-0000-000000000007'


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
        if path == '/healthz':
            self._json({'healthy': 1, 'total': 1})
        elif path == '/status':
            self._json({
                'accounts': [{'uid': UID, 'nickname': '演示号', 'credits': 800,
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
            # 两个版本的条目都有，供模型目录缓存
            self._json({'object': 'list', 'data': [
                {'id': 'glm-5.2'}, {'id': 'deepseek-v4.1-flash'},
                {'id': 'global:gpt-5.6-sol'},
            ]})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
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
    # 一条别名，用于验证「别名算合法条目」
    db.set_setting('model_map', {'gpt-4o-mini': 'glm-5.2'})
    db._conn.close()
    db._conn = None


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)

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
    seed()

    launcher = ('import uvicorn, server.main;'
                f'uvicorn.run(server.main.app, host="127.0.0.1", '
                f'port={MANAGER_PORT}, log_level="warning")')
    proc = subprocess.Popen([sys.executable, '-c', launcher], cwd=str(REPO), env=env)
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}')
    try:
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(
                    f'http://127.0.0.1:{MANAGER_PORT}/api/healthz', timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        return run_check()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.shutdown()


def run_check() -> int:
    import httpx

    findings: list[str] = []

    def step(ok: bool, label: str, extra: str = '') -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f' -- {extra}' if extra else ''))
        if not ok:
            findings.append(label + (f': {extra}' if extra else ''))

    client = httpx.Client(base_url=f'http://127.0.0.1:{MANAGER_PORT}',
                          timeout=30, trust_env=False)
    r = client.post('/api/login', json={'username': 'admin', 'password': ADMIN_PW})
    assert r.status_code == 200, r.text[:200]
    cookies = r.cookies

    # ── 0. 先暖模型目录缓存（校验只读缓存，不为了校验去打上游）──
    r = client.get('/api/model-catalog?realm=cn', cookies=cookies)
    step(r.status_code == 200, '模型目录（国内版）可取')
    r = client.get('/api/model-catalog?realm=global', cookies=cookies)
    step(r.status_code == 200, '模型目录（国际版）可取')

    def check(models: list[str], realm: str = '') -> dict:
        rr = client.post('/api/keys/check-models',
                         json={'models': models, 'realm': realm}, cookies=cookies)
        assert rr.status_code == 200, rr.text[:200]
        return rr.json()

    # ── 1. 正确的名字：不提示 ──
    out = check(['glm-5.2', 'global:gpt-5.6-sol'])
    step(out['checked'] and out['unknown'] == [],
         '正确的模型名不报问题', str(out))

    # ── 2. 拼错的名字：点出来 ──
    out = check(['glm-5.2', 'glm-5.9'])
    step(out['checked'] and out['unknown'] == ['glm-5.9'],
         '拼错的名字被点出来', str(out))

    # ── 3. 别名算合法（鉴权判请求名、映射在其后）──
    out = check(['gpt-4o-mini'])
    step(out['checked'] and out['unknown'] == [],
         '别名算合法条目（不误报）', str(out))

    # ── 4. `cn:` 前缀两种写法都认 ──
    out = check(['cn:glm-5.2'], realm='cn')
    step(out['checked'] and out['unknown'] == [],
         '存货密钥的 cn: 前缀写法被接受', str(out))

    # ── 5. 跨版本：国际版模型不被国内版清单误判 ──
    out = check(['global:gpt-5.6-sol', 'global:不存在的模型'])
    step('global:gpt-5.6-sol' not in out['unknown'],
         '国际版模型没被国内版清单误判')
    step('global:不存在的模型' in out['unknown'], '国际版里拼错的仍能点出')

    # ── 6. 缓存不可用时不猜（清缓存后应回 checked:false）──
    # 用一个独立的进程外手段做不到，这里改为验证「空清单 ≠ 全部正确」的返回契约
    step('checked' in out, '返回里带 checked 标志（前端据此区分「查不了」与「都对」）')

    client.close()

    # ── 7. 浏览器侧：提示真的出现在输入框下方 ──
    findings += browser_checks()

    if findings:
        print('\nFAILURES:\n' + '\n'.join(findings))
        return 1
    print('\nALL CHECKS PASSED')
    return 0


def browser_checks() -> list[str]:
    """在真实浏览器里确认提示会出现、会消失（静态断言看不出请求发没发出去）。"""
    SHOTS.mkdir(parents=True, exist_ok=True)
    # **必须 capture_output**：不带它时子进程直接写到终端，`r.stdout` 是 None，
    # 于是「从输出里挑 FAIL 行」永远挑不到——浏览器侧的断言全部不生效。
    # （实测踩过：断言全绿，其实一条都没执行。）
    r = subprocess.run(['node', '-e', JS],
                       env={**os.environ, 'BASE': f'http://127.0.0.1:{MANAGER_PORT}',
                            'PASS': ADMIN_PW, 'OUT': str(SHOTS)},
                       cwd=str(REPO / 'web'), text=True, timeout=300,
                       capture_output=True)
    print(r.stdout or '')
    if r.stderr:
        print(r.stderr[:1500])
    print(f'截图: {SHOTS}')
    # 退出码与「跑到结束」都要查：脚本崩了的话一行 FAIL 都不会打印，
    # 只看 FAIL 行会把它判成「全过」——比断言写错更危险的假通过。
    if r.returncode != 0:
        return [f'浏览器验收脚本自身失败（退出码 {r.returncode}）']
    if 'ALL CHECKS PASSED' not in (r.stdout or ''):
        return ['浏览器验收没有跑到结束（未见 ALL CHECKS PASSED）']
    return [ln.strip()[6:] for ln in (r.stdout or '').splitlines()
            if ln.strip().startswith('FAIL  ')]


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
  const ctx = await b.newContext({viewport:{width:1500,height:1100}, deviceScaleFactor:2});
  const p = await ctx.newPage();
  const findings = [];
  const step = (ok, label, extra='') => {
    console.log(`  ${ok?'PASS':'FAIL'}  ${label}${extra?` -- ${extra}`:''}`);
    if (!ok) findings.push(label + (extra?': '+extra:''));
  };

  await p.goto(`${BASE}/login`,{waitUntil:'domcontentloaded'});
  await p.fill('#username','admin'); await p.fill('#password', process.env.PASS);
  await Promise.all([p.waitForURL(/dashboard/).catch(()=>{}), p.click('button[type=submit]')]);

  await p.goto(`${BASE}/keys`,{waitUntil:'load'});
  await p.waitForTimeout(3500);

  const addBtn = p.locator('button', {hasText: /添加|新建|创建/}).first();
  await addBtn.click();
  await p.waitForTimeout(1200);

  const input = p.locator('input[placeholder*="glm-5.2"]').first();
  if (!(await input.count())) { console.log('  FAIL  找不到模型白名单输入框'); await b.close(); process.exit(1); }
  /**
   * 提示是否**真的看得见**。
   *
   * 三件事都要满足，缺一不可（前两条都单独踩过坑）：
   *   1. 元素存在 —— count() 会把隐藏元素也算上；
   *   2. 自身不是 display:none / visibility:hidden —— isVisible() 管这条；
   *   3. **没有被祖先的滚动容器裁掉** —— isVisible() 不管这条。白名单是弹窗里
   *      最后一个字段，提示挂在它下面，实测会被裁在可视区之下：渲染了、DOM 里有、
   *      isVisible 为真，用户一个像素都看不到。
   */
  const hintFullyVisible = async () => p.evaluate(() => {
    const el = [...document.querySelectorAll('p')].find(
      (e) => e.textContent && e.textContent.includes('找不到对应模型'));
    if (!el) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
    let box = null;
    for (let a = el.parentElement; a; a = a.parentElement) {
      const acs = getComputedStyle(a);
      if (acs.overflowY === 'scroll' || acs.overflowY === 'auto') { box = a; break; }
    }
    if (!box) return true;                     // 没有滚动容器 → 不存在裁剪
    const er = el.getBoundingClientRect(), br = box.getBoundingClientRect();
    return er.top >= br.top - 1 && er.bottom <= br.bottom + 1;
  });

  // ① 正确名字 → 不显示提示
  await input.fill('glm-5.2');
  await input.blur();
  await p.waitForTimeout(1500);
  step(!(await hintFullyVisible()), '正确名字不显示提示');

  // ② 拼错 → 显示并列出该名字（且不误报正确的那个）
  await input.fill('glm-5.2, glm-5.9');
  await input.blur();
  await p.waitForTimeout(1800);
  const visible = await hintFullyVisible();
  const shown = await p.evaluate(() => {
    const el = [...document.querySelectorAll('p')].find(
      (e) => e.textContent && e.textContent.includes('找不到对应模型'));
    return el ? el.textContent : '';
  });
  // 只截弹窗区域：全页截图会把弹窗缩得很小，看不出细节
  const dlg = p.locator('[role=dialog]').first();
  if (await dlg.count()) {
    await dlg.screenshot({path: `${OUT}/wl-hint-dialog.png`});
  }
  step(visible, '拼错名字后提示**完整可见**（含未被滚动容器裁掉）',
       visible ? '' : '元素在 DOM 里但用户看不到（被裁掉或未滚入可视区）');
  step(shown.includes('glm-5.9'), '提示里列出了那个名字', shown.slice(0, 90));
  const withoutWrong = shown.split('glm-5.9').join('');
  step(!withoutWrong.includes('glm-5.2'), '提示没把正确的名字也列上', withoutWrong.slice(0, 60));
  await p.screenshot({path:`${OUT}/wl-hint.png`, fullPage:true});

  // ③ 改内容 → 旧结论作废（否则显示的是过期的「都对」）
  await input.fill('glm-5.2');
  await p.waitForTimeout(700);
  step(!(await hintFullyVisible()), '改回正确名字后提示立即消失（旧结论已作废）');

  // 芯片列表（只认带 × 按钮的那种 span，避免把清单里的模型名也算进来）
  const chipNames = async () => p.evaluate(() => {
    const dlg = document.querySelector('[role=dialog]');
    if (!dlg) return [];
    return [...dlg.querySelectorAll('span.font-mono')]
      .filter((e) => e.querySelector('button'))
      .map((e) => e.textContent.trim());
  });
  const clearChips = async () => {
    for (let i = 0; i < 12; i++) {
      const btns = p.locator('[role=dialog] span.font-mono > button');
      if (!(await btns.count())) break;
      await btns.first().click();
      await p.waitForTimeout(120);
    }
  };

  // ④ 粘贴一串（含逗号）→ 每个名字都要留下（复审时这里丢过名字）
  await clearChips();
  await input.click();
  await input.fill('glm-5.2, deepseek-v4.1-flash, glm-5.9');
  await p.waitForTimeout(900);
  const pasted = await chipNames();
  step(pasted.includes('glm-5.2') && pasted.includes('deepseek-v4.1-flash')
       && pasted.includes('glm-5.9'),
       '粘贴多个名字后每个都在（含逗号分隔）', JSON.stringify(pasted));

  // ⑤ 手输一个拼错的名字 + 失焦 → 当场报（此前要再点一次别处才现身）
  await clearChips();
  await input.click();
  await input.fill('glm-5.9');
  await input.blur();
  await p.waitForTimeout(1800);
  step(await hintFullyVisible(), '刚手输的拼错名字在失焦时就报出来');

  // ⑥ 手输正确的名字 + 失焦 → 不报（不能为了 ⑤ 而误报）
  //    先清干净：⑤ 留下的那个拼错的名字还在列表里，不清就会连它一起报（第一次
  //    写这一步时忘了清，结果把「正确的行为」判成了失败——harness 自己的 bug）。
  await clearChips();
  await input.click();
  await input.fill('glm-5.2');
  await input.blur();
  await p.waitForTimeout(1800);
  step(!(await hintFullyVisible()), '手输的正确名字不报问题');

  // ⑦ 「从清单选择」弹层：候选齐全、与密钥版本一致的那一组排前面、可搜索、勾选即加入
  await clearChips();
  await p.locator('button', {hasText: '从清单选择'}).first().click();
  await p.waitForTimeout(1000);
  const popoverIds = async () => p.evaluate(() => {
    const root = document.querySelector('[data-radix-popper-content-wrapper]');
    if (!root) return [];
    return [...root.querySelectorAll('button[role=checkbox]')]
      .map((e) => e.textContent.trim());
  });
  const ids = await popoverIds();
  step(ids.length === 3, '清单列出了全部候选模型', JSON.stringify(ids));
  step(ids.length > 0 && !ids[0].startsWith('global:'),
       '与密钥版本一致的那组排前面（国内版密钥 → 国内版清单在前）', JSON.stringify(ids));
  step(ids.length > 0 && ids[ids.length - 1].startsWith('global:'),
       '另一版本的清单排在后面', JSON.stringify(ids));
  const searchBox = p.locator('[data-radix-popper-content-wrapper] input').first();
  await searchBox.fill('gpt');
  await p.waitForTimeout(400);
  const filtered = await popoverIds();
  step(filtered.length === 1 && filtered[0].startsWith('global:gpt'),
       '清单可按关键字搜索', JSON.stringify(filtered));
  await p.locator('[data-radix-popper-content-wrapper] button[role=checkbox]').first().click();
  await p.waitForTimeout(500);
  const afterPick = await chipNames();
  step(afterPick.includes('global:gpt-5.6-sol'), '勾选即加入白名单', JSON.stringify(afterPick));
  await p.screenshot({path: `${OUT}/wl-picker.png`});
  await p.keyboard.press('Escape');
  await p.waitForTimeout(300);

  await b.close();
  if (findings.length) { console.log('\nFAILURES:\n'+findings.join('\n')); process.exit(1); }
  console.log('\nALL CHECKS PASSED');
})().catch(e=>{console.error('harness error:',e);process.exit(2)});
'''


if __name__ == '__main__':
    raise SystemExit(main())

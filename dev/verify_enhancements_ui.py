"""浏览器冒烟：PR #166 新增的两个界面（积分收支卡片 / 一键刷新令牌 + 令牌刷新计划）。

为什么值得单独跑：这一批是「后端接口 + 前端卡片」的成对改动，单测只覆盖到接口层，
而它踩过的坑恰恰在渲染层 —— 作者自己修过一次 `TypeError: Cannot read properties of
undefined`（收支接口返回残缺时整页崩）。所以这里真起面板、真登录、真进页面：
  · 页面能渲染出来（没有落进错误边界）；
  · 新增的卡片与按钮都在；
  · 浏览器控制台**没有**报错（崩过的那种就是在这里现形的）。

    python dev/verify_enhancements_ui.py

数据落在 dev/.enhance-ui/（已 gitignore），截图输出到 dev/.shots-enhance/。
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
DATA = REPO / 'dev' / '.enhance-ui'
SHOTS = REPO / 'dev' / '.shots-enhance'
UPSTREAM_PORT = 7951
PANEL_PORT = 7952
ADMIN_PW = 'enhance-ui-pass'


class Upstream(BaseHTTPRequestHandler):
    """最小 mock 上游：面板启动后会探测，页面也会顺带拉账号/积分，给个空壳就行。"""

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
        elif self.path.startswith('/pool/status'):
            self._json({'total': 1, 'healthy': 1, 'cooling': 0})
        else:
            self._json({})

    def do_POST(self):
        self._json({})


def seed() -> None:
    sys.path.insert(0, str(REPO))
    from server import config, db

    config.DB_PATH = DATA / 'manager.db'
    db._conn = None
    db.connect()
    db._conn.close()
    db._conn = None


def _seed_chart_data() -> None:
    """喂几天用量与积分流水，让「积分收支」图真的画出线来。

    数值刻意用 4 位数：issue #184 报的就是「四位数以上的 Y 轴刻度被绘图区压住」，
    没有真实数值时那张图是空态，这条断言等于没测。
    """
    import sqlite3
    import time as _time

    conn = sqlite3.connect(DATA / 'manager.db')
    today = _time.strftime('%Y-%m-%d')
    day0 = _time.strftime('%Y-%m-%d', _time.localtime(_time.time() - 3 * 86400))
    conn.executemany(
        'INSERT OR REPLACE INTO usage_daily(day, key_id, model, requests, '
        'prompt_tokens, completion_tokens, credit, realm) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        [(today, 1, 'glm-5.2', 3, 1000, 500, 4200.0, 'cn'),
         (day0, 1, 'glm-5.2', 2, 800, 400, 1600.0, 'cn')],
    )
    now = int(_time.time())
    conn.executemany(
        'INSERT OR IGNORE INTO task_logs(ts, uid, kind, level, credits, message, dedup_key) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        [(now, 'demo-uid', 'credit', 'ok', 3200, '签到 +3200', 'seed-a'),
         (now - 86400, 'demo-uid', 'credit', 'ok', 1400, '活跃 +1400', 'seed-b')],
    )
    conn.commit()
    conn.close()


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    (DATA / 'auths').mkdir(parents=True)
    (DATA / 'config.json').write_text(json.dumps({'api_key': 'wbk_keep', 'proxies': {}},
                                                 ensure_ascii=False), encoding='utf-8')
    (DATA / 'auths' / 'workbuddy-demo.json').write_text(json.dumps({
        'account': {'uid': 'demo-uid', 'nickname': '演示号'},
        'auth': {'accessToken': 'x'},
    }, ensure_ascii=False), encoding='utf-8')
    seed()
    _seed_chart_data()

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
        findings.append(f'冒烟脚本自身失败（退出码 {r.returncode}）')
    if 'ALL CHECKS PASSED' not in (r.stdout or ''):
        findings.append('冒烟没有跑到结束（未见 ALL CHECKS PASSED）')
    findings += [ln.strip()[6:] for ln in (r.stdout or '').splitlines()
                 if ln.strip().startswith('FAIL  ')]
    if findings:
        print('\nFAILURES:\n' + '\n'.join(findings))
        return 1
    print('\nALL CHECKS PASSED')
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
  const errors = [];
  p.on('console', m => { if (m.type()==='error') errors.push(m.text().slice(0,200)); });
  p.on('pageerror', e => errors.push('pageerror: '+String(e).slice(0,200)));
  const step = (ok, label, extra='') => {
    console.log(`  ${ok?'PASS':'FAIL'}  ${label}${extra?` -- ${extra}`:''}`);
    if (!ok) findings.push(label + (extra?': '+extra:''));
  };
  const body = () => p.evaluate(() => document.body.innerText);

  await p.addInitScript(() => {
    try { window.localStorage.setItem('workbuddy-manager:dock-tip-dismissed', 'true'); } catch {}
  });
  await p.goto(`${BASE}/login`,{waitUntil:'domcontentloaded'});
  await p.fill('#username','admin'); await p.fill('#password', process.env.PASS);
  await Promise.all([p.waitForURL(/dashboard/).catch(()=>{}), p.click('button[type=submit]')]);
  await p.waitForTimeout(2500);

  // ── 仪表盘：积分收支卡片 ──
  let text = await body();
  step(text.includes('积分收支'), '仪表盘渲染出「积分收支」卡片');
  step(!/Cannot read|undefined is not/.test(text), '页面没有出现崩溃文案');
  await p.screenshot({path:`${OUT}/dashboard-credits.png`, fullPage:true});

  // ── 账号页：一键刷新令牌 + 令牌刷新计划 ──
  await p.goto(`${BASE}/accounts`,{waitUntil:'load'});
  await p.waitForTimeout(2500);
  text = await body();
  step(text.includes('一键刷新令牌') || text.includes('刷新令牌'), '账号页有「一键刷新令牌」入口');
  step(!text.includes('计划任务'),
       '账号页**不再**显示计划任务（它不属于账号这一块）');
  step(text.includes('演示号'), '账号列表正常渲染', text.slice(0,40));
  await p.screenshot({path:`${OUT}/accounts-refresh.png`, fullPage:true});

  // 点一次「一键刷新令牌」：mock 上游会返回空，无论成败都**不能**把页面打崩
  const btn = p.locator('button', {hasText: '一键刷新令牌'}).first();
  if (await btn.count()) {
    await btn.click();
    await p.waitForTimeout(2500);
    text = await body();
    step(!/Cannot read|undefined is not/.test(text), '刷新令牌后页面没崩');
  } else {
    step(false, '找不到「一键刷新令牌」按钮');
  }

  // ── 任务页：计划任务卡片应该在这里（用户反馈：原先挂在账号页）──
  await p.goto(`${BASE}/tasks`,{waitUntil:'load'});
  await p.waitForTimeout(2500);
  text = await body();
  step(text.includes('计划任务') && text.includes('令牌刷新'), '任务页有「计划任务」卡片');
  step(text.includes('每日任务') && text.includes('每日签到'), '卡片里含每日任务那一组');
  await p.screenshot({path:`${OUT}/tasks-plan.png`, fullPage:true});

  // ── 系统更新页：离线升级包卡片（issue #153）──
  await p.goto(`${BASE}/settings/system`,{waitUntil:'load'});
  await p.waitForTimeout(2500);
  text = await body();
  step(text.includes('离线升级包'), '系统更新页有「离线升级包」卡片');
  step(text.includes('发布包文件') && text.includes('sha256'), '卡片里有文件选择与摘要输入');
  step(text.includes('一键更新') || text.includes('更新'), '更新卡片仍在（没被新卡片挤掉）');
  await p.screenshot({path:`${OUT}/offline-package.png`, fullPage:true});

  // ── 图表轴刻度不能被绘图区压住（issue #184：4 位数刻度被盖住）──
  await p.goto(`${BASE}/dashboard`,{waitUntil:'load'});
  await p.waitForTimeout(3000);
  // 按「带折线的那个 SVG」定位：页面上另一张图是面积图（调用趋势），别认错
  const chart = p.locator('svg.recharts-surface').filter({
    has: p.locator('path.recharts-line-curve'),
  }).first();
  if (await chart.count()) {
    const geom = await chart.evaluate((svg) => {
      const svgBox = svg.getBoundingClientRect();
      const rel = (box) => ({left: box.left - svgBox.left, right: box.right - svgBox.left});
      // 左轴刻度是右对齐（text-anchor=end），右轴是左对齐 —— 只查左轴那批
      const texts = Array.from(svg.querySelectorAll('text'))
        .filter(t => (t.getAttribute('text-anchor') || '') === 'end')
        .map(t => ({label: (t.textContent || '').trim(), ...rel(t.getBoundingClientRect())}))
        .filter(x => x.label);
      const curves = Array.from(svg.querySelectorAll('path.recharts-line-curve'))
        .map(c => c.getBoundingClientRect());
      const plotLeft = curves.length ? Math.min(...curves.map(c => c.left - svgBox.left)) : null;
      return {texts, plotLeft, width: svgBox.width};
    });
    step(geom.texts.length > 0, '找到左轴刻度（说明图真的画出来了）',
         `刻度: ${geom.texts.map(t => t.label).join(',')} | plotLeft=${geom.plotLeft === null ? 'n/a' : Math.round(geom.plotLeft)}`);
    const clipped = geom.texts.filter(t => t.left < -1);
    step(clipped.length === 0, '轴刻度没有被左边缘裁掉',
         clipped.map(t => `${t.label}@${Math.round(t.left)}`).slice(0,3).join(' '));
    // 刻度整体必须落在绘图区左侧：右边缘不能越过绘图区左边界（越过 = 被图表压住）
    const overlapped = geom.plotLeft === null ? [] :
      geom.texts.filter(t => t.right > geom.plotLeft + 1);
    step(overlapped.length === 0, '轴刻度没有压到绘图区（4 位数也不许）',
         overlapped.map(t => `${t.label}: right=${Math.round(t.right)} > plotLeft=${Math.round(geom.plotLeft)}`)
           .slice(0,3).join(' | '));
    await chart.screenshot({path:`${OUT}/dashboard-credits-chart.png`});
  } else {
    step(false, '找不到「积分收支」卡片（aria-label 变了？）');
  }

  const realErrors = errors.filter(e => !/favicon|404|Failed to load resource/i.test(e));
  step(realErrors.length === 0, '控制台没有报错', realErrors.slice(0,3).join(' | '));

  await b.close();
  if (findings.length) { console.log('\nFAILURES:\n'+findings.join('\n')); process.exit(1); }
  console.log('\nALL CHECKS PASSED');
})().catch(e=>{console.error('harness error:',e);process.exit(2)});
'''


if __name__ == '__main__':
    raise SystemExit(main())

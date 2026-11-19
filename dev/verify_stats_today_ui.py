"""用量页「默认今日」的界面验收（issue #53）。

起一个真管理端（假上游 + 临时数据目录），再用真浏览器打开用量统计页，确认：

  1. 时段选择器**默认就是「今日」**，且页面确实按 days=1 拉数据；
  2. 「今日」排在选项第一位，四项齐全；
  3. 切到近 7 天后，趋势图与两张分解表**一起跟随**（三处请求都带 days=7）；
  4. 图表副标题随窗口变：一天时是「当日汇总」，多天时回到「按天聚合」。

为什么要真跑浏览器：这次改的是**默认值**与**同屏口径**，单测能钉住源码文本，
钉不住「界面上到底显示什么、请求到底带了什么参数」。

    python dev/verify_stats_today_ui.py

  数据落在 dev/.stats-today/（已 gitignore），截图由 Node 侧写到 dev/.shots-stats/。
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
DATA = REPO / 'dev' / '.stats-today'
SHOTS = REPO / 'dev' / '.shots-stats'
UPSTREAM_PORT = 7921
MANAGER_PORT = 7922
ADMIN_PW = 'stats-today-pass'
UID = 'bb225555-0000-0000-0000-000000000005'


def _jwt(uid: str) -> str:
    import base64
    head = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b'=').decode()
    body = base64.urlsafe_b64encode(json.dumps({
        'sub': uid, 'exp': int(time.time()) + 60 * 86400,
    }).encode()).rstrip(b'=').decode()
    return f'{head}.{body}.sig'


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
        if self.path.split('?')[0] in ('/healthz', '/status'):
            self._json({'healthy': 1, 'total': 1, 'accounts': [
                {'uid': UID, 'nickname': '演示号', 'credits': 800,
                 'disabled': False, 'cooling': False},
            ]})
        else:
            self._json({'error': {'message': 'not found', 'code': 'not_found'}}, 404)


def seed() -> None:
    """预置今天与几天前的用量，让趋势图两个窗口下都有东西可画。"""
    sys.path.insert(0, str(REPO))
    os.environ.update({
        'WB_DB': str(DATA / 'manager.db'),
        'WB_AUTH_DIR': str(DATA / 'auths'),
    })
    from server import config, db
    config.DB_PATH = DATA / 'manager.db'
    config.AUTH_DIR = DATA / 'auths'
    db._conn = None
    db.connect()
    (DATA / 'auths').mkdir(parents=True, exist_ok=True)
    (DATA / 'auths' / f'workbuddy-{UID}.json').write_text(json.dumps({
        'account': {'uid': UID, 'enterpriseId': 'e', 'nickname': '演示号'},
        'auth': {'accessToken': _jwt(UID), 'refreshToken': 'r',
                 'expiresAt': int(time.time()) + 60 * 86400,
                 'domain': 'copilot.tencent.com', 'realm': 'cn'},
    }, ensure_ascii=False), encoding='utf-8')
    # 今天 3 次请求、3 天前 2 次（切到 7 天时图上应有两根柱子）。
    # 直接写表：`bump_usage` 固定记「今天」，而这里要造历史日期。
    now = int(time.time())
    today = time.strftime('%Y-%m-%d', time.localtime(now))
    past = time.strftime('%Y-%m-%d', time.localtime(now - 3 * 86400))
    for ts in (now - 300, now - 3600, now - 3 * 86400):
        db.add_request_log(ts=ts, key_id=None, ip='1.2.3.4', model='glm-5.2',
                           mapped_model='', status=200, prompt_tokens=100,
                           completion_tokens=50, latency_ms=10, first_token_ms=5,
                           ua='seed', error=None, stream=0, credit=1.5, realm='cn')
    for day, n in ((today, 3), (past, 2)):
        db.execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, '
            'completion_tokens, credit, realm) VALUES(?, 1, ?, ?, ?, ?, ?, ?)',
            (day, 'glm-5.2', n, 100 * n, 50 * n, 1.5 * n, 'cn'))
    # 今日再按小时铺开（「今日」趋势图读的是 usage_hourly）：当前小时与前面两个小时
    # 各一次，合计与上面那行按天的数据**完全一致** —— 图与卡片同源，验收里才敢互相对照。
    h = int(time.strftime('%H', time.localtime(now)))
    for hour in sorted({h, max(0, h - 1), max(0, h - 3)}):
        db.execute(
            'INSERT INTO usage_hourly(day, hour, key_id, model, requests, prompt_tokens, '
            'completion_tokens, credit, realm) VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?)',
            (today, hour, 'glm-5.2', 1, 100, 50, 1.5, 'cn'))
    db._conn.close()
    db._conn = None


def _playwright_entry() -> str | None:
    """playwright-core 的 index.js 真实路径（找不到返回 None，交给 Node 自己找）。

    为什么要显式给：在 MSYS/Git-Bash 下 `TEMP` 是 `/tmp`，而 Node 是原生 Windows
    进程——按 TEMP 拼路径会指向错的地方（实测报「找不到 playwright-core」）。
    """
    import tempfile
    for base in (Path(tempfile.gettempdir()), Path(os.environ.get('TEMP') or '')):
        p = base / 'wb-i18n-verify' / 'node_modules' / 'playwright-core' / 'index.js'
        if p.is_file():
            return str(p)
    return None


def main() -> int:
    for d in (DATA, SHOTS):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
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

        node_env = {
            **os.environ,
            'WB_BASE': f'http://127.0.0.1:{MANAGER_PORT}',
            'WB_USER': 'admin',
            'WB_PASS': ADMIN_PW,
            'WB_SHOTS': str(SHOTS),
            'PYTHONUTF8': '1',
            # Node 是原生 Windows 进程：把 playwright 的真实路径算好传过去
            # （MSYS 下 TEMP=/tmp，Node 侧拼出来会指向错位置）
            **({'WB_PLAYWRIGHT': _playwright_entry()}
               if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_stats_today.mjs'], cwd=str(REPO),
                           env=node_env, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        print(r.stdout or '')
        if r.stderr:
            print(r.stderr[-2000:], file=sys.stderr)
        if r.returncode != 0:
            return r.returncode
        if 'ALL CHECKS PASSED' not in (r.stdout or ''):
            print('✗ 浏览器侧没有报 ALL CHECKS PASSED —— 不能当作通过', file=sys.stderr)
            return 1
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.shutdown()


if __name__ == '__main__':
    sys.exit(main())

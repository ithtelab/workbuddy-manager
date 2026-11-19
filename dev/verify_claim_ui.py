"""红包抽奖页的界面验收（真服务 + 真浏览器）。

这一页是**公开**的：收到链接的人不登录、不注册，打开就该能看懂「这是什么、
值多少、点了会得到什么」，抽完还要一次拿到三样东西 —— 额度、密钥、**调用地址**。
任何一样缺失，用户就只能回来问，而这一页恰恰是给「不想问人」的场景做的。

断言的性质（不是措辞）：
  · 抽之前就能看到调用地址（带当前 origin 与 basePath）；
  · 抽之后弹窗里有额度、密钥、调用地址三块，且密钥与库里那份一致；
  · 彩屑层不截点击（按钮仍可点，点了有反馈）；
  · 「完成」会复制密钥（用 stub 的 clipboard 断言真的写进去了）。

    python dev/verify_claim_ui.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
DATA = REPO / 'dev' / '.claim-ui'
SHOTS = REPO / 'dev' / '.shots-claim-ui'
UPSTREAM_PORT = 7995
MANAGER_PORT = 7996
ADMIN_PW = 'claim-ui-pass'
UID = 'cl000000-0000-0000-0000-000000000013'


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
        if path in ('/healthz', '/status'):
            self._json({'healthy': 1, 'total': 1, 'accounts': [
                {'uid': UID, 'nickname': '演示号', 'disabled': False, 'cooling': False,
                 'success_count': 1, 'err_total': 0, 'credits': 100}],
                'realm_totals': {'cn': {'total': 1}, 'global': {'total': 0}}})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        self._json({'ok': True})


def seed_packet() -> tuple[str, str]:
    """造一个红包，返回（抽奖码, 第一份的明文 key 前缀）。"""
    os.environ.update({'WB_DB': str(DATA / 'manager.db'),
                       'WB_AUTH_DIR': str(DATA / 'auths')})
    from server import config, db, redpacket
    config.DB_PATH = DATA / 'manager.db'
    config.AUTH_DIR = DATA / 'auths'
    db._conn = None
    db.connect()
    out = redpacket.create_packet(
        name='验收红包', kind='token', total=5000, shares=2, mode='even',
        ttl_days=7, actor='admin', models=['glm-5.2'])
    code = out['code']
    key = out['keys'][0]['key']
    print(f'已造红包：份数 2、总额 5000 token、抽奖码 {code[:8]}…')
    db._conn.close()
    db._conn = None
    return code, key


def _playwright_entry() -> str | None:
    for base in (Path(tempfile.gettempdir()), Path(os.environ.get('TEMP') or '')):
        p = base / 'wb-i18n-verify' / 'node_modules' / 'playwright-core' / 'index.js'
        if p.is_file():
            return str(p)
    return None


def main() -> int:
    for d in (DATA, SHOTS):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    (DATA / 'auths').mkdir(parents=True)
    code, _key = seed_packet()

    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_STATIC_DIR': str(REPO / 'web' / 'out'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'PYTHONUTF8': '1',
    }
    proc = subprocess.Popen(
        [sys.executable, '-c',
         'import uvicorn, server.main;'
         f'uvicorn.run(server.main.app, host="127.0.0.1", port={MANAGER_PORT}, log_level="warning")'],
        cwd=str(REPO), env=env)
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}')
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{MANAGER_PORT}/api/healthz', timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        node_env = {
            **os.environ,
            'WB_BASE': f'http://127.0.0.1:{MANAGER_PORT}',
            'WB_CODE': code,
            'WB_SHOTS': str(SHOTS),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_claim_ui.mjs'], cwd=str(REPO),
                           env=node_env, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        print(r.stdout or '')
        if r.stderr:
            print(r.stderr[-1500:], file=sys.stderr)
        return r.returncode
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.shutdown()


if __name__ == '__main__':
    sys.exit(main())

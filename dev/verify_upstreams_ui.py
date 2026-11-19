"""多上游（PR #89）的界面验收：设置→上游的增删改/探测，以及密钥弹窗选上游。

作者说明了浏览器交互未在本地跑过（本机无 Playwright），请求合并前过一眼。这个脚本
补上，并且顺带钉住评审时补的脱敏：**上游的 api_key 不能明文出现在页面上**。

    python dev/verify_upstreams_ui.py
"""
from __future__ import annotations

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
DATA = REPO / 'dev' / '.upstreams-ui'
SHOTS = REPO / 'dev' / '.shots-upstreams-ui'
UP_PORT = 8003          # 「默认上游」的假上游
SECOND_PORT = 8004      # 新建的那个上游
MANAGER_PORT = 8005
ADMIN_PW = 'upstreams-pass'
SECRET = 'SECOND-UPSTREAM-KEY-abc123'


def make_upstream(port: int):
    class U(BaseHTTPRequestHandler):
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
            p = self.path.split('?')[0]
            if p in ('/healthz', '/status'):
                self._json({'healthy': 1, 'total': 0, 'accounts': [],
                            'realm_totals': {'cn': {'total': 0}, 'global': {'total': 0}}})
            elif p == '/v1/models':
                self._json({'object': 'list', 'data': []})
            else:
                self._json({'error': 'not found'}, 404)

        def do_POST(self):
            n = int(self.headers.get('content-length') or 0)
            if n:
                self.rfile.read(n)
            self._json({'ok': True})

    return U


def _playwright_entry() -> str | None:
    for base in (Path(tempfile.gettempdir()), Path(os.environ.get('TEMP') or '')):
        p = base / 'wb-i18n-verify' / 'node_modules' / 'playwright-core' / 'index.js'
        if p.is_file():
            return str(p)
    return None


def main() -> int:
    # 控制台编码兜底：脚本输出里有 ✓ 之类的字符，GBK 控制台直接 print 会崩
    try:
        sys.stdout.reconfigure(errors='replace')
    except Exception:  # noqa: BLE001
        pass

    for d in (DATA, SHOTS):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    (DATA / 'auths').mkdir(parents=True)

    up1 = ThreadingHTTPServer(('127.0.0.1', UP_PORT), make_upstream(UP_PORT))
    up2 = ThreadingHTTPServer(('127.0.0.1', SECOND_PORT), make_upstream(SECOND_PORT))
    threading.Thread(target=up1.serve_forever, daemon=True).start()
    threading.Thread(target=up2.serve_forever, daemon=True).start()

    static_dir = REPO / 'web' / 'out'
    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UP_PORT}',
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_STATIC_DIR': str(static_dir),
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
            'WB_PASS': ADMIN_PW,
            'WB_SECOND': f'http://127.0.0.1:{SECOND_PORT}',
            'WB_SECRET': SECRET,
            'WB_SHOTS': str(SHOTS),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_upstreams.mjs'], cwd=str(REPO),
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
        up1.shutdown()
        up2.shutdown()


if __name__ == '__main__':
    sys.exit(main())

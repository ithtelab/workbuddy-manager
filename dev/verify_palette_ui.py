"""命令面板（⌘K）与设置页子路由的冒烟验收（PR #111 / #108，跑在静态导出产物上）。

两件事都是「在本地 dev 看着好好的、到生产形态才可能不成立」的类型：

  · 命令面板挂的是**全局快捷键**与一个 Dialog —— 静态导出下 App Router 的挂载时机
    与 dev 不同，快捷键没装上就会「按了没反应」，而页面上看不出任何异常；
  · 设置页改成子路由后，`/settings/upstream` 必须是**真能打开**的地址（此前那个
    路由目录被 .gitignore 吞掉，点「上游配置」就是 404）。

    python dev/verify_palette_ui.py
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
DATA = REPO / 'dev' / '.palette'
SHOTS = REPO / 'dev' / '.shots-palette'
UPSTREAM_PORT = 8061
MANAGER_PORT = 8062
ADMIN_PW = 'palette-pass'


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
            self._json({'healthy': 0, 'total': 0, 'cooling': 0, 'disabled': 0,
                        'in_flight_full': 0, 'connected': True, 'accounts': [],
                        'realm_totals': {'cn': {'total': 0}, 'global': {'total': 0}}})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        self._json({'ok': True})


def _playwright_entry() -> str | None:
    for base in (Path(tempfile.gettempdir()), Path(os.environ.get('TEMP') or '')):
        p = base / 'wb-i18n-verify' / 'node_modules' / 'playwright-core' / 'index.js'
        if p.is_file():
            return str(p)
    return None


def main() -> int:
    try:
        sys.stdout.reconfigure(errors='replace')
    except Exception:  # noqa: BLE001
        pass
    for d in (DATA, SHOTS):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    (DATA / 'auths').mkdir(parents=True)

    static_dir = REPO / 'web' / 'out'
    if not (static_dir / 'index.html').is_file():
        print(f'缺少前端产物 {static_dir}，先跑 npm run build:export', file=sys.stderr)
        return 2

    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_DATA_DIR': str(DATA),
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
    base = f'http://127.0.0.1:{MANAGER_PORT}'
    print(f'管理端: {base}（静态产物：{static_dir.name}/）')
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f'{base}/api/healthz', timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        node_env = {
            **os.environ,
            'WB_BASE': base,
            'WB_PASS': ADMIN_PW,
            'WB_SHOTS': str(SHOTS),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_palette.mjs'], cwd=str(REPO),
                           env=node_env, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        print(r.stdout or '')
        if r.stderr:
            print(r.stderr[-1200:], file=sys.stderr)
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

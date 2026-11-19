"""「清除更新记录」的运行时验收（issue #105，跑在静态导出产物上）。

现场：一次失败的更新会**长期驻留** —— 状态文件只在下次发起更新时被删、update.log
只追加不截断；结果卡片右上角的 X 只是组件内 state（刷新就回来），日志区没有任何
清除入口。用户只能进容器手删 `/app/data/update-status.json` 与 `update.log`。

这个脚本起真面板（`WB_STATIC_DIR=web/out`）+ **预先造好的失败记录**，在浏览器里：

  A 失败记录可见时 → 有「清除记录」入口；日志区显示内容
  B 点清除并确认 → 结果卡片与日志区一起消失，磁盘上两个文件也没了
  C 更新进行中（锁 + running） → **不给**清除入口（避免把「正在更新」看丢）
  D 没有任何记录时 → 整块日志区不渲染（不再永远挂一个空框）

    python dev/verify_update_clear_ui.py
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
DATA = REPO / 'dev' / '.update-clear'
SHOTS = REPO / 'dev' / '.shots-update-clear'
UPSTREAM_PORT = 8051
MANAGER_PORT = 8052
ADMIN_PW = 'update-clear-pass'


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


def write_failed_record(data_dir: Path) -> None:
    """造一条「上次更新失败」的现场（状态 + 日志各一份）。"""
    (data_dir / 'update-status.json').write_text(json.dumps({
        'running': False, 'ok': False, 'target': 'manager',
        'step': '更新未完成', 'pid': 424242,
        'started_at': int(time.time()) - 300, 'finished_at': int(time.time()) - 240,
        'duration': 60.0,
        'logs': [{'ts': int(time.time()) - 290, 'level': 'info', 'text': '下载 https://github.com/…tar.gz'},
                 {'ts': int(time.time()) - 250, 'level': 'error', 'text': '更新失败：The read operation timed out'}],
        'signature': {'status': 'none', 'detail': ''},
    }, ensure_ascii=False), encoding='utf-8')
    (data_dir / 'update.log').write_text(
        '[00:00:01] 开始更新（target=manager）\n[00:00:05] 下载 https://github.com/…tar.gz\n'
        '[00:01:00] 更新失败：The read operation timed out\n', encoding='utf-8')


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
    auth_dir = DATA / 'auths'
    auth_dir.mkdir(parents=True)

    static_dir = REPO / 'web' / 'out'
    if not (static_dir / 'index.html').is_file():
        print(f'缺少前端产物 {static_dir}，先跑 npm run build:export', file=sys.stderr)
        return 2

    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_AUTH_DIR': str(auth_dir),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_DATA_DIR': str(DATA),
        'WB_STATIC_DIR': str(static_dir),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'PYTHONUTF8': '1',
    }
    write_failed_record(DATA)
    proc = subprocess.Popen(
        [sys.executable, '-c',
         'import uvicorn, server.main;'
         f'uvicorn.run(server.main.app, host="127.0.0.1", port={MANAGER_PORT}, log_level="warning")'],
        cwd=str(REPO), env=env)
    base = f'http://127.0.0.1:{MANAGER_PORT}'
    print(f'管理端: {base}（静态产物：{static_dir.name}/，已造好一条失败记录）')
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
            'WB_DATA_DIR': str(DATA),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_update_clear.mjs'], cwd=str(REPO),
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

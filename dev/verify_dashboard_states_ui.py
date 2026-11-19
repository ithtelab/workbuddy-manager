"""仪表盘首屏状态的运行时验收（PR #83 的补充，跑在**静态导出产物**上）。

贡献者做的是 `next dev` + mock 后端；他明确说静态导出（生产形态）还没复核过。
这个脚本补上：起真面板（`WB_STATIC_DIR=web/out`，即生产用的静态产物）+ 假上游，
登录后在**浏览器层**拦截面板自己的接口，逐个驱动四种状态：

    正常      全部放行            内容出现，无骨架
    慢        每个接口延迟 4s     加载期有骨架、且**不出现「暂无账号」**（本次要修的核心）
    部分失败  daily / status 500  已有内容保留 + 顶部常驻「部分数据加载失败」+ 重试
    全部失败  所有接口 500        页面级「数据加载失败」+ 重试

在浏览器层拦截（而不是换掉后端）才能验到真实产物里的组件行为；这与贡献者 mock
后端的效果等价，但跑的是我们的静态导出而不是 dev 服务器。

    python dev/verify_dashboard_states_ui.py
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
DATA = REPO / 'dev' / '.dash-states'
SHOTS = REPO / 'dev' / '.shots-dash-states'
UPSTREAM_PORT = 8001
MANAGER_PORT = 8002
ADMIN_PW = 'dash-states-pass'
UID = 'cl000000-0000-0000-0000-000000000014'


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
                 'success_count': 7, 'err_total': 1, 'credits': 4321}],
                'realm_totals': {'cn': {'total': 1}, 'global': {'total': 0}}})
        elif path == '/v1/models':
            self._json({'object': 'list', 'data': [
                {'id': 'glm-5.2', 'object': 'model', 'created': int(time.time())}]})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        self._json({'ok': True})


def write_auth(auth_dir: Path) -> None:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')

    exp = int(time.time()) + 60 * 86400
    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'iat': int(time.time()), 'exp': exp, 'uid': UID})}.sig"
    (auth_dir / f'workbuddy-{UID}.json').write_text(json.dumps({
        'account': {'uid': UID, 'enterpriseId': 'ent', 'nickname': '演示号'},
        'auth': {'accessToken': token, 'refreshToken': 'r', 'expiresAt': exp,
                 'domain': 'copilot.tencent.com', 'realm': 'cn'},
    }, ensure_ascii=False), encoding='utf-8')


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
    auth_dir = DATA / 'auths'
    auth_dir.mkdir(parents=True)
    write_auth(auth_dir)

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
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}（静态产物：{static_dir.name}/）')
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
            'WB_SHOTS': str(SHOTS),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_dashboard_states.mjs'], cwd=str(REPO),
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

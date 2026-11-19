"""权重与活动任务摘要的浏览器验收（issue #187 / #189）。

    python dev/verify_weight_task_ui.py

两处只有真点一遍才验得到：

  · **权重落盘**：设 2 → 列表按钮标题带 `×2` → 刷新仍在 → 改回 1 后文件里的
    `weight` 键被删掉（默认值不该留噪音）。后端用例只证明接口，证明不了这条链子。
  · **活动任务默认按任务展示**：任务名/状态徽标必须出现，且「原文」页签还能切回
    原始日志 —— 分类本身由 `server/tests/test_task_summary.py` 守着，这里验渲染。

数据落在 dev/.weight-task/（已 gitignore），截图在 dev/.shots-weight-task/。
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
DATA = REPO / 'dev' / '.weight-task'
SHOTS = REPO / 'dev' / '.shots-weight-task'
UPSTREAM_PORT = 7983
MANAGER_PORT = 7984
ADMIN_PW = 'weight-task-pass'
UID = 'wt000000-0000-0000-0000-000000000021'


class Upstream(BaseHTTPRequestHandler):
    """最小 mock 上游：面板启动会探测 /status 与 /v1/models。"""

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
                 'success_count': 3, 'err_total': 0, 'credits': 500}],
                'realm_totals': {'cn': {'total': 1}, 'global': {'total': 0}}})
        elif path == '/v1/models':
            self._json({'object': 'list', 'data': [
                {'id': 'glm-5.2', 'object': 'model', 'created': int(time.time())},
            ]})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        self._json({'ok': True})


def write_auth(auth_dir: Path, uid: str, nickname: str) -> Path:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')

    exp = int(time.time()) + 60 * 86400
    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'iat': int(time.time()), 'exp': exp, 'uid': uid})}.sig"
    path = auth_dir / f'workbuddy-{uid}.json'
    path.write_text(json.dumps({
        'account': {'uid': uid, 'enterpriseId': 'ent', 'nickname': nickname},
        'auth': {'accessToken': token, 'refreshToken': 'r', 'expiresAt': exp,
                 'domain': 'copilot.tencent.com', 'realm': 'cn'},
    }, ensure_ascii=False), encoding='utf-8')
    return path


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
    auth_file = write_auth(auth_dir, UID, '演示号')

    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_AUTH_DIR': str(auth_dir),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'PYTHONUTF8': '1',
    }
    launcher = ('import uvicorn, server.main;'
                f'uvicorn.run(server.main.app, host="127.0.0.1", '
                f'port={MANAGER_PORT}, log_level="warning")')
    proc = subprocess.Popen([sys.executable, '-c', launcher], cwd=str(REPO), env=env)
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}  (admin/{ADMIN_PW})')
    try:
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
            'WB_AUTH_FILE': str(auth_file),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_weight_task.mjs'], cwd=str(REPO),
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

        # ── 服务器侧核对落盘（界面说成功 ≠ 文件里真有）────────────────
        snap = SHOTS / 'auth-after-set.json'
        if not snap.is_file():
            print('✗ 没拿到设置后的账号文件快照', file=sys.stderr)
            return 1
        saved = json.loads(snap.read_text(encoding='utf-8'))
        if saved.get('weight') != 2:
            print(f'✗ 设 2 后文件里的 weight 不是 2：{saved.get("weight")!r}', file=sys.stderr)
            return 1
        final = json.loads(auth_file.read_text(encoding='utf-8'))
        if 'weight' in final:
            print(f'✗ 改回 1 后文件里仍留着 weight={final["weight"]!r}', file=sys.stderr)
            return 1
        print('  ✓ 账号文件：设 2 → 写入 weight=2；改回 1 → 键被删除')
        print('\nALL CHECKS PASSED（含落盘核对）')
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

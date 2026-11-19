"""上游官方统计那一块的界面验收（issue #59）——真服务 + 真浏览器。

用户用的是创建 workbuddy2api 时那把 api_key 直连 7863，面板自己的统计里永远没有它。
现在面板会把上游 `/v1/stats` 的数字摆到用量页上，这个脚本验两件事：

  ① **能取到时**：数字与按模型的明细都渲染出来，且**口径写明**（含直连调用、
     自上游启动累计、与上面的时段筛选无关）——不写清会被读成「今日请求包含直连」；
  ② **取不到时**：明确写出原因（上游没起来 / 镜像太旧 / api_key 不一致），
     **不能显示成 0**——「没有用量」与「取不到」是两回事，后者显示 0 会误导。

    python dev/verify_upstream_stats_ui.py

数据落在 dev/.upstream-stats/（已 gitignore），截图在 dev/.shots-upstream-stats/。
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.upstream-stats'
SHOTS = REPO / 'dev' / '.shots-upstream-stats'
UPSTREAM_PORT = 7961
MANAGER_PORT = 7962
ADMIN_PW = 'upstream-stats-pass'
UID = 'us000000-0000-0000-0000-000000000009'

# 上游 /v1/stats 的形状（照其 MetricsSnapshot 的字段）
STATS = {
    'enabled': True,
    'since': '2026-09-22T04:00:00Z',
    'uptime_sec': 7200,
    'total': {'model': '', 'requests': 1234, 'success': 1200, 'failed': 34,
              'prompt_tokens': 800000, 'completion_tokens': 400000,
              'total_tokens': 1200000, 'credit': 45.6, 'cache_hit_rate': 0.62},
    'models': [
        {'model': 'glm-5.2', 'requests': 900, 'total_tokens': 900000,
         'credit': 30.0, 'cache_hit_rate': 0.7},
        {'model': 'global:gpt-5.6-sol', 'requests': 334, 'total_tokens': 300000,
         'credit': 15.6, 'cache_hit_rate': 0.5},
    ],
}

# 由 --no-stats 切成「上游版本太旧」：/v1/stats 返回 404
NO_STATS = '--no-stats' in sys.argv


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
                 'success_count': 3, 'err_total': 0, 'credits': 500}],
                'realm_totals': {'cn': {'total': 1}, 'global': {'total': 0}}})
        elif path == '/v1/stats':
            if NO_STATS:
                self._json({'error': 'not found'}, 404)
            else:
                self._json(STATS)
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


def write_auth(auth_dir: Path, uid: str, nickname: str) -> None:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')

    exp = int(time.time()) + 60 * 86400
    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'iat': int(time.time()), 'exp': exp, 'uid': uid})}.sig"
    (auth_dir / f'workbuddy-{uid}.json').write_text(json.dumps({
        'account': {'uid': uid, 'enterpriseId': 'ent', 'nickname': nickname},
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
    write_auth(auth_dir, UID, '演示号')

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
    mode = '上游没有 /v1/stats（版本较旧）' if NO_STATS else '上游返回统计'
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}  ({mode})')
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
            'WB_MODE': 'nostats' if NO_STATS else 'ok',
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_upstream_stats.mjs'], cwd=str(REPO),
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

"""模型受限入口与详情验收（模拟上游 + 真服务 + 真浏览器）。

造受限 1 / 2 / 4 个模型的账号，验证「模型受限」/「+1」/「+3」及点击后的完整清单。
运行 python dev/verify_model_limit_ui.py；数据和截图沿用 dev/.model-limit 与 dev/.shots-model-limit。
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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.model-limit'
SHOTS = REPO / 'dev' / '.shots-model-limit'
UPSTREAM_PORT = 7951
MANAGER_PORT = 7952
ADMIN_PW = 'model-limit-pass'

CN = timezone(timedelta(hours=8))


def until(minutes: float) -> str:
    """腾讯/上游形态的恢复时刻（UTC+8 墙钟），必须在未来才被判为「仍受限」。"""
    return datetime.fromtimestamp(time.time() + minutes * 60, CN).strftime('%Y-%m-%d %H:%M:%S')


# 三个账号：受限 1 / 2 / 4 个模型。名字里带 `global:` 前缀的那种最长，
# 正是「徽章里塞不下」的场景，所以四个号里排了它。
ACCOUNTS = [
    {'uid': 'ml-one', 'nickname': '单个号', 'models': [
        ('glm-5.2', '6004 限流')]},
    {'uid': 'ml-two', 'nickname': '两个号', 'models': [
        ('glm-5.2', '6004 限流'), ('deepseek-v4.1-flash', '6004 限流')]},
    {'uid': 'ml-four', 'nickname': '四个号', 'models': [
        ('glm-5.2', '6004 限流'), ('deepseek-v4.1-flash', '6004 限流'),
        ('global:gpt-5.6-sol', '6004 限流'), ('kimi-k2', '11102 该账号没有这个模型')]},
]


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
            pool = []
            for a in ACCOUNTS:
                pool.append({
                    'uid': a['uid'], 'nickname': a['nickname'],
                    'disabled': False, 'cooling': False,
                    'success_count': 5, 'err_total': 0, 'credits': 1000,
                    'rate_limited_models': [
                        {'model': m, 'until': until(9), 'reason': r}
                        for m, r in a['models']
                    ],
                })
            self._json({'healthy': len(pool), 'total': len(pool), 'accounts': pool,
                        'realm_totals': {'cn': {'total': len(pool)},
                                         'global': {'total': 0}}})
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
    for a in ACCOUNTS:
        write_auth(auth_dir, a['uid'], a['nickname'])

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
    launcher = (
        'import uvicorn, server.main;'
        f'uvicorn.run(server.main.app, host="127.0.0.1", port={MANAGER_PORT}, log_level="warning")'
    )
    proc = subprocess.Popen([sys.executable, '-c', launcher], cwd=str(REPO), env=env)
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}  (admin/{ADMIN_PW})')
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
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_model_limit.mjs'], cwd=str(REPO),
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

"""请求日志里提示词缓存标记的界面验收（issue #69）——真服务 + 真浏览器。

报告者想要的是「这条请求到底有没有吃到缓存」。数据的来源是上游 usage 里的
`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` / `prompt_cache_write_tokens`，
而**老上游不返回这三个字段**——所以「没命中」和「上游压根没给」必须长得不一样：

    · 命中为主   → 绿色的「缓存 98%」
    · 完全没命中 → 琥珀色的「未命中」（用户就是靠这条发现缓存没生效）
    · 上游没给   → 什么都不显示（显示 0% 会让人以为缓存从来没生效过）

这个脚本造三条这样的日志，然后逐行核对标记落在对的那一行上：

    python dev/verify_cache_log_ui.py

种子走的是真实的写入路径 `gateway._record(usage=...)`（不走 HTTP，其余都是真的）：
上游字段名到库列的对应就发生在这一步，绕过它等于把要验的那一环跳过。
三条日志的 IP 不同，浏览器断言据此把「哪一行」和「该显示什么」对上。

数据落在 dev/.cache-log/（已 gitignore），截图在 dev/.shots-cache-log/。
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
DATA = REPO / 'dev' / '.cache-log'
SHOTS = REPO / 'dev' / '.shots-cache-log'
UPSTREAM_PORT = 7991
MANAGER_PORT = 7992
ADMIN_PW = 'cache-log-pass'
UID = 'cl000000-0000-0000-0000-000000000012'


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
        elif path == '/v1/models':
            self._json({'object': 'list', 'data': [
                {'id': 'glm-5.2', 'object': 'model', 'created': int(time.time())},
                {'id': 'deepseek-v4.1-flash', 'object': 'model', 'created': int(time.time())},
                {'id': 'global:gpt-5.6-sol', 'object': 'model', 'created': int(time.time())},
            ]})
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


def seed_logs() -> None:
    """造三条请求日志，覆盖三种形态（issue #69）：

      · 命中为主（7808 命中 / 192 未命中）→ 界面显示「缓存 98%」
      · 完全没命中（0 / 900）→ 显示「未命中」（这是用户要抓的那种情况）
      · 老上游没给这三个字段 → 什么都不显示（而不是显示 0%）

    走真实的 `gateway._record(usage=...)`，只把 HTTP 那一段省掉：
    「上游 usage 字段名 → 库列」的对应就在这里，直接按库列名造数等于跳过它。
    IP 三条各不相同，供浏览器断言定位到具体某一行。
    """
    sys.path.insert(0, str(REPO))
    os.environ.update({'WB_DB': str(DATA / 'manager.db'),
                       'WB_AUTH_DIR': str(DATA / 'auths')})
    from server import config, db
    from server.routers import gateway
    config.DB_PATH = DATA / 'manager.db'
    config.AUTH_DIR = DATA / 'auths'
    db._conn = None
    db.connect()
    # (说明, IP, 上游那份 usage)
    rows = [
        ('命中为主', '10.0.0.1', {'prompt_tokens': 8000, 'completion_tokens': 100,
                                'credit': 0.5,
                                'prompt_cache_hit_tokens': 7808,
                                'prompt_cache_miss_tokens': 192}),
        ('完全没命中', '10.0.0.2', {'prompt_tokens': 8000, 'completion_tokens': 100,
                                 'credit': 0.5,
                                 'prompt_cache_hit_tokens': 0,
                                 'prompt_cache_miss_tokens': 900}),
        ('老上游没给', '10.0.0.3', {'prompt_tokens': 8000, 'completion_tokens': 100,
                                 'credit': 0.5}),
    ]
    for _label, ip, usage in rows:
        gateway._record(None, ip, 'glm-5.2', '', 200,
                        usage['prompt_tokens'], usage['completion_tokens'],
                        1200, 'seed', None, True, first_token=300, usage=usage)
    # 账号列（PR #70）走的是**采集上游容器日志再按时间回填**这条路：
    # 这里直接喂三条「已解析好的上游日志条目」，等价于采集器解析后的入参。
    # 三条都在匹配窗口内，各自认领一行（回填后立刻置为已填，不会重复认领）。
    now = db.query_one('SELECT ts FROM request_logs ORDER BY id DESC LIMIT 1')['ts']
    filled = db.attach_request_accounts([
        {'ts': now, 'model': 'glm-5.2', 'account': '张叔叔(299e342b)'},
        {'ts': now, 'model': 'glm-5.2', 'account': 'Moonquakes(3a3a19b1)'},
        {'ts': now, 'model': 'glm-5.2', 'account': '备用号(11112222)'},
    ])
    print(f'已造三条请求日志（命中 / 未命中 / 没给），账号回填 {filled} 条')
    db._conn.close()
    db._conn = None


def main() -> int:
    for d in (DATA, SHOTS):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    auth_dir = DATA / 'auths'
    auth_dir.mkdir(parents=True)
    write_auth(auth_dir, UID, '演示号')
    seed_logs()

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
        r = subprocess.run(['node', 'dev/verify_cache_log.mjs'], cwd=str(REPO),
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

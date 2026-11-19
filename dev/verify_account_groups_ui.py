"""账号分组（多账号池）的界面验收——真服务 + 真浏览器。

账号页的分组是本功能的日常入口，而下列三条**单测看不见**、也正是最容易出错的：

  1. 每个分组的列表读的是**它自己的账号目录**（互相看不见对方组的账号）；
  2. 「在线」徽章来自**该分组自己的上游实例**——若问错实例，另一组的账号会
     显示「未加载」，而这在界面上和真的没加载长得一模一样；
  3. 「移动到分组」把账号文件真的搬到了目标目录（源列表里消失、目标列表里出现）。

脚本走一遍用户路径：默认分组 → 添加分组（只填名称）→「设置 → 上游」补实例参数 →
移动到分组 → 没配目录的分组的提示。截图落在 dev/.shots-account-groups/（随 PR 一并提交）。

    python dev/verify_account_groups_ui.py
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
DATA = REPO / 'dev' / '.account-groups'
SHOTS = REPO / 'dev' / '.shots-account-groups'
DEFAULT_UP_PORT = 8021      # 「默认分组」的假上游
GROUP_UP_PORT = 8022        # 新建的「甲组」的假上游
MANAGER_PORT = 8023
ADMIN_PW = 'account-groups-pass'
GROUP_KEY = 'GROUP-UPSTREAM-KEY-xyz789'
UID_D = 'ag000000-0000-0000-0000-0000000000d1'
UID_G = 'ag000000-0000-0000-0000-0000000000g1'


def make_upstream(uid: str, nickname: str):
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
                self._json({'healthy': 1, 'total': 1, 'accounts': [
                    {'uid': uid, 'nickname': nickname, 'disabled': False,
                     'cooling': False, 'success_count': 3, 'err_total': 0,
                     'credits': 500}],
                    'realm_totals': {'cn': {'total': 1}, 'global': {'total': 0}}})
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


def write_auth(auth_dir: Path, uid: str, nickname: str) -> None:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')

    exp = int(time.time()) + 60 * 86400
    token = (f"{b64({'alg': 'none', 'typ': 'JWT'})}."
             f"{b64({'iat': int(time.time()), 'exp': exp, 'uid': uid})}.sig")
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
    # 控制台编码兜底：脚本输出里有 ✓ 之类的字符，GBK 控制台直接 print 会崩
    try:
        sys.stdout.reconfigure(errors='replace')
    except Exception:  # noqa: BLE001
        pass

    for d in (DATA, SHOTS):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    default_auths = DATA / 'default-auths'
    group_auths = DATA / 'group-auths'
    default_auths.mkdir(parents=True)
    group_auths.mkdir(parents=True)
    write_auth(default_auths, UID_D, '默认号')
    write_auth(group_auths, UID_G, '甲组号')

    up1 = ThreadingHTTPServer(('127.0.0.1', DEFAULT_UP_PORT),
                              make_upstream(UID_D, '默认号'))
    up2 = ThreadingHTTPServer(('127.0.0.1', GROUP_UP_PORT),
                              make_upstream(UID_G, '甲组号'))
    threading.Thread(target=up1.serve_forever, daemon=True).start()
    threading.Thread(target=up2.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{DEFAULT_UP_PORT}',
        'WB_AUTH_DIR': str(default_auths),
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
            except Exception:  # noqa: BLE001
                time.sleep(0.5)

        node_env = {
            **os.environ,
            'WB_BASE': f'http://127.0.0.1:{MANAGER_PORT}',
            'WB_PASS': ADMIN_PW,
            'WB_DEFAULT_URL': f'http://127.0.0.1:{DEFAULT_UP_PORT}',
            'WB_DEFAULT_DIR': str(default_auths),
            'WB_GROUP_URL': f'http://127.0.0.1:{GROUP_UP_PORT}',
            'WB_GROUP_KEY': GROUP_KEY,
            'WB_GROUP_DIR': str(group_auths),
            'WB_SHOTS': str(SHOTS),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_account_groups.mjs'], cwd=str(REPO),
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
        up1.shutdown()
        up2.shutdown()


if __name__ == '__main__':
    sys.exit(main())

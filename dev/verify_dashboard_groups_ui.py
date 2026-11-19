"""首页统计「覆盖全部分组」的运行时验收（PR #98，跑在静态导出产物上）。

issue #94 问题 2：把账号「移动到分组」之后，首页的账号总数 / 积分 / 反代上游面板
只反映默认分组——数字看起来完全正常，所以没人会怀疑。PR #98 改成逐分组取数再合并。

贡献者的验收跑在 `next dev` 上（他自己的沙箱跑不了 `build:export`），这个脚本补上
生产形态：起真面板（`WB_STATIC_DIR=web/out`）+ 两套假上游 + 两个账号目录，
**用三个互不相等的数**做分辨：

    默认分组 2 个号 + 乙组 3 个号
      · 只报默认分组（修前）→ 2
      · 全部聚合（正确）    → 5
      · 把「只转发」的分组也算进分母 → 范围说明会写成 3

断言覆盖：聚合数 / 范围说明 / 反代上游按分组分块（不求和）/ 单组失败时如实说明
「有几个分组没取到」且不升级成整页错误。

    python dev/verify_dashboard_groups_ui.py
"""
from __future__ import annotations

import base64
import http.cookiejar
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
DATA = REPO / 'dev' / '.dash-groups'
SHOTS = REPO / 'dev' / '.shots-dash-groups'
DEFAULT_UP_PORT = 8031
GROUP_UP_PORT = 8032
MANAGER_PORT = 8033
ADMIN_PW = 'dash-groups-pass'

UID_A1 = 'dg000000-0000-0000-0000-0000000000a1'
UID_A2 = 'dg000000-0000-0000-0000-0000000000a2'
UID_B1 = 'dg000000-0000-0000-0000-0000000000b1'
UID_B2 = 'dg000000-0000-0000-0000-0000000000b2'
UID_B3 = 'dg000000-0000-0000-0000-0000000000b3'


def make_upstream(uids: list[str], names: list[str], credits: int):
    accounts = [
        {'uid': u, 'nickname': n, 'disabled': False, 'cooling': False,
         'success_count': 5, 'err_total': 0, 'credits': credits}
        for u, n in zip(uids, names)
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
                self._json({
                    'healthy': len(uids), 'total': len(uids), 'accounts': accounts,
                    'cooling': 0, 'disabled': 0, 'in_flight_full': 0,
                    'redis_mode': 'noop', 'sticky_sessions': 0, 'connected': True,
                    'realm_totals': {
                        'cn': {'total': len(uids), 'healthy': len(uids), 'cooling': 0,
                               'disabled': 0, 'in_flight_full': 0},
                        'global': {'total': 0, 'healthy': 0, 'cooling': 0,
                                   'disabled': 0, 'in_flight_full': 0},
                    },
                })
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

    return Upstream


def write_auth(auth_dir: Path, uid: str, nickname: str, credits: int) -> None:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')

    exp = int(time.time()) + 60 * 86400
    token = (f"{b64({'alg': 'none', 'typ': 'JWT'})}."
             f"{b64({'iat': int(time.time()), 'exp': exp, 'uid': uid})}.sig")
    (auth_dir / f'workbuddy-{uid}.json').write_text(json.dumps({
        'account': {'uid': uid, 'enterpriseId': 'ent', 'nickname': nickname},
        'auth': {'accessToken': token, 'refreshToken': 'r', 'expiresAt': exp,
                 'domain': 'copilot.tencent.com', 'realm': 'cn'},
        'credits': credits,
    }, ensure_ascii=False), encoding='utf-8')


def _playwright_entry() -> str | None:
    for base in (Path(tempfile.gettempdir()), Path(os.environ.get('TEMP') or '')):
        p = base / 'wb-i18n-verify' / 'node_modules' / 'playwright-core' / 'index.js'
        if p.is_file():
            return str(p)
    return None


def _login_and_create_group(base: str, name: str, upstream_url: str, auth_dir: Path) -> int:
    """建一个指向第二套实例的分组，返回它的 id。"""
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.open(urllib.request.Request(
        f'{base}/api/login',
        data=json.dumps({'username': 'admin', 'password': ADMIN_PW}).encode(),
        headers={'content-type': 'application/json'}))
    req = urllib.request.Request(
        f'{base}/api/upstreams',
        data=json.dumps({'name': name, 'base_url': upstream_url,
                         'api_key': 'GROUP-KEY', 'auth_dir': str(auth_dir)}).encode(),
        headers={'content-type': 'application/json'})
    out = json.loads(op.open(req).read())
    return int(out['id'])


def main() -> int:
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
    write_auth(default_auths, UID_A1, '默认一号', 1000)
    write_auth(default_auths, UID_A2, '默认二号', 2000)
    # 健康快照只画前 9 条：这里多铺 6 个号（合计 11），用来验「查看全部账号」入口
    for i in range(3, 9):
        write_auth(default_auths, f'dg000000-0000-0000-0000-0000000000a{i}', f'默认{i}号', 1000)
    write_auth(group_auths, UID_B1, '乙组一号', 3000)
    write_auth(group_auths, UID_B2, '乙组二号', 4000)
    write_auth(group_auths, UID_B3, '乙组三号', 5000)

    default_uids = [UID_A1, UID_A2] + [f'dg000000-0000-0000-0000-0000000000a{i}' for i in range(3, 9)]
    up1 = ThreadingHTTPServer(('127.0.0.1', DEFAULT_UP_PORT),
                              make_upstream(default_uids,
                                            ['默认一号', '默认二号'] + [f'默认{i}号' for i in range(3, 9)],
                                            1000))
    up2 = ThreadingHTTPServer(('127.0.0.1', GROUP_UP_PORT),
                              make_upstream([UID_B1, UID_B2, UID_B3],
                                            ['乙组一号', '乙组二号', '乙组三号'], 3000))
    for s in (up1, up2):
        threading.Thread(target=s.serve_forever, daemon=True).start()

    static_dir = REPO / 'web' / 'out'
    if not (static_dir / 'index.html').is_file():
        print(f'缺少前端产物 {static_dir}，先跑 npm run build:export', file=sys.stderr)
        return 2

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{DEFAULT_UP_PORT}',
        'WB_AUTH_DIR': str(default_auths),
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
    base = f'http://127.0.0.1:{MANAGER_PORT}'
    print(f'管理端: {base}（静态产物：{static_dir.name}/，默认分组 8 个号 + 乙组 3 个号）')
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f'{base}/api/healthz', timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        gid = _login_and_create_group(base, '乙组', f'http://127.0.0.1:{GROUP_UP_PORT}',
                                      group_auths)
        print(f'已建分组「乙组」 id={gid}')
        node_env = {
            **os.environ,
            'WB_BASE': base,
            'WB_PASS': ADMIN_PW,
            'WB_SHOTS': str(SHOTS),
            'WB_GROUP_ID': str(gid),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_dashboard_groups.mjs'], cwd=str(REPO),
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

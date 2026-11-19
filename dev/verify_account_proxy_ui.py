"""「线路」绑定的界面验收——真服务 + 真浏览器（PR #119）。

三条只有真人点一遍才能确认的事，都在这个脚本里：

  1. **上游没配 `proxies` 时不出现「线路」**。此时下拉里只有「直连」一项，显示出来
     只会让人以为能选却选不动（维护者复核时提的就是这条）。反方向同样要成立：
     配了线路，列表和「添加账号」弹窗里都必须出现。
  2. **代理地址不外泄**。线路表里有的是 `http://user:pass@host:port`，界面只能出现
     线路**名**；页面上出现任一 URL 片段即判失败。
  3. **下拉是真能改的**。选一条线路 → 落盘到账号文件的 `proxy` 字段 → 选回「直连」
     能清掉。只改内存不回写的「假下拉」在这三条里过不去。

线路表读的是 workbuddy2api 的 config.json（`WB_UPSTREAM_CONFIG`），每次请求现读，
所以脚本在两个阶段之间直接改这个文件，不用重启面板。

    python dev/verify_account_proxy_ui.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.account-proxy'
SHOTS = REPO / 'dev' / '.shots-account-proxy'
UP_PORT = 8031
MANAGER_PORT = 8032
ADMIN_PW = 'account-proxy-pass'
UID_P = 'ap000000-0000-0000-0000-0000000000p1'
FILE_P = f'workbuddy-{UID_P}.json'
ROUTE_A = 'hk'
ROUTE_B = 'jp'
# 带凭据的代理地址：只该出现在服务端配置里，界面上一个字都不能有
PROXY_URL_A = 'http://proxyuser:proxypass@127.0.0.1:7890'
PROXY_URL_B = 'http://127.0.0.1:7891'


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
    """按上游真实的账号文件格式写：字段名是 camelCase，昵称在 account 里。

    写错字段名（比如 access_token）不会报错，只会让面板把它当成「缺少
    accessToken」的坏文件——界面上就是「未命名 / 未加载」，断言跟着全歪。
    """
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
    root = Path(os.environ.get('LOCALAPPDATA', '')) / 'ms-playwright'
    if not root.is_dir():
        return None
    for d in sorted(root.iterdir(), reverse=True):
        if d.name.startswith('chromium-') and 'headless_shell' not in d.name:
            for cand in (d / 'chrome-win64' / 'chrome.exe',):
                if cand.is_file():
                    return str(cand)
    return None


def write_upstream_config(path: Path, proxies: dict | None) -> None:
    """线路表就是 workbuddy2api config.json 的 `proxies`；不给就是「没配过」。"""
    cfg = {'api_key': 'PROXY-HARNESS-KEY'}
    if proxies:
        cfg['proxies'] = proxies
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')


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
    shots = SHOTS
    shots.mkdir(parents=True)
    write_auth(auth_dir, UID_P, '代理号')
    config_path = DATA / 'config.json'
    # 阶段 A：上游没配过线路
    write_upstream_config(config_path, None)

    up = ThreadingHTTPServer(('127.0.0.1', UP_PORT), make_upstream(UID_P, '代理号'))
    threading.Thread(target=up.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UP_PORT}',
        'WB_AUTH_DIR': str(auth_dir),
        'WB_UPSTREAM_CONFIG': str(config_path),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_STATIC_DIR': str(REPO / 'web' / 'out'),
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
    print(f'管理端: {base}（静态产物：web/out/，线路表：{config_path.name}）')
    try:
        import urllib.request
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
            'WB_CONFIG': str(config_path),
            'WB_AUTH_DIR': str(auth_dir),
            'WB_UID': UID_P,
            'WB_ROUTE_A': ROUTE_A,
            'WB_ROUTE_B': ROUTE_B,
            'WB_PROXY_URL_A': PROXY_URL_A,
            'WB_PROXY_URL_B': PROXY_URL_B,
            'WB_SHOTS': str(shots),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_account_proxy.mjs'], cwd=str(REPO),
                           env=node_env, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        print(r.stdout or '')
        if r.stderr:
            print(r.stderr[-2000:], file=sys.stderr)
        if r.returncode != 0 or 'ALL CHECKS PASSED' not in (r.stdout or ''):
            print('✗ 浏览器侧没有报 ALL CHECKS PASSED —— 不能当作通过', file=sys.stderr)
            return 1

        # ── 落盘核对：浏览器点选的那一下，真的写进了账号文件 ──────────────
        bound = json.loads((shots / 'bound-auth.json').read_text(encoding='utf-8'))
        if bound.get('proxy') != ROUTE_A:
            print(f'✗ 绑定后快照里 proxy={bound.get("proxy")!r}，应为 {ROUTE_A!r}', file=sys.stderr)
            return 1
        print(f'  ✓  绑定时账号文件的 proxy 字段 = {ROUTE_A}（浏览器那一下真的落盘了）')
        live = json.loads((auth_dir / FILE_P).read_text(encoding='utf-8'))
        if live.get('proxy'):
            print(f'✗ 改回「直连」后文件里仍有 proxy={live["proxy"]!r}', file=sys.stderr)
            return 1
        print('  ✓  改回「直连」后账号文件里的 proxy 字段已清掉')
        if not (bound.get('auth') or {}).get('accessToken'):
            print('✗ 改线路把账号凭据弄丢了', file=sys.stderr)
            return 1
        live_token = (live.get('auth') or {}).get('accessToken')
        if live_token != (bound.get('auth') or {}).get('accessToken'):
            print('✗ 改线路顺带改动了 accessToken', file=sys.stderr)
            return 1
        print('  ✓  改线路没有动账号凭据（account 与 auth 其余字段原样）')
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        up.shutdown()


if __name__ == '__main__':
    sys.exit(main())

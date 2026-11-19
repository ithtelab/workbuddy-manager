"""本轮新增控件的浏览器验收（#78 积分耗尽冷却 / #173 兜底模型 / #191 不限制版本密钥）。

为什么单独跑：这三个改动都是「后端语义 + 前端控件」成对的，单测只到接口层，
而踩过的坑都在渲染与提交路径上 ——
  · 冷却时长填 `0` 时，前端必须**当场**报格式错误（不是等保存后被上游拒绝）；
  · 兜底那条在映射表里必须显示成「兜底模型」，否则用户看不懂那个 `*`；
  · 新建密钥勾了「不限制」却仍被页面版本覆盖（#191 的原病），只有真点一次才发现。

    python dev/verify_new_controls_ui.py

数据落在 dev/.new-controls/（已 gitignore），截图在 dev/.shots-new-controls/。
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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.new-controls'
SHOTS = REPO / 'dev' / '.shots-new-controls'
UPSTREAM_PORT = 7981
MANAGER_PORT = 7982
ADMIN_PW = 'new-controls-pass'
UID = 'nc000000-0000-0000-0000-000000000020'


class Upstream(BaseHTTPRequestHandler):
    """最小 mock 上游：面板启动会探测，模型映射的下拉框要有个模型可选。"""

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
            ]})
        elif path == '/v1/models/catalog':
            self._json({'data': []})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        path = self.path.split('?')[0]
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        # 活动补偿：网页「用量管理」那两个端点，信封 {code, msg, data}。
        # 面板会把 billing 基址指到本 mock（见 launcher），所以走不到真实腾讯。
        if path == '/billing/meter/compensation-status':
            return self._json({'code': 0, 'msg': 'ok', 'data': {
                'claimed': False, 'credit_num': 500, 'button_text': '领取 500 积分',
                'title': '活动补偿',
            }})
        if path == '/billing/meter/claim-compensation':
            return self._json({'code': 0, 'msg': 'ok', 'data': {'credit': 500}})
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


def seed_upstream_config() -> None:
    """写一份干净的上游 config.json：冷却段带 soft_rate（对照组）。"""
    sys.path.insert(0, str(REPO))
    os.environ.update({'WB_DB': str(DATA / 'manager.db'),
                       'WB_AUTH_DIR': str(DATA / 'auths')})
    cfg = DATA / 'upstream' / 'config.json'
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        'api_key': 'seed-key',
        'cooldown': {'soft_rate': '600s', 'soft_rate_max': '2h'},
    }, ensure_ascii=False, indent=2), encoding='utf-8')


def _playwright_entry() -> str | None:
    for base in (Path(os.environ.get('TEMP') or ''), Path('/tmp')):
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
    seed_upstream_config()

    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_UPSTREAM_DIR': str(DATA / 'upstream'),
        # 面板读的是 WB_UPSTREAM_CONFIG（不是 WB_UPSTREAM_DIR/config.json）——
        # 只设后者会让设置页拿到 available=false，所有输入框 disabled（踩过）。
        'WB_UPSTREAM_CONFIG': str(DATA / 'upstream' / 'config.json'),
        'WB_AUTH_DIR': str(auth_dir),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'PYTHONUTF8': '1',
    }
    # 把 billing 基址指向 mock：补偿领取会打 {billing_base}/billing/meter/claim-compensation，
    # 不重定向就会拿着假 token 打到真实腾讯（吃 401，验收变成在测网络）。
    # 指向 mock 之后整条链路（面板 → 腾讯接口 → 落库/回执）都在本机闭环。
    launcher = ('import server.config as _c;'
                f'_c.TENCENT_BILLING_BASE = "http://127.0.0.1:{UPSTREAM_PORT}";'
                'import uvicorn, server.main;'
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
            'WB_CONFIG': str(DATA / 'upstream' / 'config.json'),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_new_controls.mjs'], cwd=str(REPO),
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
        except Exception:
            proc.kill()


if __name__ == '__main__':
    raise SystemExit(main())

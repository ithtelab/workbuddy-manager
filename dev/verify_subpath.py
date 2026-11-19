"""子路径部署的端到端验收（PR #72）——启动真服务，按两种反代形态各打一遍。

反向代理把面板挂在 `/wm` 这类前缀下时有两种写法：

  · **保留前缀**：`proxy_pass http://127.0.0.1:7864;`（不带尾斜杠）
    → 服务端收到 `/wm/api/healthz`
  · **剥掉前缀**：`proxy_pass http://127.0.0.1:7864/;`（带尾斜杠）
    → 服务端收到 `/api/healthz`

改动前只支持后者；前者表现为「页面能打开、点什么都 404」。这个脚本用
`WB_BASE_PATH=/wm` 起一个真服务（真静态产物、真路由），两种形态都打一遍：

    python dev/verify_subpath.py

数据落在 dev/.subpath/（已 gitignore）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
DATA = REPO / 'dev' / '.subpath'
PORT = 7993
PREFIX = '/wm'
ADMIN_PW = 'subpath-pass'


def _req(path: str, *, method: str = 'GET', headers: dict | None = None,
         body: bytes | None = None, follow: bool = False):
    """返回 (状态码, 响应头, 正文)。**不跟随跳转**——302 的 Location 正是要验的东西。"""
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}', method=method,
                                 data=body, headers=headers or {})
    opener = urllib.request.build_opener(
        urllib.request.HTTPRedirectHandler() if follow else _NoRedirect())
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # noqa: D102
        return None


def _header(hdr: dict, name: str) -> str | None:
    """按名字取响应头（大小写不敏感——Starlette 发的是小写 `location`）。"""
    for k, v in hdr.items():
        if k.lower() == name.lower():
            return v
    return None


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)
    users = DATA / 'users.json'
    users.write_text(json.dumps({
        'secret': 'S',
        'users': [{'username': 'admin', 'role': 'admin',
                   'pwd_hash': __import__('server.security', fromlist=['x']).make_hash(ADMIN_PW)}],
        'api_keys': [],
    }), encoding='utf-8')

    static_dir = REPO / 'web' / 'out'
    if not (static_dir / 'index.html').is_file():
        print(f'缺少前端产物 {static_dir}，先跑 npm run build:export', file=sys.stderr)
        return 2

    env = {
        **os.environ,
        'WB_BASE_PATH': PREFIX,
        'WB_STATIC_DIR': str(static_dir),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(users),
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'PYTHONUTF8': '1',
    }
    (DATA / 'auths').mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, '-c',
         'import uvicorn, server.main;'
         f'uvicorn.run(server.main.app, host="127.0.0.1", port={PORT}, log_level="warning")'],
        cwd=str(REPO), env=env)

    findings: list[str] = []

    def check(ok: bool, label: str, detail: str = '') -> None:
        print(f'  {"✓" if ok else "✗"}  {label}' + (f'\n       {detail}' if detail else ''))
        if not ok:
            findings.append(label + (f': {detail}' if detail else ''))

    try:
        ready = False
        for _ in range(80):
            try:
                code, _, _ = _req('/api/healthz')
                if code == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        if not ready:
            print('服务没起来', file=sys.stderr)
            return 2
        print(f'已启动（WB_BASE_PATH={PREFIX}）')

        # ① 两种反代形态下 API 都要通
        code_a, _, _ = _req(f'{PREFIX}/api/healthz')
        check(code_a == 200, '保留前缀的请求能通（/wm/api/healthz）', f'状态码={code_a}')
        code_b, _, _ = _req('/api/healthz')
        check(code_b == 200, '已剥前缀的请求照旧能通（/api/healthz）', f'状态码={code_b}')

        # ② 前端页面与静态资源
        code, hdr, body = _req(f'{PREFIX}/login')
        ctype = (hdr.get('content-type') or '').lower()
        check(code == 200 and 'text/html' in ctype,
              '子路径下能取到页面（/wm/login）', f'状态码={code} type={ctype}')
        html = body.decode('utf-8', 'replace')
        # 产物是否带前缀取决于**构建时**有没有设 NEXT_PUBLIC_BASE_PATH：容器部署
        # 靠 build-arg BASE_PATH 传进去。这里只在明确用带前缀的产物跑时才断言
        # （WB_EXPECT_PREFIXED_ASSETS=1），否则这条会因为「本地产物没带前缀」而
        # 假红——那验的是构建参数，不是这次要验的中间件。
        if os.environ.get('WB_EXPECT_PREFIXED_ASSETS') == '1':
            sample = [s for s in html.split(chr(34)) if s.startswith(PREFIX)]
            check(bool(sample),
                  '带前缀构建的产物里，静态资源地址带前缀（否则页面白屏）',
                  f'样例：{sample[:2]}')
        code, _, _ = _req(f'{PREFIX}/_next/static/chunks/not-exist.js')
        check(code == 404, '前缀下的静态资源路径不会穿透到其它路由', f'状态码={code}')

        # ③ RSC 兜底 302：Location 要带前缀，且**只带一层**
        doc = {'Accept': 'text/html,application/xhtml+xml'}
        code, hdr, _ = _req(f'{PREFIX}/dashboard/index.txt', headers=doc)
        check(code == 302 and _header(hdr, 'location') == f'{PREFIX}/dashboard',
              '保留前缀时：RSC 兜底 302 指回带前缀的页面',
              f'状态码={code} Location={_header(hdr, "location")}')
        code, hdr, _ = _req('/dashboard/index.txt', headers=doc)
        check(code == 302 and _header(hdr, 'location') == f'{PREFIX}/dashboard',
              '已剥前缀时：同一个 302 也带前缀（不能让浏览器跑到域名根）',
              f'状态码={code} Location={_header(hdr, "location")}')

        # ④ 只是「前缀开头相似」的路径不能被误剥
        code, _, _ = _req('/wmx/api/healthz')
        check(code == 404, '和前缀只是长得像的路径不被误剥（/wmx/...）', f'状态码={code}')

        # ⑤ 网关也挂在前缀下（接入地址是 <前缀>/v1）。
        # 无密钥打一次：401/403 = 路由匹配上了、进到鉴权；404 = 前缀没剥掉。
        code, _, _ = _req(f'{PREFIX}/v1/chat/completions', method='POST',
                          body=b'{}', headers={'content-type': 'application/json'})
        check(code in (401, 403),
              '子路径下的网关能到达鉴权环节（POST /wm/v1/chat/completions）',
              f'状态码={code}（404 说明路由没匹配上）')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print('\n=== 结果 ===')
    if findings:
        print(f'✗ {len(findings)} 项未通过：')
        for f in findings:
            print('   - ' + f)
        return 1
    print('ALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())

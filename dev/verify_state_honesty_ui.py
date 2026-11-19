"""「取不到 ≠ 没有」的运行时验收：安全页（PR #93）、任务记录页（PR #97）、
密钥页（PR #100），以及底栏的分组与遮挡（批次 4）。

前三页各自修的都是同一件事——取数失败被静默丢掉，界面照常渲染初始值，于是
**「不知道」被显示成「确实没有」**：

  · 安全页：开关停在「关闭」、规则表写「暂无规则」、日志写「暂无访问记录」。
    管理员据此会判断「拦截没生效 / 没人被拦过」，而这两句话没有任何依据。
  · 任务记录页：两块正片写「暂无签到记录 / 暂无自动任务记录」，页脚还挂着「共 0 条」。
  · 密钥页：列表写「暂无 API 密钥」。密钥是**凭据**，这句读起来是「我的密钥被删了」，
    用户会顺手点旁边的「新建密钥」重建一个 —— 于是建出重复密钥，而重复密钥会让
    「按密钥限额 / 按密钥统计用量」全都对不上。

底栏那一批是另一类：它不涉及取数，错的是**信息架构**——13 个入口平铺、分隔线不带
组名、手机端连分隔线都没有，引导气泡还挂在底栏正上方（底栏能被拖到页面中部，于是
正好压住正文）。同样不会报错，只能靠真浏览器看。

设置页的子路由（批次 4 的后半）也是信息架构，但多一层**状态**：7 个 Tab 变成
`/settings/<tab>` 之后，切 Tab 就是一次路由跳转，而 App Router 会重挂载子路由的
page。取数与表单状态因此必须留在**不重挂载**的 `layout.tsx` 上——一旦掉回 page，
每切一次 Tab 都会重新拉一次配置、闪一次骨架，还会丢掉没保存的编辑。这条在界面上
只能靠**请求次数**证明（骨架可能只闪几毫秒），所以 `.mjs` 里那一段用的是 `hits`
计数，并配一条正对照：真刷新**必须**重取。

底栏「11 → 8」是同一批的最后一件：被吸收的三页（任务记录 / 红包 / 聊天测试台）
不再挂在底栏上，改成在它们**所属的那一页**里用页内二级导航切换（路径没变，所以
旧书签与已分享的链接照常能用）。这一段要防的错法是**成对**的——「入口少了一个」
和「那一页没了」在用户眼里长得一模一样，都只是「找不到了」，所以 `.mjs` 里既数
「底栏只剩 8 个页面入口」，又逐个证明那三页都还在、都点得到、高亮跟着走。

三位贡献者的验收都跑在 `next dev` 上（沙箱跑不了 `build:export`）；这个脚本补上
生产形态（静态导出产物），断言的是**用户看得见的东西**：哪些话说出来了、哪些话
不许说。

    python dev/verify_state_honesty_ui.py

⚠️ 本机跑不了 `build:export`（写 `.next/` 被沙箱拦）时，这个脚本会因缺少
`web/out` 直接退出。此时把 `dev/verify_state_honesty.mjs` 指向 `next dev` 的端口
+ 一个真后端（`WB_BASE`/`WB_PASS`/`WB_SHOTS`/`WB_PLAYWRIGHT` 四个环境变量）照样
能跑断言——**分工可以明说**：断言脚本在 dev 上验，生产形态由维护者跑。
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
DATA = REPO / 'dev' / '.state-honesty'
SHOTS = REPO / 'dev' / '.shots-state-honesty'
UPSTREAM_PORT = 8041
MANAGER_PORT = 8042
ADMIN_PW = 'state-honesty-pass'
UID = 'sh000000-0000-0000-0000-000000000001'


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
            self._json({'healthy': 1, 'total': 1, 'cooling': 0, 'disabled': 0,
                        'in_flight_full': 0, 'redis_mode': 'noop', 'sticky_sessions': 0,
                        'connected': True,
                        'accounts': [{'uid': UID, 'nickname': '演示号', 'disabled': False,
                                      'cooling': False, 'success_count': 3, 'err_total': 0,
                                      'credits': 1000}],
                        'realm_totals': {'cn': {'total': 1}, 'global': {'total': 0}}})
        elif path == '/v1/models':
            # **不能返回空清单**：模型中心那组断言里，「恢复后」这一条要能区分
            # 「页面真的把列表渲染出来了」与「页面仍然什么都没有」。目录为空时
            # 恢复后显示「暂无模型」是**如实**的，断言就退化成只看错误态消没消失，
            # 判别力全丢了。这里给两条国内版条目（管理端的回退路径按 `cn:` 前缀
            # 挑版本，见 server/services/modelcatalog.py 的 `_belongs`）。
            self._json({'object': 'list', 'data': [
                {'id': 'cn:glm-5.2', 'context_length': 131072, 'max_output_tokens': 8192},
                {'id': 'cn:hunyuan-turbos', 'context_length': 262144,
                 'max_output_tokens': 16384},
            ]})
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
    token = (f"{b64({'alg': 'none', 'typ': 'JWT'})}."
             f"{b64({'iat': int(time.time()), 'exp': exp, 'uid': UID})}.sig")
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
    try:
        sys.stdout.reconfigure(errors='replace')
    except Exception:  # noqa: BLE001
        pass
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
    base = f'http://127.0.0.1:{MANAGER_PORT}'
    print(f'管理端: {base}（静态产物：{static_dir.name}/）')
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f'{base}/api/healthz', timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        # 两端各留几条记录：两个面板的页脚数字不同（2 / 1），才分得清
        # 「这一块收起了页脚」与「本来就 0 条」。
        import sqlite3
        con = sqlite3.connect(str(DATA / 'manager.db'))
        now = int(time.time())
        con.executemany(
            'INSERT INTO checkin_logs(ts, uid, nickname, source, kind, success, code, message)'
            ' VALUES(?,?,?,?,?,?,?,?)',
            [(now - 60, UID, '演示号', 'manual', 'checkin', 1, 0, '签到成功'),
             (now - 30, UID, '演示号', 'manual', 'checkin', 1, 0, '签到成功')])
        con.execute(
            'INSERT INTO task_logs(ts, uid, kind, level, credits, message, dedup_key)'
            ' VALUES(?,?,?,?,?,?,?)',
            (now - 45, UID, 'travel', 'ok', 100, '旅行领奖 +100', 'seed-1'))
        con.commit()
        con.close()
        node_env = {
            **os.environ,
            'WB_BASE': base,
            'WB_PASS': ADMIN_PW,
            'WB_SHOTS': str(SHOTS),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_state_honesty.mjs'], cwd=str(REPO),
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

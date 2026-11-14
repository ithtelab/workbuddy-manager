"""前端传参与后端参数绑定的回归测试。

这类 bug 的共同特征是**不报错**：FastAPI 对标量参数默认按 **query** 解析，
如果前端把参数放在 JSON body 里（我们的 `post()` 就是这么做），后端拿到的
是默认值——接口照常 200，只是行为是错的。

真实案例（v1.0.26 及更早）：`POST /api/auth/start` 声明 `realm: str = 'cn'`，
前端发 `{realm: 'global'}`，后端恒收到 'cn'。表现为「切到国际版添加账号，
扫出来的还是国内版二维码（copilot.tencent.com）」，而且页面上没有任何报错，
用户只能靠肉眼比对二维码链接才发现。

所以这里不是测「一个端点」，是给**这类静默绑定失配**建一道闸：凡是前端
用 post() 传参的端点，都要有一个用例把「前端实际发的形状」打进后端并断言
参数真的生效。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TMP = tempfile.mkdtemp()
os.environ.setdefault('WB_DATA_DIR', _TMP)

import server.security as sec  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server.main import app  # noqa: E402
from server.services import tencent  # noqa: E402


class _Spy:
    """记录 start_login 实际收到的 realm。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def install(self) -> None:
        async def fake(realm: str = 'cn') -> dict:
            self.calls.append(realm)
            return {'state': 'S', 'authUrl': f'https://example.com/{realm}',
                    'realm': realm}
        self._orig = tencent.start_login
        tencent.start_login = fake  # type: ignore[assignment]

    def restore(self) -> None:
        tencent.start_login = self._orig  # type: ignore[assignment]


class AuthStartRealmBindingTest(unittest.TestCase):
    """`realm` 必须真的生效——这是「国际版添加账号」能不能用的前提。"""

    @classmethod
    def setUpClass(cls) -> None:
        app.dependency_overrides[sec.require_admin] = lambda: {'username': 't', 'role': 'admin'}
        app.dependency_overrides[sec.current_user] = lambda: {'username': 't', 'role': 'admin'}

    @classmethod
    def tearDownClass(cls) -> None:
        app.dependency_overrides.clear()

    def setUp(self) -> None:
        self.spy = _Spy()
        self.spy.install()
        self.addCleanup(self.spy.restore)
        self.client = TestClient(app)

    def _realm_sent(self) -> str:
        self.assertTrue(self.spy.calls, 'start_login 未被调用')
        return self.spy.calls[-1]

    # ── 前端实际使用的形状（JSON body）—— 这条就是当初漏掉的 ──

    def test_realm_in_json_body_is_honoured(self) -> None:
        r = self.client.post('/api/auth/start', json={'realm': 'global'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._realm_sent(), 'global',
                         'body 里的 realm 被忽略了 —— 前端就是这么传的')

    def test_cn_in_json_body(self) -> None:
        self.client.post('/api/auth/start', json={'realm': 'cn'})
        self.assertEqual(self._realm_sent(), 'cn')

    # ── 兼容旧调用方（query） ──

    def test_realm_in_query_still_works(self) -> None:
        self.client.post('/api/auth/start', params={'realm': 'global'})
        self.assertEqual(self._realm_sent(), 'global')

    def test_body_takes_precedence_over_query(self) -> None:
        self.client.post('/api/auth/start',
                         json={'realm': 'cn'}, params={'realm': 'global'})
        self.assertEqual(self._realm_sent(), 'cn', 'body 应与前端一致并优先')

    # ── 缺省与异常输入 ──

    def test_default_is_cn(self) -> None:
        self.client.post('/api/auth/start')
        self.assertEqual(self._realm_sent(), 'cn')

    def test_garbage_falls_back_to_cn(self) -> None:
        for bad in ('xx', '', '  ', 'CN '):
            self.spy.calls.clear()
            self.client.post('/api/auth/start', json={'realm': bad})
            self.assertIn(self._realm_sent(), ('cn',),
                          f'{bad!r} 应回落 cn 而不是被当成 global')

    def test_global_is_case_insensitive(self) -> None:
        for val in ('global', 'GLOBAL', 'Global', ' global '):
            self.spy.calls.clear()
            self.client.post('/api/auth/start', json={'realm': val})
            self.assertEqual(self._realm_sent(), 'global', f'{val!r} 应识别为国际版')


class FrontendPostShapeTest(unittest.TestCase):
    """确认前端 post() 确实把参数放在 body —— 测试要跟真实调用形状一致。

    若哪天 post() 改成 query 形式，上面那个用例就失去意义了；这里把
    「前端到底怎么传」钉住，避免回归测试慢慢与真实调用脱节。
    """

    API_TS = Path(__file__).resolve().parents[2] / 'web' / 'lib' / 'api.ts'

    def test_post_helper_sends_json_body(self) -> None:
        api = self.API_TS.read_text(encoding='utf-8')
        # post 是箭头函数：const post = async <T>(url, body) => (await http.post(url, body)).data
        self.assertIn('const post =', api, 'post 封装不见了，本测试需要跟着改')
        self.assertIn('http.post<T>(url, body)', api,
                      'post() 应把第二个参数作为 body 发出')

    def test_auth_start_passes_realm_in_body(self) -> None:
        """前端这一行就是当初那条 bug 的另一半：{realm, region} 是 body 不是 query。

        region 与 realm 同一条规矩：服务端的后台轮询要用它做国际版地区登记，
        而它拿不到前端组件的 state —— 只能跟着发码请求一起传（见 api.ts 注释）。
        """
        api = self.API_TS.read_text(encoding='utf-8')
        idx = api.find("'/api/auth/start'")
        self.assertGreater(idx, 0, '找不到 auth/start 的前端调用')
        snippet = api[max(0, idx - 200):idx + 80]
        self.assertIn("post<", snippet, 'auth/start 应通过 post() 调用')
        self.assertRegex(snippet, r'\{\s*realm\s*,\s*region\s*(?:,|\})',
                         'realm 与 region 都应以对象形式作为 body 传入 —— '
                         '后端已按 body 解析（写成 query 会静默失效）')


class PostParamLocationAuditTest(unittest.TestCase):
    """审计所有 POST 端点：query 参数必须与前端实际传法一致。

    这类失配**不会报错**，只会让参数静默失效。已知的两个 query 参数端点：
      * `/api/accounts/refresh-credits?force=` —— 前端就是发 query，一致 ✓
      * `/api/auth/start`                     —— 前端发 body，曾经失配（本次修复）

    这里把清单钉住：新增 POST 端点若用 query 参数，必须在此显式登记，
    强制作者确认「前端确实按 query 传」。
    """

    # 允许使用 query 参数的 POST 端点（前端必须相应发 query）
    ALLOWED_QUERY_POSTS = {
        # 单账号刷新（issue #432）按 `?file=` 指定账号；不传 = 整池。
        '/api/accounts/refresh-credits': {'force', 'upstream_id', 'file'},
        # realm / region / uid 都同时接受 query（旧调用方）与 JSON body（前端），
        # 声明成 Query 只是保留兼容；前端一律发 body。
        '/api/auth/start': {'realm', 'region', 'uid', 'upstream_id'},
        # 账号分组（多账号池）：下列写端点都接受 `?upstream_id=` 指定作用于哪个
        # 分组（前端 accountApi 统一按 query 拼；缺省不传 = 默认分组）。
        # 账号拖拽排序（issue #433）：顺序按分组建键，前端按 query 传分组。
        '/api/accounts/order': {'upstream_id'},
        '/api/accounts/checkin-all': {'upstream_id'},
        # 领取活动补偿：与 checkin-all 同形，按 `?upstream_id=` 指定分组。
        '/api/accounts/claim-compensation': {'upstream_id'},
        # 一键刷新令牌：同 checkin-all，按 `?upstream_id=` 指定作用于哪个分组。
        '/api/accounts/refresh-tokens': {'upstream_id'},
        # 账号 × 模型主动探测（issue #270）：账号/模型走 JSON body，分组走 query。
        '/api/accounts/probe-models': {'upstream_id'},
        '/api/accounts/{filename}/checkin': {'upstream_id'},
        '/api/accounts/{filename}/test': {'upstream_id'},
        '/api/accounts/{filename}/refresh': {'upstream_id'},
        '/api/accounts/{filename}/clear-cooling': {'upstream_id'},
        '/api/accounts/{filename}/force-cooling': {'upstream_id'},
        '/api/accounts/{filename}/note': {'upstream_id'},
        '/api/accounts/{filename}/proxy': {'upstream_id'},
        # 手写权重（issue #187）：与 proxy 同形，按 `?upstream_id=` 指定分组。
        '/api/accounts/{filename}/weight': {'upstream_id'},
        # 代理线路（设置 → 代理）：改名/删除要按分组重绑账号，前端按 query 传。
        '/api/proxy-routes/{name}': {'upstream_id'},
        '/api/proxy-routes/{name}/accounts': {'upstream_id'},
        '/api/proxy-routes/{name}/auto-assign': {'upstream_id'},
        '/api/accounts/{filename}/disabled': {'upstream_id'},
        '/api/accounts/{filename}/move': {'upstream_id'},
        # 账号导入（PR #145）：前端把分组与「是否覆盖同 uid」都按 query 传
        '/api/accounts/upload': {'upstream_id', 'overwrite'},
        '/api/restart': {'upstream_id'},
    }

    # 覆盖的方法：**所有带 body 的写方法**，不只是 POST。
    # 只查 POST 会漏掉 PUT/PATCH —— 它们同样会「前端发 body、后端按 query 解析」
    # 而静默失效（本项审查时新增了 `PUT /api/accounts/{filename}/note`，正好落在这个
    # 盲区里：闸门是绿的，但没检查过它）。
    MUTATING_METHODS = ('post', 'put', 'patch')

    def test_sweep_covers_put_and_patch(self) -> None:
        """闸门自身的方法集合不能退化成只剩 post（否则又是空的守）。"""
        for m in ('post', 'put', 'patch'):
            self.assertIn(m, self.MUTATING_METHODS)

    def test_no_undeclared_query_params_on_posts(self) -> None:
        import os
        import tempfile
        os.environ.setdefault('WB_DATA_DIR', tempfile.mkdtemp())
        from server.main import app

        schema = app.openapi()
        offenders = []
        counted = 0
        for path, ops in schema['paths'].items():
            for method, op in ops.items():
                if method not in self.MUTATING_METHODS:
                    continue
                counted += 1
                query = {p['name'] for p in op.get('parameters', [])
                         if p.get('in') == 'query'}
                if not query:
                    continue
                allowed = self.ALLOWED_QUERY_POSTS.get(path)
                if allowed is None or not query <= allowed:
                    offenders.append((path, sorted(query)))
        self.assertGreater(counted, 10, '没扫到写接口 —— 闸门空转了')
        self.assertEqual(
            offenders, [],
            '这些写端点的 query 参数未登记：'
            f'{offenders}。若前端确实发 query，请加入 ALLOWED_QUERY_POSTS；'
            '若前端发 JSON body，则需要用 Body() 声明（否则参数会静默失效）。',
        )

    def test_auth_start_accepts_body(self) -> None:
        """修复的具体保证：auth/start 必须能从 requestBody 取值。"""
        import os
        import tempfile
        os.environ.setdefault('WB_DATA_DIR', tempfile.mkdtemp())
        from server.main import app

        op = app.openapi()['paths']['/api/auth/start']['post']
        self.assertIn('requestBody', op,
                      'auth/start 必须声明 requestBody —— 前端把 realm 放在 body 里')


if __name__ == '__main__':
    unittest.main()

"""网关数据面 CORS（`/v1`、`/v2`）的行为测试 —— issue #445。

为什么要单独一套、不复用 `WB_CORS_ORIGINS`：

  · 管理面 `/api/*` 用 Cookie 会话，`allow_credentials=True`，所以**禁止** `*`
    （见 `test_security_audit_2026_10.CorsStarHardFailTest`）；
  · 数据面 `/v1`、`/v2` 用 `Authorization: Bearer`，浏览器不会自动带，允许 `*`
    是安全的 —— 任意页面能发请求，但没密钥就调不动。

这两条边界必须被测试钉住，否则将来有人「顺手统一成一个开关」，就会在管理面上
开出一个任意站点可读的洞。

另外钉住的是**即时生效**：配置存 DB，中间件每请求现读；如果用启动时固定的
`CORSMiddleware`，改完设置要重启，用户会以为没保存成功。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import db  # noqa: E402
from server.services import gatewaycors as gc  # noqa: E402

_OK_ORIGIN = 'https://app.example.com'
_EVIL_ORIGIN = 'https://evil.example.com'


def _client() -> TestClient:
    from server.main import app
    return TestClient(app)


class SettingsShapeTest(unittest.TestCase):
    """配置的归一化与校验（纯函数层）。"""

    def setUp(self) -> None:
        db.connect()

    def tearDown(self) -> None:
        # 每个用例都从「未配置」开始：用例之间不能靠执行顺序耦合
        db.set_setting(gc.SETTING_KEY, {})

    def test_default_is_disabled_and_empty(self) -> None:
        cfg = gc.settings()
        self.assertFalse(cfg['enabled'])
        self.assertEqual(cfg['origins'], [])
        self.assertEqual(cfg['max_age'], 600)

    def test_trailing_slash_is_stripped(self) -> None:
        """用户常连斜杠一起粘。浏览器比对是不带斜杠的，不削掉会「预检过、真请求被拒」。"""
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN + '/']})
        self.assertEqual(gc.settings()['origins'], [_OK_ORIGIN])

    def test_duplicates_collapse_preserving_order(self) -> None:
        gc.save_settings({'enabled': True,
                          'origins': [_OK_ORIGIN, 'https://b.test', _OK_ORIGIN]})
        self.assertEqual(gc.settings()['origins'], [_OK_ORIGIN, 'https://b.test'])

    def test_missing_scheme_is_rejected(self) -> None:
        for bad in ('app.example.com', '//app.example.com', 'example.com:443'):
            with self.assertRaises(ValueError, msg=bad):
                gc.save_settings({'origins': [bad]})

    def test_header_hostile_characters_are_rejected(self) -> None:
        """引号/逗号/换行写进响应头会让 Starlette 编码时抛错 —— 配置错误不能变成 500。"""
        for bad in ('https://a.test,https://b.test', 'https://a.test"',
                    'https://a.test\nX-Evil: 1', 'https://a.test;q=1'):
            with self.assertRaises(ValueError, msg=bad):
                gc.save_settings({'origins': [bad]})

    def test_non_string_entries_rejected(self) -> None:
        with self.assertRaises(ValueError):
            gc.save_settings({'origins': [123]})
        with self.assertRaises(ValueError):
            gc.save_settings({'origins': 'https://a.test'})

    def test_too_many_origins_rejected_not_truncated(self) -> None:
        """超过上限要报错：静默截断会让用户以为全存进去了。"""
        with self.assertRaises(ValueError):
            gc.save_settings({'origins': [f'https://a{i}.test' for i in range(gc.MAX_ORIGINS + 1)]})

    def test_max_age_clamped(self) -> None:
        db.set_setting(gc.SETTING_KEY, {'enabled': True, 'origins': [_OK_ORIGIN],
                                        'max_age': 99999})
        self.assertEqual(gc.settings()['max_age'], 7200)
        db.set_setting(gc.SETTING_KEY, {'enabled': True, 'origins': [_OK_ORIGIN],
                                        'max_age': -5})
        self.assertEqual(gc.settings()['max_age'], 0)

    def test_enabled_requires_at_least_one_origin(self) -> None:
        """开关打开但列表为空 = 实际不生效。对外报 enabled=False，界面才不会自相矛盾。"""
        gc.save_settings({'enabled': True, 'origins': []})
        self.assertFalse(gc.settings()['enabled'])

    def test_null_origin_can_be_listed_explicitly(self) -> None:
        """`file://` 页面 / 沙箱 iframe 的 Origin 是字面量 `null`，本地 HTML 直连时需要。"""
        gc.save_settings({'enabled': True, 'origins': ['null']})
        self.assertEqual(gc.settings()['origins'], ['null'])
        self.assertTrue(gc.allows('null'))

    def test_wildcard_is_literal(self) -> None:
        gc.save_settings({'enabled': True, 'origins': ['*']})
        self.assertEqual(gc.settings()['origins'], ['*'])
        self.assertTrue(gc.allows('https://anything.at.all'))
        self.assertEqual(gc.allow_origin_value('https://anything.at.all'), '*')

    def test_dirty_stored_value_falls_back(self) -> None:
        """手工改库/旧版本写进去的形态不能让读配置炸掉。"""
        db.set_setting(gc.SETTING_KEY, 'not-a-dict')
        self.assertEqual(gc.settings()['origins'], [])
        db.set_setting(gc.SETTING_KEY, {'enabled': True, 'origins': [_OK_ORIGIN, None, 7]})
        self.assertEqual(gc.settings()['origins'], [_OK_ORIGIN])

    def test_allows_is_false_when_disabled(self) -> None:
        cfg = {'enabled': False, 'origins': [_OK_ORIGIN], 'max_age': 600}
        self.assertFalse(gc.allows(_OK_ORIGIN, cfg))
        self.assertEqual(gc.allow_origin_value(_OK_ORIGIN, cfg), '')

    def test_matching_is_case_sensitive(self) -> None:
        """与 Starlette 一致：逐字比对。强行小写化反而会让有意区分的来源失效。"""
        cfg = {'enabled': True, 'origins': ['https://App.Example.com'], 'max_age': 600}
        self.assertTrue(gc.allows('https://App.Example.com', cfg))
        self.assertFalse(gc.allows('https://app.example.com', cfg))

    def test_non_ascii_origin_rejected(self) -> None:
        """中文域名不能存：响应头按 latin-1 编码，回显时抛异常 = 每次预检 500。"""
        for bad in ('https://例子.com', 'https://a.test/😀'):
            with self.assertRaises(ValueError, msg=bad):
                gc.save_settings({'origins': [bad]})

    def test_dirty_non_ascii_stored_value_does_not_break(self) -> None:
        """历史落库的脏值（旧版本/手改库）不能让预检 500 —— 读配置时就被滤掉。"""
        db.set_setting(gc.SETTING_KEY, {'enabled': True, 'origins': ['https://例子.com'],
                                        'max_age': 600})
        cfg = gc.settings()
        self.assertEqual(cfg['origins'], [])
        self.assertFalse(cfg['enabled'], '没有合法来源时开关必须自认关闭')

    def test_db_failure_never_raises(self) -> None:
        """settings() 跑在 /v1 每个请求上，DB 故障不能把网关打成 500。"""
        class _Boom:
            def get_setting(self, *a, **k):
                raise RuntimeError('database is locked')
        real = gc.db
        gc.db = _Boom()
        try:
            self.assertEqual(gc.settings()['origins'], [])
        finally:
            gc.db = real

    def test_path_matching_is_segment_aware(self) -> None:
        """/v1foo、/v10/x 不属于数据面，不该被加上 CORS 头（实测曾命中）。"""
        for bad in ('/v1foo', '/v10/x', '/v2abc', '/v1beta'):
            self.assertFalse(gc.is_data_api_path(bad), bad)
        for good in ('/v1', '/v1/chat/completions', '/v2/chat/completions', '/v1/messages'):
            self.assertTrue(gc.is_data_api_path(good), good)

    def test_false_like_strings_do_not_enable(self) -> None:
        """`bool('false')` 是 True —— 那会让「已经关掉」的配置仍在放行（错误放行）。"""
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN]})
        for value in ('false', '0', 'off', 'no', '', False, 0):
            gc.save_settings({'enabled': value})
            self.assertFalse(gc.settings()['enabled'], f'enabled={value!r} 不应等于开启')
        for value in ('true', '1', 'on', 'yes', True, 1):
            gc.save_settings({'enabled': value})
            self.assertTrue(gc.settings()['enabled'], f'enabled={value!r} 应等于开启')

    def test_dirty_stored_enabled_string_does_not_enable(self) -> None:
        """历史落库/手改库写成字符串时，读路径也要按字面值判断。"""
        db.set_setting(gc.SETTING_KEY, {'enabled': 'false', 'origins': [_OK_ORIGIN]})
        self.assertFalse(gc.settings()['enabled'])


class PreflightTest(unittest.TestCase):
    """预检（OPTIONS + Access-Control-Request-Method）由中间件直接应答。"""

    def setUp(self) -> None:
        db.connect()
        db.set_setting(gc.SETTING_KEY, {})
        self.client = _client()

    def tearDown(self) -> None:
        db.set_setting(gc.SETTING_KEY, {})

    def _preflight(self, path: str, origin: str, method: str = 'POST'):
        return self.client.options(path, headers={
            'Origin': origin,
            'Access-Control-Request-Method': method,
            'Access-Control-Request-Headers': 'authorization,content-type',
        })

    def test_disabled_gateway_blocks_preflight(self) -> None:
        """未配置时不能静默 200：那会让浏览器在真实请求上才失败，症状难定位。"""
        r = self._preflight('/v1/chat/completions', _OK_ORIGIN)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error']['code'], 'origin_not_allowed')

    def test_allowed_origin_gets_204_with_headers(self) -> None:
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN], 'max_age': 300})
        r = self._preflight('/v1/chat/completions', _OK_ORIGIN)
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers['access-control-allow-origin'], _OK_ORIGIN)
        self.assertEqual(r.headers['access-control-max-age'], '300')
        self.assertIn('POST', r.headers['access-control-allow-methods'])
        # **不能**带 allow-credentials：数据面用 Bearer，带上会与将来放宽的 `*`
        # 冲突，也会误导读者以为这里认 Cookie。
        self.assertNotIn('access-control-allow-credentials', r.headers)

    def test_unknown_origin_blocked(self) -> None:
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN]})
        self.assertEqual(self._preflight('/v1/chat/completions', _EVIL_ORIGIN).status_code, 403)

    def test_covers_anthropic_and_responses_protocols(self) -> None:
        """三条协议都要能用：chat / anthropic messages / responses。"""
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN]})
        for path in ('/v1/chat/completions', '/v2/chat/completions',
                     '/v1/messages', '/v1/responses', '/v1/models'):
            r = self._preflight(path, _OK_ORIGIN, method='GET' if path == '/v1/models' else 'POST')
            self.assertEqual(r.status_code, 204, path)

    def test_admin_surface_is_not_opened(self) -> None:
        """`/api/*` 不归本中间件管 —— 那是 Cookie 管理面，绝不能因为这里放宽而暴露。"""
        gc.save_settings({'enabled': True, 'origins': ['*']})
        r = self._preflight('/api/accounts', _EVIL_ORIGIN, method='GET')
        # 没装 WB_CORS_ORIGINS 时 CorsMiddleware 不存在，OPTIONS 落到路由 → 405；
        # 关键是**没有** access-control-allow-origin。
        self.assertNotIn('access-control-allow-origin', r.headers)

    def test_takes_effect_without_restart(self) -> None:
        """配置改完立刻生效 —— 这正是没用启动期 `CORSMiddleware` 的原因。"""
        self.assertEqual(self._preflight('/v1/chat/completions', _OK_ORIGIN).status_code, 403)
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN]})
        self.assertEqual(self._preflight('/v1/chat/completions', _OK_ORIGIN).status_code, 204)
        gc.save_settings({'enabled': False})
        self.assertEqual(self._preflight('/v1/chat/completions', _OK_ORIGIN).status_code, 403)

    def test_simple_preflight_without_request_headers_passes(self) -> None:
        """没有 Access-Control-Request-Headers 是合法的（简单请求），不能拒。"""
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN]})
        r = self._preflight('/v1/chat/completions', _OK_ORIGIN)
        self.assertEqual(r.status_code, 204)

    def test_sdk_specific_headers_are_echoed(self) -> None:
        """OpenAI / Anthropic SDK 会带 openai-* / anthropic-* 头，不回显就发不出去。"""
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN]})
        r = self.client.options('/v1/chat/completions', headers={
            'Origin': _OK_ORIGIN,
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'authorization,openai-organization,anthropic-beta',
        })
        self.assertEqual(r.status_code, 204)
        got = r.headers['access-control-allow-headers']
        self.assertIn('openai-organization', got)
        self.assertIn('anthropic-beta', got)

    def test_unknown_headers_are_not_echoed(self) -> None:
        """请求方要的头不能原样全回显 —— 只放白名单里的，其余丢弃。"""
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN]})
        r = self.client.options('/v1/chat/completions', headers={
            'Origin': _OK_ORIGIN,
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'authorization,x-evil,x-bad',
        })
        self.assertEqual(r.status_code, 204)
        got = r.headers['access-control-allow-headers']
        self.assertNotIn('x-evil', got)
        self.assertNotIn('x-bad', got)

    def test_preflight_with_only_rejected_headers_is_403(self) -> None:
        """一个都不允许时明确拒，而不是给个缺头的允许响应（后者报错方向会误导）。"""
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN]})
        r = self.client.options('/v1/chat/completions', headers={
            'Origin': _OK_ORIGIN,
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'x-evil,x-bad',
        })
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error']['code'], 'header_not_allowed')

    def test_rejection_carries_vary_origin(self) -> None:
        """403 也要 Vary: Origin，否则中间缓存会把一个来源的拒绝复用给另一个。"""
        r = self._preflight('/v1/chat/completions', _OK_ORIGIN)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.headers.get('vary'), 'Origin')

    def test_non_surface_prefixes_get_no_headers(self) -> None:
        """/v1foo、/v10/x 不属于数据面（曾因 startswith 误命中）。"""
        gc.save_settings({'enabled': True, 'origins': ['*']})
        for path in ('/v1foo', '/v10/x', '/v2abc'):
            r = self._preflight(path, _OK_ORIGIN, method='GET')
            self.assertNotIn('access-control-allow-origin', r.headers, path)


class ActualRequestTest(unittest.TestCase):
    """普通请求：命中来源时补头，错误响应也补（前端才读得到 401 的正文）。"""

    def setUp(self) -> None:
        db.connect()
        db.set_setting(gc.SETTING_KEY, {})
        self.client = _client()

    def tearDown(self) -> None:
        db.set_setting(gc.SETTING_KEY, {})

    def test_error_response_still_carries_cors_headers(self) -> None:
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN]})
        r = self.client.post('/v1/chat/completions',
                             headers={'Origin': _OK_ORIGIN},
                             json={'model': 'x', 'messages': []})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers['access-control-allow-origin'], _OK_ORIGIN)
        self.assertIn('x-trace-id', r.headers['access-control-expose-headers'])

    def test_non_allowed_origin_gets_no_headers(self) -> None:
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN]})
        r = self.client.post('/v1/chat/completions',
                             headers={'Origin': _EVIL_ORIGIN},
                             json={'model': 'x', 'messages': []})
        self.assertNotIn('access-control-allow-origin', r.headers)

    def test_no_origin_header_is_left_alone(self) -> None:
        """非浏览器客户端（curl / SDK）不带 Origin，不该被加上任何 CORS 头。"""
        gc.save_settings({'enabled': True, 'origins': [_OK_ORIGIN]})
        r = self.client.post('/v1/chat/completions',
                             json={'model': 'x', 'messages': []})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn('access-control-allow-origin', r.headers)


class EndpointTest(unittest.TestCase):
    """`/api/settings/gateway-cors` 的读写与鉴权。"""

    def setUp(self) -> None:
        db.connect()
        db.set_setting(gc.SETTING_KEY, {})
        self.client = _client()

    def tearDown(self) -> None:
        db.set_setting(gc.SETTING_KEY, {})

    def _login(self) -> None:
        """用管理员会话登录（首次启动会自动建号，密码打印在日志里 —— 这里直接取）。"""
        from server import security
        cfg = security.load_users()
        admin = next((u for u in cfg.get('users') or [] if u.get('role') == 'admin'), None)
        self.assertIsNotNone(admin, '没有管理员账号')
        token = security.issue_token(admin['username'], '')
        self.client.cookies.set('wb_session', token)

    def test_requires_auth(self) -> None:
        self.assertIn(self.client.get('/api/settings/gateway-cors').status_code, (401, 403))

    def test_roundtrip(self) -> None:
        self._login()
        r = self.client.put('/api/settings/gateway-cors',
                            json={'enabled': True, 'origins': [_OK_ORIGIN + '/'],
                                  'max_age': 120})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['enabled'])
        self.assertEqual(body['origins'], [_OK_ORIGIN])
        self.assertEqual(body['max_age'], 120)
        # 管理面来源要在响应里可见（好让用户明白为什么 /api 仍可能跨域失败）
        self.assertIn('admin_origins', body)

        r = self.client.get('/api/settings/gateway-cors')
        self.assertEqual(r.json()['origins'], [_OK_ORIGIN])

    def test_bad_origin_returns_400(self) -> None:
        self._login()
        r = self.client.put('/api/settings/gateway-cors',
                            json={'enabled': True, 'origins': ['nope']})
        self.assertEqual(r.status_code, 400)
        self.assertIn('格式', r.json()['detail'])

class VaryMergeTest(unittest.TestCase):
    """`Vary` 必须与下游已有值合并 —— 否则共享缓存会跨来源复用响应。"""

    def test_adds_origin_when_absent(self) -> None:
        from server.main import _merge_vary_origin
        self.assertEqual(_merge_vary_origin([]), [(b'vary', b'Origin')])

    def test_merges_with_accept_encoding(self) -> None:
        from server.main import _merge_vary_origin
        got = _merge_vary_origin([(b'vary', b'Accept-Encoding')])
        self.assertEqual(got, [(b'vary', b'Accept-Encoding, Origin')])

    def test_does_not_duplicate_origin(self) -> None:
        from server.main import _merge_vary_origin
        got = _merge_vary_origin([(b'Vary', b'accept-encoding, origin')])
        self.assertEqual(got, [(b'vary', b'accept-encoding, origin')])

    def test_collapses_multiple_vary_headers(self) -> None:
        """多条 Vary 语义上是并集，但不少代理只看第一条 —— 合并成一条更稳。"""
        from server.main import _merge_vary_origin
        got = _merge_vary_origin([(b'vary', b'Accept-Encoding'), (b'vary', b'Origin')])
        self.assertEqual(got, [(b'vary', b'Accept-Encoding, Origin')])

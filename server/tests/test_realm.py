"""版本（realm）适配的回归测试：国内版 / 国际版。

上游从 2026-09-14 起单实例同时支持两套上游（共用账号池，按账号 realm 或
模型名前缀路由）。这里锁定几个容易写错、且写错就会「打错上游」的点：

  1. realm 判定顺序：显式字段 > 域名后缀；**逃生门（enabled=false）恒 cn**
  2. 落盘用的 resolve_realm 不受逃生门影响（否则会把国际版账号永久写成 cn）
  3. 端点分派：billing 的路径候选顺序**两边相反**（照上游实现，不是笔误）
  4. 国际版没有签到体系 → 直接跳过，不打上游请求
  5. 登录 state 的 realm 一致性（防止跨版本串用授权码）
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import realm, tencent  # noqa: E402


class RealmDetectTest(unittest.TestCase):
    def test_explicit_field_wins(self) -> None:
        self.assertEqual(realm.resolve_realm('global', 'www.codebuddy.cn'), 'global')
        self.assertEqual(realm.resolve_realm('cn', 'www.workbuddy.ai'), 'cn')

    def test_domain_fallback(self) -> None:
        self.assertEqual(realm.resolve_realm(None, 'www.workbuddy.ai'), 'global')
        self.assertEqual(realm.resolve_realm(None, 'api.workbuddy.ai'), 'global')
        self.assertEqual(realm.resolve_realm(None, 'WORKBUDDY.AI'), 'global')
        self.assertEqual(realm.resolve_realm(None, 'www.codebuddy.cn'), 'cn')

    def test_empty_is_cn(self) -> None:
        """存量账号既无 realm 也无 domain → cn，保证升级后行为不变。"""
        self.assertEqual(realm.resolve_realm(None, ''), 'cn')
        self.assertEqual(realm.resolve_realm(None, None), 'cn')

    def test_unknown_explicit_falls_back_to_domain(self) -> None:
        self.assertEqual(realm.resolve_realm('bogus', 'www.workbuddy.ai'), 'global')

    def test_escape_hatch_forces_cn(self) -> None:
        """逃生门（global.enabled=false）下 Realm() 恒 cn，但落盘解析不受影响。"""
        with mock.patch.object(realm, '_read_global_config',
                               return_value={'enabled': False, 'chat_base': '', 'billing_base': ''}):
            self.assertEqual(realm.realm_of({'realm': 'global'}), 'cn')
            self.assertEqual(realm.realm_of({'domain': 'www.workbuddy.ai'}), 'cn')
            self.assertFalse(realm.global_enabled())
            # 关键：落盘时必须仍按真实归属解析，否则国际版账号会被写成 cn
            self.assertEqual(realm.resolve_realm('global', ''), 'global')

    def test_enabled_by_default(self) -> None:
        self.assertTrue(realm.global_enabled())


class EndpointTest(unittest.TestCase):
    def test_chat_base_per_realm(self) -> None:
        self.assertEqual(realm.chat_base('cn'), 'https://copilot.tencent.com')
        self.assertEqual(realm.chat_base('global'), 'https://www.workbuddy.ai')

    def test_billing_base_per_realm(self) -> None:
        self.assertEqual(realm.billing_base('cn'), 'https://www.codebuddy.cn')
        self.assertEqual(realm.billing_base('global'), 'https://www.workbuddy.ai')

    def test_chat_paths(self) -> None:
        """`/v2` 仍是**主路径**，`/console` 只在渠道校验时兜底（2026-10-17）。

        国际版曾以 `/console` 优先，但那个端点上挂了腾讯云 WAF 的请求体内容规则：
        正文里出现反引号 `printf` / `whoami` 等命令执行特征会被确定性拦成 403
        ——用户只是问一句 shell 命令就中招。`/v2` 是同一 base 下的等价端点，不挂该规则，
        所以**主路径不变**。

        2026-10-17 加回 `/console` 做兜底：上游给 /v2 加了渠道强校验（被校验到的请求回
        「没通过渠道校验」），而官方客户端走的 /console 放行 —— 用户实测换过去就恢复。
        触发条件**只有渠道校验**（不是 404/405），所以上面那条 WAF 规则只在兜底路径上
        才可能碰到。

        这条断言的意义不是「路径长什么样」，而是**我们的探测打的端点必须与上游转发用的
        端点一致**：否则某天 /v2 真被渠道校验拦死时，我们的「测活」会告诉用户账号不可用，
        而实际上游转发一切正常（反过来也一样）。
        """
        for r in ('cn', 'global'):
            paths = realm.chat_paths(r)
            self.assertEqual(paths[0], '/v2/chat/completions',
                             '主路径必须是 /v2（/console 挂 WAF 内容规则，只能当兜底）')
            self.assertEqual(paths[1:], ['/console/chat/completions'],
                             '兜底路径要存在且只有它一条，与上游 chatPaths 同序')

    def test_billing_path_order_is_reversed(self) -> None:
        """国际版无 /v2 前缀优先，国内版只有 /v2——两者顺序相反，照上游实现。"""
        self.assertEqual(realm.billing_paths('cn', 'user-resource'),
                         ['/v2/billing/meter/get-user-resource'])
        self.assertEqual(realm.billing_paths('global', 'user-resource'),
                         ['/billing/meter/get-user-resource',
                          '/v2/billing/meter/get-user-resource'])
        self.assertEqual(realm.billing_paths('cn', 'daily-checkin'),
                         ['/v2/billing/meter/daily-checkin'])

    def test_origin_and_ua_per_realm(self) -> None:
        cn = realm.headers('cn')
        gl = realm.headers('global')
        self.assertEqual(cn['Origin'], 'https://www.codebuddy.cn')
        self.assertEqual(gl['Origin'], 'https://www.workbuddy.ai')
        # 品牌段不同：国际版是 "WorkBuddy AI"
        self.assertIn('WorkBuddy/', cn['User-Agent'])
        self.assertIn('WorkBuddy AI/', gl['User-Agent'])
        self.assertNotIn('WorkBuddy AI', cn['User-Agent'])

    # ── 出站风控头对齐上游 2026-09-14 的改动（D1/D5/D6）──
    #
    # 管理端有一批请求**绕过上游直连腾讯**（扫码登录、签到、积分、trial、
    # 注册）。上游给它自己的出站加了这些头；我们这条路若不跟，就会成为
    # 唯一「形态不像官方客户端」的流量，被风控挑出来的代价是账号被封。

    def test_outbound_risk_control_headers_present(self) -> None:
        """D1：X-CodeBuddy-Request 是官方客户端的风控闸门头，所有请求必带。"""
        for r in ('cn', 'global'):
            h = realm.headers(r)
            self.assertEqual(h.get('X-CodeBuddy-Request'), '1', f'{r} 缺风控闸门头')

    def test_accept_language_switches_by_realm(self) -> None:
        """D5：按账号域切语言标识（官方客户端就是这么发的）。"""
        self.assertEqual(realm.headers('cn')['Accept-Language'], 'zh-CN')
        self.assertEqual(realm.headers('global')['Accept-Language'], 'en-US')

    def test_accept_tightened_for_non_stream(self) -> None:
        """D6：非流式收紧为 application/json，不再带宽松的 text/plain, */*。"""
        for r in ('cn', 'global'):
            accept = realm.headers(r)['Accept']
            self.assertEqual(accept, 'application/json', f'{r} 的 Accept 未收紧')
            self.assertNotIn('text/plain', accept)

    def test_chat_stream_overrides_accept(self) -> None:
        """流式路径才声明 event-stream —— 由调用方覆盖（probe_account 等）。"""
        h = realm.headers('cn', 'tok')
        self.assertEqual(h['Authorization'], 'Bearer tok')
        # 确认「能覆盖」这件事本身成立（调用方据此改写）
        h['Accept'] = 'application/json, text/event-stream'
        self.assertIn('text/event-stream', h['Accept'])

    def test_static_cn_headers_match_realm_headers(self) -> None:
        """config.TENCENT_HEADERS 目前无调用点，但必须与 realm.headers 同口径。

        否则将来有人照着它取值，就发出与风控口径矛盾的请求 —— 这类"看起来
        能用"的静态常量最容易成为下一个坑。
        """
        static = config.TENCENT_HEADERS
        live = realm.headers('cn')
        for key in ('Accept', 'Accept-Language', 'X-CodeBuddy-Request',
                    'Content-Type', 'X-Requested-With', 'Origin', 'Referer'):
            self.assertEqual(static.get(key), live.get(key),
                             f'TENCENT_HEADERS.{key} 与 realm.headers("cn") 不一致')

    def test_custom_base_overrides_default(self) -> None:
        with mock.patch.object(realm, '_read_global_config',
                               return_value={'enabled': True,
                                             'chat_base': 'https://intl.example.com',
                                             'billing_base': 'https://bill.example.com'}):
            realm.invalidate()
            self.assertEqual(realm.chat_base('global'), 'https://intl.example.com')
            self.assertEqual(realm.billing_base('global'), 'https://bill.example.com')
        realm.invalidate()

    def test_cn_base_unaffected_by_global_config(self) -> None:
        with mock.patch.object(realm, '_read_global_config',
                               return_value={'enabled': True,
                                             'chat_base': 'https://intl.example.com',
                                             'billing_base': ''}):
            self.assertEqual(realm.chat_base('cn'), 'https://copilot.tencent.com')


class CheckinSupportTest(unittest.TestCase):
    def test_cn_supported_global_not(self) -> None:
        self.assertTrue(realm.supports_checkin('cn'))
        self.assertFalse(realm.supports_checkin('global'))

    def test_global_checkin_skips_without_request(self) -> None:
        """国际版签到必须直接返回，不能打上游请求（避免风控）。"""
        import asyncio
        called = []

        class Boom:
            def __init__(self, *a, **k):
                called.append(1)

        with mock.patch.object(config, 'http_client', Boom):
            code, msg = asyncio.run(tencent.checkin('tok', 'global'))
        self.assertEqual(code, -2)
        self.assertIn('国际版', msg)
        self.assertEqual(called, [], '不应发起任何 HTTP 请求')


class StateRealmTest(unittest.TestCase):
    """扫码 state 的版本一致性：跨版本轮询必须被拒绝。"""

    def setUp(self) -> None:
        tencent._state_cache.clear()

    def tearDown(self) -> None:
        tencent._state_cache.clear()

    def test_registered_realm_recorded(self) -> None:
        import asyncio
        import time
        # 直接写缓存模拟 start_login 的登记
        tencent._state_cache['s1'] = (time.time(), 'global')
        self.assertEqual(tencent.state_realm('s1'), 'global')
        self.assertTrue(tencent.is_pending('s1'))
        # 用国内版去轮询 → 明确报不匹配，而不是继续往下走
        out = asyncio.run(tencent.poll_login('s1', 'cn'))
        self.assertEqual(out['status'], 'realm_mismatch')
        self.assertEqual(out['expected'], 'global')
        self.assertEqual(out['got'], 'cn')
        # 不 drop：用户切回去还能继续用
        self.assertTrue(tencent.is_pending('s1'))

    def test_unknown_state_invalid(self) -> None:
        import asyncio
        out = asyncio.run(tencent.poll_login('nope', 'cn'))
        self.assertEqual(out['status'], 'invalid')

    def test_ttl_is_generous_enough_for_phone_login(self) -> None:
        """state 有效期要够手机号登录用（社区反馈：手机号登录「返回登录有问题」）。

        腾讯授权页除了扫码，也支持**手机号 + 短信验证码**登录：短信有运营商延迟、
        用户还可能中途去翻手机，全程超过 5 分钟很常见。此前 TTL 写 300 秒，
        超时后前端报「二维码已失效」，而用户觉得自己刚授权成功 —— 现象对不上。

        上游 workbuddy2api 对这个 state **没有任何超时**（它的 login.sh 是手动按 y
        才 poll）。我们保留 TTL 只为清理内存，放宽的开销可以忽略（人工低频操作、
        每条几十字节）。
        """
        self.assertGreaterEqual(
            tencent.STATE_TTL, 900,
            f'STATE_TTL={tencent.STATE_TTL}s 对手机号登录偏紧，用户会被误报过期')

    def test_state_survives_a_realistic_phone_login(self) -> None:
        """手机号登录耗时 8 分钟时**不能**判过期（拿真实 poll_login 走一遍）。

        poll_login 的过期判定发生在出站请求**之前**，所以这里不需要打网络：
        只要不返回 expired 就说明判定通过了（后续会因拿不到 token 而 waiting）。
        """
        import asyncio
        import time as _t

        tencent._state_cache['phone'] = (_t.time() - 8 * 60, 'cn')

        class _Resp:
            def json(self) -> dict:
                return {'code': 0, 'data': {}}

        class _Ctx:
            async def __aenter__(self):
                class _C:
                    async def get(self, *a, **kw):
                        return _Resp()
                return _C()

            async def __aexit__(self, *a):
                return False

        with mock.patch.object(config, 'http_client', lambda *a, **k: _Ctx()):
            out = asyncio.run(tencent.poll_login('phone', 'cn'))
        self.assertNotEqual(out['status'], 'expired',
                            '8 分钟就判过期 —— 手机号登录很容易踩到')
        self.assertEqual(out['status'], 'waiting')
        tencent._state_cache.clear()
        _ = asyncio  # 保持导入一致性


class PollDoesNotDropStateTest(unittest.TestCase):
    """issue #26：`poll_login` 拿到 ready 后**不能**自己丢 state。

    报障现象：微信扫码确认后，弹窗显示「二维码已失效」。

    根因链：`poll_login` 早先是拿到 uid 就 `drop_state`，而真正落盘在**路由**里
    （`accounts.py` 的 `write_auth_file`）。若落盘抛错（宝塔/1Panel 部署下 auths
    目录属主不对 → PermissionError 很常见），那次请求 500、前端 catch 静默吞掉；
    下一轮轮询时 state 已被丢掉 → 返回 `invalid` → 界面显示「二维码已失效」，
    把用户引向「重新扫码」—— 而重扫必然同样失败，因为问题是目录权限。

    正确语义：state 的有效期是「发码起 5 分钟」（由 TTL 分支负责），
    不是「拿到 token 就作废」；成功路径由调用方在**落盘之后**显式 drop。
    """

    def setUp(self) -> None:
        tencent._state_cache.clear()

    def tearDown(self) -> None:
        tencent._state_cache.clear()

    def _poll_with_stubbed_tencent(self, tokens: dict) -> dict:
        """跑一次 poll_login，把出站 HTTP 换成假响应，避免真发请求。"""
        import asyncio

        class _Resp:
            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def json(self) -> dict:
                return self._payload

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None, headers=None):
                if 'auth/token' in url:
                    return _Resp({'code': 0, 'data': tokens})
                return _Resp({'code': 0, 'data': {'uid': 'u123', 'nickname': 'n',
                                                  'enterpriseId': ''}})

        class _Ctx:
            async def __aenter__(self):
                return _Client()

            async def __aexit__(self, *a):
                return False

        with mock.patch.object(config, 'http_client', lambda *a, **k: _Ctx()):
            return asyncio.run(tencent.poll_login('s1', 'cn'))

    def test_ready_keeps_state_for_caller(self) -> None:
        """拿到 ready 后 state 必须还在 —— 落盘失败时用户才能重试。"""
        import time

        tencent._state_cache['s1'] = (time.time(), 'cn')
        out = self._poll_with_stubbed_tencent(
            {'accessToken': 'AT', 'refreshToken': 'RT', 'expiresIn': 3600, 'domain': ''})
        self.assertEqual(out['status'], 'ready')
        self.assertTrue(tencent.is_pending('s1'),
                        'poll_login 提前丢了 state —— 落盘失败后重试会被误报成「二维码失效」')

    def test_caller_can_drop_after_saving(self) -> None:
        """成功路径由调用方在落盘后收尾，drop 之后才判 invalid。"""
        import time

        tencent._state_cache['s1'] = (time.time(), 'cn')
        self._poll_with_stubbed_tencent(
            {'accessToken': 'AT', 'refreshToken': 'RT', 'expiresIn': 3600, 'domain': ''})
        tencent.drop_state('s1')
        self.assertFalse(tencent.is_pending('s1'))

    def test_ttl_still_bounds_the_retry_window(self) -> None:
        """不 drop 不等于永不过期：TTL 仍会兜住（否则 state 缓存会无限涨）。"""
        import asyncio
        import time

        tencent._state_cache['old'] = (time.time() - tencent.STATE_TTL - 1, 'cn')
        out = asyncio.run(tencent.poll_login('old', 'cn'))
        self.assertEqual(out['status'], 'expired')
        self.assertFalse(tencent.is_pending('old'), '过期时应顺手清掉')


class AuthPollSaveFailureTest(unittest.TestCase):
    """落盘失败要给出**可执行的**提示，且不能把 state 一起弄丢（issue #26）。"""

    def setUp(self) -> None:
        tencent._state_cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_auth = config.AUTH_DIR
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.AUTH_DIR = Path(self._tmp.name) / 'auths'
        config.DB_PATH = Path(self._tmp.name) / 'm.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        from server import db
        db._conn = None
        db.connect()
        self._db = db

    def tearDown(self) -> None:
        if self._db._conn is not None:
            self._db._conn.close()
        self._db._conn = None
        config.AUTH_DIR = self._orig_auth
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        tencent._state_cache.clear()
        self._tmp.cleanup()

    def test_save_failure_message_is_actionable_and_keeps_state(self) -> None:
        import time

        from fastapi.testclient import TestClient
        from server import security
        from server.main import app
        from server.routers import accounts as accounts_mod

        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')}],
            'api_keys': []})
        c = TestClient(app)
        self.assertEqual(c.post('/api/login',
                                json={'username': 'admin', 'password': 'p'}).status_code, 200)

        tencent._state_cache['s1'] = (time.time(), 'cn')
        ready = {
            'status': 'ready', 'uid': 'u1', 'nickname': 'n', 'enterprise_id': '',
            'access_token': 'AT', 'refresh_token': 'RT',
            'expires_at': int(time.time()) + 3600, 'domain': '', 'realm': 'cn',
        }
        with mock.patch.object(tencent, 'poll_login', return_value=ready), \
             mock.patch.object(tencent, 'checkin', return_value=(0, 'ok')), \
             mock.patch.object(tencent, 'write_auth_file',
                               side_effect=PermissionError('permission denied')), \
             mock.patch.object(accounts_mod.reload, 'request_restart'):
            r = c.get('/api/auth/poll', params={'state': 's1', 'realm': 'cn'})

        self.assertEqual(r.status_code, 500, r.text)
        detail = r.json()['detail']
        # 提示必须说清三件事：已授权成功、去哪查权限、不用重新扫码
        self.assertIn('授权成功', detail)
        self.assertIn('权限', detail)
        self.assertIn('不需要重新扫码', detail)
        # 最关键的：state 还在，用户重试能成功
        self.assertTrue(tencent.is_pending('s1'),
                        '落盘失败却丢了 state —— 用户重试会看到「二维码已失效」')

    def test_retry_does_not_repeat_side_effects(self) -> None:
        """重试只补落盘，**不重跑签到与 trial**。

        这是「不丢 state」带来的连带问题（发版前自审发现）：前端每 2 秒轮询同一个
        state，而 poll_login 每次都返回 ready —— 落盘失败后不记一笔，每次轮询都会
        重跑签到（重复写 checkin_logs）并重复调腾讯接口。用户看到的「重试」本该
        是幂等的。
        """
        from fastapi.testclient import TestClient
        from server import security
        from server.main import app
        from server.routers import accounts as accounts_mod

        accounts_mod._provisioned_states.clear()
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')}],
            'api_keys': []})
        c = TestClient(app)
        c.post('/api/login', json={'username': 'admin', 'password': 'p'})

        tencent._state_cache['s2'] = (time.time(), 'cn')
        ready = {
            'status': 'ready', 'uid': 'u2', 'nickname': 'n2', 'enterprise_id': '',
            'access_token': 'AT', 'refresh_token': 'RT',
            'expires_at': int(time.time()) + 3600, 'domain': '', 'realm': 'cn',
        }
        # 必须是 AsyncMock：路由里是 `await tencent.checkin(...)`，
        # 用同步 MagicMock 会得到 "object tuple can't be used in 'await' expression"
        checkin = mock.AsyncMock(return_value=(0, 'ok'))

        # 第一次：落盘失败 → 500
        with mock.patch.object(tencent, 'poll_login', return_value=ready), \
             mock.patch.object(tencent, 'checkin', checkin), \
             mock.patch.object(tencent, 'write_auth_file',
                               side_effect=PermissionError('permission denied')), \
             mock.patch.object(accounts_mod.reload, 'request_restart'):
            r1 = c.get('/api/auth/poll', params={'state': 's2', 'realm': 'cn'})
        self.assertEqual(r1.status_code, 500, r1.text)
        self.assertEqual(checkin.call_count, 1, '首次应正常签到一次')

        # 第二次（用户重试）：落盘成功 → 不该再签到
        with mock.patch.object(tencent, 'poll_login', return_value=ready), \
             mock.patch.object(tencent, 'checkin', checkin), \
             mock.patch.object(tencent, 'write_auth_file',
                               return_value=('workbuddy-u2.json', False)), \
             mock.patch.object(accounts_mod.reload, 'request_restart'):
            r2 = c.get('/api/auth/poll', params={'state': 's2', 'realm': 'cn'})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(checkin.call_count, 1,
                         f'重试又签到了一次（共 {checkin.call_count} 次）—— 会重复写记录')
        self.assertFalse(tencent.is_pending('s2'), '成功落盘后应丢弃 state')
        self.assertEqual(list(accounts_mod._provisioned_states), [],
                         '成功后要清掉供给标记，避免长期占用内存')

    def test_provisioned_marker_expires_with_state(self) -> None:
        """供给标记要与 state 同一有效期，不能无限堆积。"""
        from server.routers import accounts as accounts_mod

        accounts_mod._provisioned_states.clear()
        # 一条过期的 + 一条新鲜的
        accounts_mod._provisioned_states['old'] = time.time() - tencent.STATE_TTL - 10
        accounts_mod._mark_provisioned('new')
        self.assertNotIn('old', accounts_mod._provisioned_states, '过期标记应被清掉')
        self.assertIn('new', accounts_mod._provisioned_states)
        accounts_mod._provisioned_states.clear()


class WriteAuthFileTest(unittest.TestCase):
    """落盘要带 realm，且用不含逃生门的解析。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        self._tmp.cleanup()

    def _write(self, **kw) -> dict:
        acct = {
            'uid': kw.get('uid', '123'),
            'access_token': 'tok',
            'refresh_token': 'rt',
            'expires_at': 9999999999,
            'domain': kw.get('domain', ''),
            'realm': kw.get('realm'),
            'nickname': 'N',
            'enterprise_id': '',
        }
        name, _ = tencent.write_auth_file(acct)
        return json.loads((config.AUTH_DIR / name).read_text(encoding='utf-8'))

    def test_realm_written_into_auth_object(self) -> None:
        d = self._write(realm='global', domain='www.workbuddy.ai')
        self.assertEqual(d['auth']['realm'], 'global')
        # 与 domain 同级（上游就是从这里读的）
        self.assertIn('domain', d['auth'])
        self.assertNotIn('realm', d)

    def test_domain_inferred_when_realm_missing(self) -> None:
        d = self._write(domain='www.workbuddy.ai')
        self.assertEqual(d['auth']['realm'], 'global')

    def test_cn_default(self) -> None:
        d = self._write(domain='www.codebuddy.cn')
        self.assertEqual(d['auth']['realm'], 'cn')

    def test_write_not_affected_by_escape_hatch(self) -> None:
        """逃生门开着时也不能把国际版账号写成 cn（上游专门警告过）。"""
        with mock.patch.object(realm, '_read_global_config',
                               return_value={'enabled': False, 'chat_base': '', 'billing_base': ''}):
            d = self._write(realm='global', domain='www.workbuddy.ai')
        self.assertEqual(d['auth']['realm'], 'global')


class GlobalConfigSectionTest(unittest.TestCase):
    """global 段此前不在写入白名单里，保存会被静默吞掉。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / 'config.json'
        self._path.write_text(json.dumps({'upstream': {}, 'server': {}}), encoding='utf-8')
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self._path
        realm.invalidate()

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        realm.invalidate()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _save(self, patch: dict) -> dict:
        from server.services import wb2api
        return wb2api.save_upstream_config(patch)

    def test_global_section_is_persisted(self) -> None:
        self._save({'global': {'enabled': True, 'chat_base': 'https://intl.example.com/'}})
        saved = json.loads(self._path.read_text(encoding='utf-8'))
        self.assertIn('global', saved, 'global 段被静默丢弃了')
        # 归一化：去掉尾部斜杠
        self.assertEqual(saved['global']['chat_base'], 'https://intl.example.com')

    def test_invalid_values_rejected(self) -> None:
        for bad, why in [
            ({'enabled': 'yes'}, 'enabled 必须是布尔'),
            ({'chat_base': 'not-a-url'}, '需以 http 开头'),
            ({'chat_base': 'http://a\nb'}, '不能含控制字符'),
        ]:
            with self.assertRaises(ValueError, msg=why):
                self._save({'global': bad})

    def test_saving_invalidates_realm_cache(self) -> None:
        """改完 base 应立即生效，而不是等 10 秒缓存过期。"""
        self._save({'global': {'chat_base': 'https://intl.example.com'}})
        self.assertEqual(realm.chat_base('global'), 'https://intl.example.com')


if __name__ == '__main__':
    unittest.main()

"""出站请求头的端到端核对：管理端直连腾讯 vs 上游参照实现。

为什么要单写这个文件：管理端有一批请求**绕过 workbuddy2api 直连腾讯**
（扫码登录、签到、查积分、地区注册、trial、探测）。上游 Go 侧有一套头构造
（CommonHeaders / ChatHeaders / BillingHeaders），管理端这套必须与之同形 ——
否则这批流量会成为唯一「不像官方客户端」的请求，风控挑出来的代价是封号。

这些缺口**不会报错**：接口照样返回 200，只是形态不对。所以只能靠本文件
把「每个端点该带哪些头」逐条钉死。

上游参照位置（2026-09-14，commit 6cee564c）：
  * internal/upstream/headers.go  CommonHeaders / ChatHeaders / BillingHeaders
  * internal/upstream/trial_test.go  断言 trial 必须带 X-User-Id
  * scripts/global_region.py         地区注册的请求体形状（注释标注「实测」）
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import realm, tencent  # noqa: E402


class FakeResp:
    """最小 httpx.Response 替身：把请求原样记下来供断言。"""

    def __init__(self, payload=None, status_code=200) -> None:
        self._payload = payload if payload is not None else {'code': 0, 'data': {}}
        self.status_code = status_code

    def json(self):
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    async def aread(self) -> bytes:
        return self.text.encode()

    async def aclose(self) -> None:
        return None


class RecordingClient:
    """记录出站请求（method/url/json/headers）的 httpx.AsyncClient 替身。"""

    calls: list = []

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        RecordingClient.calls.append(('POST', url, kw))
        return FakeResp()

    async def get(self, url, **kw):
        RecordingClient.calls.append(('GET', url, kw))
        return FakeResp()

    def stream(self, method, url, **kw):
        RecordingClient.calls.append((method, url, kw))
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                return FakeResp({'code': 0, 'data': {'choices': []}}, 200)

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()

    async def aclose(self) -> None:
        return None


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        RecordingClient.calls = []
        self._p = mock.patch.object(config, 'http_client', RecordingClient)
        self._p.start()
        self.addCleanup(self._p.stop)
        realm.invalidate()

    def _last(self):
        self.assertTrue(RecordingClient.calls, '没有任何出站请求')
        return RecordingClient.calls[-1]


class BillingHeaderTest(_Base):
    """billing 域（签到/积分/trial/注册）必须带身份头 —— 对齐上游 BillingHeaders。

    上游 Go 测试明确断言 trial 请求携带 X-User-Id（trial_test.go:46），
    签到与查积分同理。此前管理端这条路只发通用头，缺全部身份头。
    """

    AUTH = {
        'access_token': 'TOK',
        'uid': 'u123',
        'enterprise_id': 'ent-9',
        'domain': 'copilot.tencent.com',
        'realm': 'cn',
    }

    def test_checkin_carries_identity_headers(self) -> None:
        code, msg = _run(tencent.checkin(self.AUTH, 'cn'))
        self.assertEqual(code, 0, msg)
        _, url, kw = self._last()
        h = kw['headers']
        self.assertIn('/billing/meter/daily-checkin', url)
        self.assertEqual(h.get('X-User-Id'), 'u123')
        self.assertEqual(h.get('X-Enterprise-Id'), 'ent-9')
        self.assertEqual(h.get('X-Tenant-Id'), 'ent-9', '上游企业账号发两个同值头')
        self.assertEqual(h.get('X-Domain'), 'copilot.tencent.com')
        self.assertEqual(h.get('Authorization'), 'Bearer TOK')
        self.assertEqual(h.get('X-CodeBuddy-Request'), '1', 'D1 风控闸门头')

    def test_billing_ua_is_single_segment(self) -> None:
        """billing 域 UA 是**单段** WorkBuddy/<ver>（官方白名单接口的覆写形态）。

        上游 2026-09-14 起默认如此；三段式（带 CLI 段）是 chat 域的形态。
        """
        _run(tencent.checkin(self.AUTH, 'cn'))
        ua = self._last()[2]['headers'].get('User-Agent', '')
        self.assertTrue(ua.startswith('WorkBuddy/'), ua)
        self.assertNotIn('CLI/', ua, 'billing 域不该带 CLI 段')
        self.assertNotIn(' ', ua, '单段 UA 不含空格')

    def test_saas_client_name_opts_out(self) -> None:
        """显式 client_name="SaaS" 还原旧行为：**不设** UA（交给 HTTP 客户端默认）。

        上游此时 req.Header 里就没有 User-Agent（billingUA 返回空）。我们跟着
        把通用头里的 UA 摘掉，否则会发出「三段式（chat 形态）UA 打到 billing 域」
        这种两边都不是的形态。
        """
        with mock.patch.object(realm, '_read_upstream_sec', return_value={'client_name': 'SaaS'}):
            realm.invalidate()
            self.assertEqual(realm.billing_ua(), '')
            h = realm.billing_headers('cn', {'access_token': 't'})
        self.assertNotIn('User-Agent', h, 'SaaS 模式下 billing 域不设 UA（对齐上游）')

    def test_fetch_credits_carries_identity_headers(self) -> None:
        # AUTH 带 enterprise_id：企业额度走专用端点（PR #227），个人路径零改动。
        _run(tencent.fetch_credits(self.AUTH))
        _, url, kw = self._last()
        h = kw['headers']
        self.assertIn('get-enterprise-user-usage', url)
        self.assertEqual(h.get('X-User-Id'), 'u123')
        self.assertEqual(h.get('X-Enterprise-Id'), 'ent-9')
        self.assertEqual(h.get('X-Domain'), 'copilot.tencent.com')

    def test_trial_carries_user_id(self) -> None:
        auth = dict(self.AUTH, realm='global', domain='www.workbuddy.ai')
        _run(tencent.claim_trial(auth))
        _, url, kw = self._last()
        self.assertIn('/billing/ide/trial', url)
        self.assertEqual(kw['headers'].get('X-User-Id'), 'u123',
                         '上游 trial_test.go 断言必须携带 X-User-Id')

    def test_identity_headers_absent_when_unknown(self) -> None:
        """字段缺失时不发空头 —— 上游也是「非空才发」。"""
        _run(tencent.checkin({'access_token': 'TOK'}, 'cn'))
        h = self._last()[2]['headers']
        for k in ('X-User-Id', 'X-Enterprise-Id', 'X-Tenant-Id', 'X-Domain'):
            self.assertNotIn(k, h, f'{k} 不该以空值发出')

    def test_accept_language_follows_realm(self) -> None:
        _run(tencent.checkin(self.AUTH, 'cn'))
        self.assertEqual(self._last()[2]['headers'].get('Accept-Language'), 'zh-CN')


class RegionSubmissionTest(_Base):
    """地区注册的请求体形状 —— 照上游参照实现（scripts/global_region.py）。

    上游注释标注「实测」的形状：
        {"attributes": {"countryCode": [Code],
                        "countryFullName": [EnName],
                        "countryName": [IOS2]}}
    三个字段**取值不同源**。此前我们少了 attributes 外层，且把三者都填成 IOS2，
    会把地区归属写歪（接口通常仍返回 200，所以不会自己暴露）。
    """

    AUTH = {'access_token': 'TOK', 'uid': 'u1', 'realm': 'global',
            'domain': 'www.workbuddy.ai'}

    def _country_list(self):
        inner = {'code': 0, 'data': {'list': [
            {'IOS2': 'HK', 'IOS3': 'HKG', 'Code': '810000', 'EnName': 'China Hong Kong'},
            {'IOS2': 'SG', 'IOS3': 'SGP', 'Code': '702000', 'EnName': 'Singapore'},
        ]}}
        # 上游这一层的 data 是**内嵌 JSON 字符串**（脚本里显式 json.loads）
        return {'code': 0, 'data': json.dumps(inner)}

    def test_body_shape_matches_reference(self) -> None:
        with mock.patch.object(tencent, '_region_fields',
                               return_value=('HK', 'China Hong Kong', '810000')):
            ok, msg = _run(tencent.submit_region(self.AUTH, 'HK'))
        self.assertTrue(ok, msg)
        _, url, kw = self._last()
        self.assertIn('/console/login/account', url)
        body = kw['json']
        self.assertIn('attributes', body,
                      '缺 attributes 外层 —— 上游参照实现是 {"attributes": {...}}')
        attrs = body['attributes']
        self.assertEqual(attrs['countryName'], ['HK'])
        self.assertEqual(attrs['countryFullName'], ['China Hong Kong'])
        self.assertEqual(attrs['countryCode'], ['810000'],
                         'countryCode 是数字码，不是 IOS2')

    def test_region_fields_parses_nested_json(self) -> None:
        """地区列表的 data 是内嵌 JSON 字符串，解析要穿透一层。"""
        with mock.patch.object(config, 'http_client',
                               _ListClient(self._country_list())):
            ios2, en, code = _run(tencent._region_fields('HK'))
        self.assertEqual((ios2, en, code), ('HK', 'China Hong Kong', '810000'))

    def test_region_fields_falls_back_without_lying(self) -> None:
        """查不到就退回 IOS2 —— 宁可让上游拒绝，也不瞎猜一个数字码。"""
        with mock.patch.object(config, 'http_client', RecordingClient):
            ios2, en, code = _run(tencent._region_fields('XX'))
        self.assertEqual((ios2, en, code), ('XX', 'XX', 'XX'))


class _ListClient:
    def __init__(self, payload) -> None:
        self._payload = payload

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        return FakeResp(self._payload)

    async def aclose(self):
        return None


class DeviceTokenTest(_Base):
    """X-Device-Token 三级回退：auth 每号 > config 全局 > 文件（对齐上游）。"""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = Path(__file__).resolve().parent / '_dt_tmp'
        self._tmp.mkdir(exist_ok=True)
        self.addCleanup(lambda: [p.unlink() for p in self._tmp.glob('*')])

    def _patch_cfg(self, up: dict):
        p = mock.patch.object(realm, '_read_upstream_sec', return_value=up)
        p.start()
        self.addCleanup(p.stop)

    def test_priority_auth_over_config_over_file(self) -> None:
        f = self._tmp / 'dt.txt'
        f.write_text('FROM_FILE', encoding='utf-8')
        self._patch_cfg({'device_token': 'FROM_CONFIG', 'device_token_file': str(f)})
        realm.invalidate()
        self.assertEqual(realm.device_token_for({'device_token': 'PER_ACCOUNT'}), 'PER_ACCOUNT')
        # 每号为空 → 落到 config 全局
        realm.invalidate()
        self.assertEqual(realm.device_token_for({'device_token': ''}), 'FROM_CONFIG')

    def test_file_fallback_and_size_limit(self) -> None:
        f = self._tmp / 'dt2.txt'
        f.write_text('FILE_TOK', encoding='utf-8')
        self._patch_cfg({'device_token': '', 'device_token_file': str(f)})
        realm.invalidate()
        self.assertEqual(realm.device_token_for({}), 'FILE_TOK')
        # 超过 1KB 视为没有（上游 deviceTokenFileMaxLen = 1024）
        big = self._tmp / 'dt_big.txt'
        big.write_text('x' * 2000, encoding='utf-8')
        self._patch_cfg({'device_token': '', 'device_token_file': str(big)})
        realm.invalidate()
        self.assertEqual(realm.device_token_for({}), '')

    def test_missing_everything_is_empty_not_error(self) -> None:
        self._patch_cfg({})
        realm.invalidate()
        self.assertEqual(realm.device_token_for({}), '')
        self.assertEqual(realm.device_token_for(None), '')

    def test_headers_include_device_token_when_present(self) -> None:
        self._patch_cfg({'device_token': 'DT'})
        realm.invalidate()
        h = realm.billing_headers('cn', {'access_token': 't', 'uid': 'u'})
        self.assertEqual(h.get('X-Device-Token'), 'DT')
        # 空时不发头（上游语义：空则不注入）
        self._patch_cfg({})
        realm.invalidate()
        h2 = realm.billing_headers('cn', {'access_token': 't'})
        self.assertNotIn('X-Device-Token', h2)

    def test_device_token_never_logged_or_returned(self) -> None:
        """它是凭据：不出现在任何返回结构里（只进请求头）。"""
        self._patch_cfg({'device_token': 'SECRET_DT'})
        realm.invalidate()
        h = realm.billing_headers('cn', {'access_token': 't', 'uid': 'u'})
        self.assertIn('X-Device-Token', h)
        # realm.headers（通用头，会被日志/调试打印的那套）不得含它
        plain = realm.headers('cn', 't')
        self.assertNotIn('X-Device-Token', plain)
        self.assertNotIn('SECRET_DT', json.dumps(plain))


class AttributionFingerprintTest(_Base):
    """chat 路径的用量归属头：默认伪造官方桌面端指纹。

    上游 2026-09-14 起把默认从「X-Product=SaaS、不设 X-IDE-*」**翻转**为
    「X-Agent-Purpose=conversation + X-IDE-* 四头」，理由是空的 client/agentPurpose
    在官网用量归因里是显眼的「网关特征」。我们的 probe 此前停留在旧行为。
    """

    AUTH = {'access_token': 'TOK', 'uid': 'u1', 'realm': 'cn'}

    def _patch_cfg(self, up: dict):
        p = mock.patch.object(realm, '_read_upstream_sec', return_value=up)
        p.start()
        self.addCleanup(p.stop)
        realm.invalidate()

    def test_default_fingerprint_matches_desktop(self) -> None:
        self._patch_cfg({})  # 未配置 client_name
        h = realm.attribution_headers()
        self.assertEqual(h.get('X-Agent-Purpose'), 'conversation')
        self.assertEqual(h.get('X-IDE-Name'), 'WorkBuddy')
        self.assertEqual(h.get('X-IDE-Type'), 'WorkBuddy')
        self.assertEqual(h.get('X-Product'), 'WorkBuddy')
        self.assertTrue(h.get('X-IDE-Version'), 'X-IDE-Version 应是客户端版本段')

    def test_saas_restores_old_behavior(self) -> None:
        """显式配 SaaS 时只有 X-Product=SaaS、不设 X-IDE-*（还原旧行为）。"""
        self._patch_cfg({'client_name': 'SaaS'})
        h = realm.attribution_headers()
        self.assertEqual(h, {'X-Product': 'SaaS'})

    def test_custom_name_follows_value(self) -> None:
        self._patch_cfg({'client_name': 'MyClient'})
        h = realm.attribution_headers()
        self.assertEqual(h.get('X-Product'), 'MyClient')
        self.assertEqual(h.get('X-IDE-Name'), 'MyClient')

    def test_probe_sends_attribution_headers(self) -> None:
        """探测请求带上完整归属头组（此前只有 X-Product=SaaS）。"""
        self._patch_cfg({})
        _run(tencent.probe_account(self.AUTH, 'glm-5.2'))
        h = self._last()[2]['headers']
        self.assertEqual(h.get('X-Agent-Purpose'), 'conversation')
        self.assertEqual(h.get('X-IDE-Name'), 'WorkBuddy')
        self.assertEqual(h.get('X-IDE-Version'), realm.client_version())


class ExpiringSoonFieldTest(unittest.TestCase):
    """pool.expiring_soon 的校验与会话：新增字段不能因为白名单不认识被静默丢弃。

    它与普通时长字段有一处语义差异：**空串与 "0" 是「禁用」而不是非法值**。
    """

    def test_duration_accepted(self) -> None:
        from server.services import wb2api
        for val in ('168h', '30m'):
            out = wb2api._sanitize_section('pool', {'expiring_soon': val})
            self.assertEqual(out['expiring_soon'], val)

    def test_day_suffix_rejected(self) -> None:
        """上游是 Go time.ParseDuration，不认 `d`；面板保存前必须拦下。"""
        from server.services import wb2api
        for val in ('7d', '1d'):
            with self.assertRaises(ValueError):
                wb2api._sanitize_section('pool', {'expiring_soon': val})

    def test_empty_and_zero_mean_disabled(self) -> None:
        from server.services import wb2api
        for val in ('', '0'):
            out = wb2api._sanitize_section('pool', {'expiring_soon': val})
            self.assertEqual(out['expiring_soon'], val,
                             '空/0 表示禁用，不该被拒绝也不该被改写')

    def test_garbage_rejected(self) -> None:
        from server.services import wb2api
        with self.assertRaises(ValueError):
            wb2api._sanitize_section('pool', {'expiring_soon': '一星期'})


class WriteAuthFilePreservesDeviceTokenTest(unittest.TestCase):
    """重新登录不能把用户手写的 device_token 冲掉。

    落盘是**整体覆盖**：若不复用旧值，用户配置的设备风控凭据会在下次换 token
    重登时静默消失 —— 之后所有 billing 请求都降级成「没有设备标识」的形态，
    而接口依然 200，没人会发现。
    """

    def setUp(self) -> None:
        import tempfile
        self.dir = Path(tempfile.mkdtemp())
        p = mock.patch.object(config, 'AUTH_DIR', self.dir)
        p.start()
        self.addCleanup(p.stop)

    def test_existing_device_token_survives_relogin(self) -> None:
        target = self.dir / 'workbuddy-u9.json'
        target.write_text(json.dumps({
            'account': {'uid': 'u9'},
            'auth': {'accessToken': 'old'},
            'device_token': 'MY_DEVICE_TOKEN',
        }, ensure_ascii=False), encoding='utf-8')

        tencent.write_auth_file({
            'uid': 'u9', 'access_token': 'new-token', 'refresh_token': 'r',
            'expires_at': 123, 'domain': '', 'realm': 'cn',
            'enterprise_id': '', 'nickname': 'n',
        })
        data = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(data.get('device_token'), 'MY_DEVICE_TOKEN',
                         '重登把 device_token 冲掉了 —— 风控形态会静默降级')
        self.assertEqual(data['auth']['accessToken'], 'new-token', 'token 应已更新')

    def test_new_account_has_no_device_token_key(self) -> None:
        """没有旧值时不引入空键（与上游「非空才写」一致）。"""
        tencent.write_auth_file({
            'uid': 'u10', 'access_token': 't', 'refresh_token': 'r',
            'expires_at': 1, 'domain': '', 'realm': 'cn',
            'enterprise_id': '', 'nickname': 'n',
        })
        data = json.loads((self.dir / 'workbuddy-u10.json').read_text(encoding='utf-8'))
        self.assertNotIn('device_token', data)


if __name__ == '__main__':
    unittest.main()


class DeviceFingerprintHeaderTest(_Base):
    """X-Machine-ID / X-Session-ID：按账号稳定派生的设备指纹头。

    上游 2026-09-15 新增（deriveAccountStableID），语义是「每账号一台固定虚拟
    设备」——跨重启恒定、账号间互异，对齐官方桌面端，防多号被按设备指纹缺失
    或漂移关联风控。

    我们直连腾讯的那批请求（扫码登录/签到/积分/注册/trial/探测）必须同样携带：
    上游给它的出站加了这两个头，我们这条路不加，就成了唯一「没有设备标识」
    的流量 —— 与 D1/D5 那批风控头是同一个道理。

    派生必须与上游**逐字一致**（固定盐 "wb2a:"、sha256 前 18 字节 hex）：
    否则同一账号在「经上游」与「直连」两条路上会是两台设备，反而制造出
    可被关联的异常。
    """

    UID = '76a5e629-dcdf-4c62-8607-e2de2571e26c'

    def _expect(self, purpose: str) -> str:
        import hashlib
        return hashlib.sha256(f'wb2a:{purpose}:{self.UID}'.encode()).hexdigest()[:36]

    def test_derivation_matches_upstream(self) -> None:
        h = realm.device_fingerprint_headers(self.UID)
        self.assertEqual(h['X-Machine-ID'], self._expect('machine'))
        self.assertEqual(h['X-Session-ID'], self._expect('session'))
        # 上游截 18 字节 → 36 hex
        self.assertEqual(len(h['X-Machine-ID']), 36)

    def test_accounts_are_isolated(self) -> None:
        a = realm.device_fingerprint_headers('uid-A')
        b = realm.device_fingerprint_headers('uid-B')
        self.assertNotEqual(a['X-Machine-ID'], b['X-Machine-ID'])
        self.assertNotEqual(a['X-Session-ID'], b['X-Session-ID'])

    def test_purposes_are_salted_apart(self) -> None:
        """machine 与 session 必须是不同值（用途盐隔离）。"""
        h = realm.device_fingerprint_headers(self.UID)
        self.assertNotEqual(h['X-Machine-ID'], h['X-Session-ID'])

    def test_idempotent(self) -> None:
        """同 uid 恒同值 —— 跨重启稳定是它存在的意义。"""
        self.assertEqual(realm.device_fingerprint_headers(self.UID),
                         realm.device_fingerprint_headers(self.UID))

    def test_empty_uid_injects_nothing(self) -> None:
        for v in ('', '   ', None):
            self.assertEqual(realm.device_fingerprint_headers(v), {},
                             '匿名请求没有设备可言，不应注入')

    # ── 接线：两条出站路径都要带上 ──

    def test_billing_headers_include_fingerprint(self) -> None:
        h = realm.billing_headers('cn', {'access_token': 't', 'uid': self.UID})
        self.assertEqual(h.get('X-Machine-ID'), self._expect('machine'))
        self.assertEqual(h.get('X-Session-ID'), self._expect('session'))

    def test_generic_headers_include_fingerprint_when_uid_given(self) -> None:
        h = realm.headers('cn', 't', self.UID)
        self.assertEqual(h.get('X-Machine-ID'), self._expect('machine'))

    def test_checkin_request_carries_fingerprint(self) -> None:
        """端到端：签到请求（billing 域）实际带上这两个头。"""
        _run(tencent.checkin({'access_token': 'TOK', 'uid': self.UID}, 'cn'))
        h = self._last()[2]['headers']
        self.assertEqual(h.get('X-Machine-ID'), self._expect('machine'),
                         '签到请求没带设备指纹 —— 会被当作异常流量')
        self.assertEqual(h.get('X-Session-ID'), self._expect('session'))

    def test_probe_request_carries_fingerprint(self) -> None:
        """端到端：探测请求（chat 域）也要带。"""
        _run(tencent.probe_account({'access_token': 'TOK', 'uid': self.UID,
                                    'realm': 'cn'}, 'glm-5.2'))
        h = self._last()[2]['headers']
        self.assertEqual(h.get('X-Machine-ID'), self._expect('machine'))

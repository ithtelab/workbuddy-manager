"""余额协议端点：客户端面板据此显示密钥剩余额度。

钉住三件事：
  1. 单位是公开元数据、额度是鉴权数据 —— `/api/status` 不含任何额度，
     所以它无鉴权可达；数值只从 `/api/usage/token/` 出。
  2. 三档口径各自的量纲与算式（双额度取更紧的比例、单额度报剩余、都不设限
     报账号池总额），以及小数保留两位。
  3. 停用 / 过期 / 用尽的密钥**仍能查到余额**：那正是最需要它的时刻。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, security  # noqa: E402
from server.routers import balance  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / 'm.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        config.AUTH_DIR = Path(self._tmp.name) / 'auths'
        config.STATIC_DIR = Path(self._tmp.name) / 'no-static'
        db._conn = None
        db.connect()
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('p')}], 'api_keys': []})
        balance._pool_cache.clear()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        self._tmp.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        from server.main import app
        return TestClient(app)

    def _make_key(self, **overrides) -> tuple[str, dict]:
        client = self._client()
        assert client.post('/api/login', json={'username': 'admin',
                                               'password': 'p'}).status_code == 200
        body = {'name': '余额测试', 'quota': 0, 'quota_credit': 0}
        body.update(overrides)
        r = client.post('/api/keys', json=body)
        assert r.status_code == 200, r.text
        created = r.json()
        return created['key'], created

    def _usage(self, token: str) -> dict:
        r = self._client().get('/api/usage/token/',
                               headers={'Authorization': f'Bearer {token}'})
        return {'status': r.status_code, 'body': r.json()}

    def _spend(self, key_id: int, *, credit: float = 0, tokens: int = 0) -> None:
        db.execute('UPDATE api_keys SET used_credit = ?, used_tokens = ? WHERE id = ?',
                   (credit, tokens, key_id))


class StatusMetadataTest(_Base):
    def test_public_and_declares_unit(self) -> None:
        """无鉴权可达，且只声明单位（字段不全时客户端会退化成 QUOTA）。"""
        r = self._client().get('/api/status')
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIs(data['success'], True)
        self.assertEqual(data['data']['quota_display_type'], 'CUSTOM')
        self.assertEqual(data['data']['custom_currency_symbol'], 'UNIT')
        self.assertEqual(data['data']['quota_per_unit'], 1)
        self.assertEqual(data['data']['custom_currency_exchange_rate'], 1)

    def test_previous_upstream_endpoint_moved(self) -> None:
        """原 `/api/status`（上游状态）已迁到 `/api/upstream/status` 并要求登录。

        不迁走的话它会把公开元数据端点抢掉：路由按注册顺序匹配，先注册者胜。
        """
        self.assertIn(self._client().get('/api/upstream/status').status_code, (401, 403))


class AuthTest(_Base):
    def test_missing_and_invalid_key_rejected(self) -> None:
        c = self._client()
        self.assertEqual(c.get('/api/usage/token/').status_code, 401)
        self.assertEqual(c.get('/api/usage/token/',
                               headers={'Authorization': 'Bearer sk-nope'}).status_code, 401)


class AmountRuleTest(_Base):
    """三档口径的算式。直接测纯函数，避免为每个用例造库。"""

    def test_both_limits_take_tighter_ratio(self) -> None:
        """积分剩 50%、token 剩 30% → 30% × 积分上限 100 = 30。"""
        key = {'quota_credit': 100, 'used_credit': 50,
               'quota': 1_000_000_000, 'used_tokens': 700_000_000}
        self.assertEqual(balance._amounts(key, 0.0), (30.0, 70.0, 100.0))

    def test_credit_limit_only(self) -> None:
        key = {'quota_credit': 100.5, 'used_credit': 12.34, 'quota': 0, 'used_tokens': 0}
        self.assertEqual(balance._amounts(key, 0.0), (88.16, 12.34, 100.5))

    def test_token_limit_only_scaled_to_millions(self) -> None:
        key = {'quota_credit': 0, 'used_credit': 0,
               'quota': 2_000_000, 'used_tokens': 500_000}
        self.assertEqual(balance._amounts(key, 0.0), (1.5, 0.5, 2.0))

    def test_unlimited_falls_back_to_pool_credits(self) -> None:
        key = {'quota_credit': 0, 'used_credit': 0, 'quota': 0, 'used_tokens': 0}
        self.assertEqual(balance._amounts(key, 1234.567), (1234.57, 0.0, 1234.57))

    def test_overspent_clamps_to_zero(self) -> None:
        """超支按 0 计，不能回负数（客户端会把它当成非法响应）。

        已用如实保留成 150 而不是钳到 100：一次请求冲破上限是真实状态，
        抹平它会让用户以为「刚好用完」。客户端只约束 used 非负。
        """
        key = {'quota_credit': 100, 'used_credit': 150, 'quota': 0, 'used_tokens': 0}
        self.assertEqual(balance._amounts(key, 0.0), (0.0, 150.0, 100.0))

    def test_dirty_numbers_do_not_raise(self) -> None:
        key = {'quota_credit': 'x', 'used_credit': None, 'quota': -5, 'used_tokens': float('nan')}
        self.assertEqual(balance._amounts(key, 0.0), (0.0, 0.0, 0.0))


class EndpointTest(_Base):
    def test_both_limits_endpoint(self) -> None:
        token, created = self._make_key(quota=1_000_000_000, quota_credit=100)
        self._spend(created['id'], credit=50, tokens=700_000_000)
        got = self._usage(token)
        self.assertEqual(got['status'], 200)
        data = got['body']['data']
        self.assertEqual(data['object'], 'token_usage')
        self.assertIs(data['unlimited_quota'], False)
        self.assertEqual((data['total_available'], data['total_used'], data['total_granted']),
                         (30.0, 70.0, 100.0))

    def test_credit_only_endpoint(self) -> None:
        token, created = self._make_key(quota_credit=20)
        self._spend(created['id'], credit=4.5)
        data = self._usage(token)['body']['data']
        self.assertEqual((data['total_available'], data['total_used'], data['total_granted']),
                         (15.5, 4.5, 20.0))

    def test_token_only_endpoint(self) -> None:
        token, created = self._make_key(quota=3_000_000)
        self._spend(created['id'], tokens=1_200_000)
        data = self._usage(token)['body']['data']
        self.assertEqual((data['total_available'], data['total_used'], data['total_granted']),
                         (1.8, 1.2, 3.0))

    def test_unlimited_reads_pool_total(self) -> None:
        token, _ = self._make_key()

        async def fake_status(**_: object) -> dict:
            return {'connected': True, 'accounts': [
                {'uid': 'a', 'credits': 12.3}, {'uid': 'b', 'credits': 7.7},
                {'uid': 'c', 'credits': None},
            ]}

        with mock.patch('server.services.wb2api.get_status', fake_status):
            data = self._usage(token)['body']['data']
        self.assertEqual((data['total_available'], data['total_used'], data['total_granted']),
                         (20.0, 0.0, 20.0))

    def test_pool_total_cached_between_calls(self) -> None:
        """上游只该被问一次：轮询频繁，每轮穿透没有意义。"""
        token, _ = self._make_key()
        calls: list[int] = []

        async def fake_status(**_: object) -> dict:
            calls.append(1)
            return {'connected': True, 'accounts': [{'uid': 'a', 'credits': 5}]}

        with mock.patch('server.services.wb2api.get_status', fake_status):
            self._usage(token)
            self._usage(token)
        self.assertEqual(len(calls), 1)

    def test_pool_zero_falls_back_to_snapshot(self) -> None:
        """上游整池报 0 时按本地快照算 —— 这不是「钱花光了」。

        上游的 credits 只在它自己的定时任务里刷新，重启后 / 两次刷新之间会整池
        报 0；快照来自腾讯账单，更可信。线上就撞到过这个：池里 4 个号上游报 0，
        快照刚记到 22233，接口却吐了 0。
        """
        token, _ = self._make_key()

        async def zeros(**_: object) -> dict:
            return {'connected': True, 'accounts': [
                {'uid': 'a', 'credits': 0}, {'uid': 'b', 'credits': 0}]}

        with mock.patch('server.services.wb2api.get_status', zeros), \
                mock.patch('server.services.credits.snapshot_entries',
                           lambda: {'accounts': [{'uid': 'a', 'credits': 4000},
                                                 {'uid': 'b', 'credits': 6000}]}):
            data = self._usage(token)['body']['data']
        self.assertEqual(data['total_available'], 10000.0)

    def test_snapshot_ignores_accounts_outside_pool(self) -> None:
        """快照不会随账号删除而清理，统计时必须按池内账号过滤。"""
        token, _ = self._make_key()

        async def zeros(**_: object) -> dict:
            return {'connected': True, 'accounts': [{'uid': 'a', 'credits': 0}]}

        with mock.patch('server.services.wb2api.get_status', zeros), \
                mock.patch('server.services.credits.snapshot_entries',
                           lambda: {'accounts': [{'uid': 'a', 'credits': 100},
                                                 {'uid': '已删除的号', 'credits': 999}]}):
            data = self._usage(token)['body']['data']
        self.assertEqual(data['total_available'], 100.0)

    def test_pool_failure_falls_back_to_snapshot(self) -> None:
        """上游不可用时用已登记的余额快照兜底，而不是报 0 或 500。"""
        token, _ = self._make_key()

        async def offline(**_: object) -> dict:
            return {'connected': False, 'error': 'connection refused'}

        with mock.patch('server.services.wb2api.get_status', offline), \
                mock.patch('server.services.credits.snapshot_entries',
                           lambda: {'accounts': [{'uid': 'a', 'credits': 6.4}]}):
            data = self._usage(token)['body']['data']
        self.assertEqual(data['total_available'], 6.4)

    def test_disabled_and_expired_key_still_readable(self) -> None:
        """调不动的时候最需要看余额，所以状态不拦这个只读端点。"""
        token, created = self._make_key(quota_credit=10)
        keysvc.update_key(created['id'], {'enabled': False, 'expires_at': 1})
        got = self._usage(token)
        self.assertEqual(got['status'], 200)
        self.assertEqual(got['body']['data']['total_available'], 10.0)

    def test_exhausted_key_reports_zero(self) -> None:
        token, created = self._make_key(quota_credit=10)
        self._spend(created['id'], credit=10)
        got = self._usage(token)
        self.assertEqual(got['status'], 200)
        self.assertEqual(got['body']['data']['total_available'], 0.0)

    def test_bound_upstream_missing_does_not_break(self) -> None:
        """绑定的上游被删了也一样给数：这时更该看得到池里还剩多少。"""
        token, created = self._make_key()
        db.execute('UPDATE api_keys SET upstream_id = 999 WHERE id = ?', (created['id'],))
        with mock.patch('server.services.credits.snapshot_entries',
                        lambda: {'accounts': [{'uid': 'a', 'credits': 3}]}):
            got = self._usage(token)
        self.assertEqual(got['status'], 200)
        self.assertEqual(got['body']['data']['total_available'], 3.0)

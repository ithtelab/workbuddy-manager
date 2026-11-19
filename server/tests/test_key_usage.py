"""密钥自查用量（issue #137）：拿密钥查自己还剩多少。

需求方的用法：在自己的小工具里调一次查询接口，就知道这把密钥的已用量与剩余额度，
**不用登录管理后台，也不额外扩大权限范围**。

这里钉住四件事：
  1. 只回**这把密钥自己**的数字（拿别人的密钥查不到）；
  2. **一次上游请求都不发**（用「调用即失败」的桩钉住：查用量不该被计成一次调用）；
  3. 调不动的时候更要能查：额度用尽 / 已过期 / 被停用的密钥仍能读到自己的状态与数字，
     而不是再吃一个 401（那正是用户最需要信息的时候）；
  4. 返回里写清「这是面板已记录的值」（`data_source` / `snapshot_at`），不冒充实时。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, security  # noqa: E402


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
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        self._tmp.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        from server.main import app
        c = TestClient(app)
        assert c.post('/api/login', json={'username': 'admin',
                                          'password': 'p'}).status_code == 200
        return c

    def _make_key(self, **overrides) -> str:
        body = {'name': '自查用的钥匙', 'quota': 1000, 'quota_credit': 50}
        body.update(overrides)
        r = self._client().post('/api/keys', json=body)
        assert r.status_code == 200, r.text
        # 创建接口把明文放在 `key` 字段里（库里只有哈希，仅此一次返回）
        return r.json()['key']


class KeyUsageEndpointTest(_Base):
    def test_reports_own_used_and_remaining(self) -> None:
        token = self._make_key()
        prefix = token[:12]
        db.execute('UPDATE api_keys SET used_tokens = 250, used_credit = 12, '
                   'last_used_at = ? WHERE prefix = ?', (int(time.time()) - 30, prefix))

        with mock.patch.object(config, 'http_client', side_effect=AssertionError('不该问上游')):
            r = self._client().get('/v1/usage', headers={'Authorization': f'Bearer {token}'})

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['tokens'], {'used': 250, 'limit': 1000, 'remaining': 750})
        self.assertEqual(body['credits'], {'used': 12, 'limit': 50, 'remaining': 38})
        self.assertEqual(body['data_source'], 'panel-recorded')
        self.assertGreaterEqual(body['snapshot_at'], int(time.time()) - 5)
        self.assertGreater(body['last_used_at'], 0, '应带回最后一次使用时刻')

    def test_unlimited_quota_reports_none_not_zero(self) -> None:
        """不限额要显示「不限」，不能糊成「剩余 0」——那会让人以为已经用完了。"""
        token = self._make_key(quota=0, quota_credit=0)
        r = self._client().get('/v1/usage', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIsNone(body['tokens']['limit'])
        self.assertIsNone(body['tokens']['remaining'])
        self.assertIsNone(body['credits']['remaining'])

    def test_exhausted_and_disabled_keys_can_still_read_their_state(self) -> None:
        """额度用尽 / 被停用：查用量仍要能读到数字与原因，而不是 401。"""
        token = self._make_key(quota=100)
        prefix = token[:12]
        db.execute('UPDATE api_keys SET used_tokens = 100, enabled = 0 WHERE prefix = ?', (prefix,))
        r = self._client().get('/v1/usage', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['tokens']['remaining'], 0)
        self.assertFalse(body['key']['enabled'], '被停用要如实说出，否则用户不知道为何调不动')

    def test_expired_key_is_flagged(self) -> None:
        token = self._make_key()
        prefix = token[:12]
        db.execute('UPDATE api_keys SET expires_at = ? WHERE prefix = ?',
                   (int(time.time()) - 10, prefix))
        body = self._client().get(
            '/v1/usage', headers={'Authorization': f'Bearer {token}'}).json()
        self.assertTrue(body['key']['expired'])

    def test_missing_or_invalid_key_is_rejected(self) -> None:
        client = self._client()
        missing = client.get('/v1/usage')
        self.assertEqual(missing.status_code, 401)
        self.assertIn('missing_api_key', missing.text)
        r = client.get('/v1/usage', headers={'Authorization': 'Bearer wbk_not-a-real-key'})
        self.assertEqual(r.status_code, 401)
        self.assertIn('invalid_api_key', r.text)

        # 这条路径**未鉴权可达**，两种认不出调用方的访问都要留在入站日志里，
        # 且原因可区分（复审补的审计缺口：此前整条路径一行都不记，
        # 「谁在用无效密钥扫 /v1/usage」在安全页上不存在）。
        rows = db.query('SELECT reason, blocked FROM ip_access_logs ORDER BY id')
        self.assertEqual([(x['reason'], x['blocked']) for x in rows],
                         [('missing_key', 1), ('invalid_key', 1)])

    def test_reading_usage_is_logged_but_costs_nothing(self) -> None:
        """查用量要进**请求日志**（否则监控轮询在运维眼里是隐形的），
        但**不花钱、不记账**：已用 token / 已用积分一律不动 —— 这两个数字是
        计费口径，只有真实调用才会推动它们。
        `last_used_at` 与「来源 IP」照记：与 `/v1/models`、面板 API 令牌同口径，
        「这把钥匙最近被谁用过」在安全页上是有效信号。"""
        token = self._make_key()
        prefix = token[:12]

        r = self._client().get('/v1/usage', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 200, r.text)

        row = db.query_one('SELECT last_used_at, used_tokens, used_credit '
                           'FROM api_keys WHERE prefix = ?', (prefix,))
        self.assertGreaterEqual(row['last_used_at'], int(time.time()) - 5,
                                '查用量应在「最近使用」上留痕，与模型列表页同口径')
        self.assertEqual(row['used_tokens'], 0, '查用量被计成了 token 消耗')
        self.assertEqual(row['used_credit'], 0, '查用量被计成了积分消耗')

        logs = db.query('SELECT status, prompt_tokens, completion_tokens '
                        'FROM request_logs ORDER BY id')
        self.assertEqual(len(logs), 1, '查用量的请求没进请求日志')
        self.assertEqual((logs[0]['status'], logs[0]['prompt_tokens'],
                          logs[0]['completion_tokens']), (200, 0, 0))

    def test_cannot_read_another_keys_numbers(self) -> None:
        mine = self._make_key(name='我的')
        theirs = self._make_key(name='别人的', quota=9999, quota_credit=9999)
        db.execute('UPDATE api_keys SET used_tokens = 9000, used_credit = 9000 '
                   'WHERE prefix = ?', (theirs[:12],))
        body = self._client().get(
            '/v1/usage', headers={'Authorization': f'Bearer {mine}'}).json()
        self.assertEqual(body['tokens']['used'], 0, '读到了别人的用量')
        self.assertEqual(body['tokens']['limit'], 1000, '限额也对成了别人的')


if __name__ == '__main__':
    unittest.main()

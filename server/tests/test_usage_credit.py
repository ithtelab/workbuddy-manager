"""调用扣费（上游 usage.credit）的采集、迁移与统计回归。

背景：上游 2026-09-13 起在末帧 usage 里带 credit（本次真实扣费），
管理端据此在请求日志与用量统计里展示「实付」。

两个容易出错的地方：
  1. 「上游没返回」与「扣了 0」必须区分——混为一谈会把未知当成免费；
  2. request_logs / usage_daily 都是已存在的表，SQLite 的
     CREATE TABLE IF NOT EXISTS 不会补列，必须显式迁移。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server import keysvc  # noqa: E402
from server.routers import gateway  # noqa: E402


_HASH_SEQ = 0


def _uniq_hash() -> str:
    """每次调用返回唯一 hash。

    `api_keys.key_hash` 有唯一索引（issue #447 防并发重复），而下面这些测试辅助
    会被同一用例调用多次、每次都插一行 —— 写死同一个字面量会撞索引。测试并不
    关心 hash 取什么值，唯一即可。
    """
    global _HASH_SEQ
    _HASH_SEQ += 1
    return f'h{int(__import__("time").time() * 1e6)}-{_HASH_SEQ}'



class KeyCreditQuotaTest(unittest.TestCase):
    """密钥的积分额度（issue #27）。

    需求原话：「现在限制 token 不太好估算，直接限制积分」。确实如此——同样 1M
    token，便宜模型与贵模型的扣费能差几十倍，按 token 限额估不出实际花了多少。

    数据基础是现成的：上游自 2026-09-13 起在末帧 usage 里带真实 credit，
    我们已按请求存进 request_logs.credit。这个额度只是把同一份数据**按密钥累计**。

    要点：
      · 与 token 额度**各自独立**（任一超限即拒绝），不是替代关系；
      · 0 = 不限（与 token 额度同口径），存量密钥升级后行为不变；
      · 上游没返回 credit 时**不计入**，不按 0 记（否则积分额度永远用不完）；
      · 界面的「超额」标记要与网关的拒绝口径一致，否则会出现
        「列表显示正常、调用却被 429」。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = pathlib.Path(self._tmp.name) / 'm.db'
        config.USERS_FILE = pathlib.Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        self._tmp.cleanup()

    def _key(self, **kw) -> dict:
        created = keysvc.create_key(name='k', realm='cn', **kw)
        return keysvc.resolve(created['key'])

    def _spend(self, key: dict, credit: float | None) -> None:
        keysvc.touch(key, '1.2.3.4', 1000, credit)

    def _fresh(self, key: dict) -> dict:
        """重新读一次密钥行。

        必须重读：`resolve` 返回的是**当时那一行的快照**，`touch` 写库不会
        回头改这个 dict。网关是每个请求重新 resolve 的，所以按快照判定就跟不上
        累计值 —— 测试里若不重读，会得到「花了钱却没超限」的假结论。
        """
        return next(x for x in keysvc.list_keys() if x['id'] == key['id'])

    def _rejection(self, key: dict) -> object | None:
        return keysvc.validate(self._fresh(key), {'id': key['id'], 'ip': '1.2.3.4'},
                               model='glm-5.2', is_model_list=False)

    def test_default_is_unlimited(self) -> None:
        """不设额度（0）= 不限，存量密钥升级后不受影响。"""
        k = self._key()
        self.assertEqual(k['quota_credit'], 0.0)
        self._spend(k, 999.0)
        self.assertIsNone(self._rejection(k))

    def test_quota_blocks_after_reaching_limit(self) -> None:
        k = self._key(quota_credit=100.5)
        self._spend(k, 30.0)
        self.assertIsNone(self._rejection(k), '未到上限不该拦')
        self._spend(k, 70.5)          # 正好到 100.5
        rej = self._rejection(k)
        self.assertIsNotNone(rej, '到上限就该拦')
        self.assertEqual(rej.code, 'credit_quota_exhausted')
        # 429 而非 403：403 会被客户端读成「密钥无效」，用户会去查密钥而不是调额度
        self.assertEqual(rej.status, 429)

    def test_expended_message_shows_numbers(self) -> None:
        """拒绝文案要带具体数字 —— 用户需要知道超了多少、该充多少。"""
        k = self._key(quota_credit=50)
        self._spend(k, 60.0)
        msg = str(self._rejection(k))
        self.assertIn('60', msg)
        self.assertIn('50', msg)

    def test_token_and_credit_quotas_are_independent(self) -> None:
        """两种额度互不影响，任一超限即拦。

        「互不影响」的含义是**判定各看各的**，不是「花了钱不看 token」——
        `touch` 同时累加两者，所以构造用例时要只让其中一个越线。
        """
        # ① 只设 token 额度：积分花再多也不触发积分判定（token 未越线）
        k1 = self._key(quota=1000)
        keysvc.touch(k1, '1.2.3.4', 0, 9999.0)
        self.assertIsNone(self._rejection(k1), '只设了 token 额度，不该按积分拦')

        # ② 只设积分额度：token 用得多（但没设 token 上限）也不拦
        k2 = self._key(quota_credit=10)
        keysvc.touch(k2, '1.2.3.4', 10_000_000, None)
        self.assertIsNone(self._rejection(k2), '没设 token 额度，不该按 token 拦')

        # ③ 积分越线 → 拦（且只拦积分那一条）
        self._spend(k2, 11.0)
        rej = self._rejection(k2)
        self.assertIsNotNone(rej)
        self.assertEqual(rej.code, 'credit_quota_exhausted')

    def test_missing_credit_is_not_counted(self) -> None:
        """上游没返回 credit 时不计入 —— 否则积分额度永远用不完。"""
        k = self._key(quota_credit=5)
        for _ in range(10):
            self._spend(k, None)
        self.assertEqual(self._fresh(k)['used_credit'], 0.0)
        self.assertIsNone(self._rejection(k))

    def test_zero_credit_is_not_counted(self) -> None:
        """credit=0 同样不计入（与 request_logs 的口径一致）。"""
        k = self._key(quota_credit=5)
        self._spend(k, 0)
        self.assertEqual(self._fresh(k)['used_credit'], 0.0)

    def test_reset_clears_both(self) -> None:
        """「重置用量」要把两种额度一起归零。

        只清 token 会留下看不见的积分残留，下次超额时用户会莫名
        （「我明明重置过」）。
        """
        k = self._key(quota=100, quota_credit=10)
        keysvc.touch(k, '1.2.3.4', 500, 8.0)
        self.assertTrue(keysvc.reset_usage(k['id']))
        row = self._fresh(k)
        self.assertEqual(row['used_tokens'], 0)
        self.assertEqual(row['used_credit'], 0.0)

    def test_dirty_quota_values_are_sanitized(self) -> None:
        """脏数据归一化成 0（不限），且**不能让保存失败**。"""
        for bad in (-5, 'abc', None, float('nan'), float('inf'), ''):
            with self.subTest(bad=bad):
                k = self._key(quota_credit=bad)
                self.assertEqual(k['quota_credit'], 0.0, repr(bad))

    def test_gateway_records_credit_into_key(self) -> None:
        """网关记账要把 credit 传进 keysvc —— 这是整条链路的接线点。

        早先 `keysvc.touch(key, ip, total)` 只传了 token，积分用量永远是 0，
        额度判定就永远不触发（功能看起来做了、实际没用）。
        """
        k = self._key(quota_credit=10)
        gateway._record(k, '1.2.3.4', 'glm-5.2', 'glm-5.2', 200, 100, 50, 120,
                        'ua', None, False, credit=7.5)
        row = self._fresh(k)
        self.assertAlmostEqual(row['used_credit'], 7.5, places=6)
        self.assertEqual(row['used_tokens'], 150)

    def test_migration_adds_columns_to_existing_db(self) -> None:
        """老库要能自动补上这两列（否则升级后一调用就报 no such column）。"""
        legacy = pathlib.Path(self._tmp.name) / 'legacy.db'
        conn = sqlite3.connect(str(legacy))
        conn.execute('CREATE TABLE api_keys (id INTEGER PRIMARY KEY, name TEXT, '
                     'key_hash TEXT, prefix TEXT, enabled INTEGER, quota INTEGER, '
                     'used_tokens INTEGER)')
        conn.execute("INSERT INTO api_keys VALUES(1, 'old', 'h', 'p', 1, 100, 5)")
        conn.commit()
        conn.close()

        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = legacy
        db.connect()

        cols = {r[1] for r in db.query('PRAGMA table_info(api_keys)')}
        self.assertIn('quota_credit', cols)
        self.assertIn('used_credit', cols)
        # 存量行的默认值必须是 0（不限），不能变成「已超限」
        row = db.query_one('SELECT quota_credit, used_credit FROM api_keys WHERE id = 1')
        self.assertEqual(float(row['quota_credit']), 0.0)
        self.assertEqual(float(row['used_credit']), 0.0)


class UsageCreditParsing(unittest.TestCase):
    """_usage_credit 只接受合法的非负数值。"""

    def test_valid_values(self) -> None:
        self.assertEqual(gateway._usage_credit({'credit': 1.5}), 1.5)
        self.assertEqual(gateway._usage_credit({'credit': 0}), 0.0)
        self.assertEqual(gateway._usage_credit({'credit': 0.0004}), 0.0004)
        self.assertEqual(gateway._usage_credit({'credit': 12}), 12.0)

    def test_missing_is_none_not_zero(self) -> None:
        """缺失必须是 None——否则会把「未知」当成「免费」。"""
        for u in ({}, {'credit': None}, None, 'x', []):
            self.assertIsNone(gateway._usage_credit(u), repr(u))

    def test_invalid_values_are_none(self) -> None:
        for u in ({'credit': 'abc'}, {'credit': True}, {'credit': -1}, {'credit': {}}):
            self.assertIsNone(gateway._usage_credit(u), repr(u))


class CreditSchema(unittest.TestCase):
    """两张表都要能自动补上 credit 列（老库迁移）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def _reopen(self, path: pathlib.Path) -> None:
        db._conn = None
        config.DB_PATH = path
        db.connect()

    def test_fresh_db_has_credit_columns(self) -> None:
        self._reopen(pathlib.Path(self._tmp.name) / 'new.db')
        for table in ('request_logs', 'usage_daily'):
            cols = {r[1] for r in db.query(f'PRAGMA table_info({table})')}
            self.assertIn('credit', cols, table)

    def test_old_db_gets_migrated_and_keeps_data(self) -> None:
        """模拟升级前已有的库：没有 credit 列。"""
        legacy = pathlib.Path(self._tmp.name) / 'legacy.db'
        conn = sqlite3.connect(str(legacy))
        conn.executescript(
            '''
            CREATE TABLE request_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, key_id INTEGER,
              ip TEXT, model TEXT, mapped_model TEXT, status INTEGER DEFAULT 0,
              prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
              latency_ms INTEGER DEFAULT 0, ua TEXT, error TEXT, stream INTEGER DEFAULT 0);
            CREATE TABLE usage_daily (
              day TEXT NOT NULL, key_id INTEGER NOT NULL, model TEXT NOT NULL,
              requests INTEGER NOT NULL DEFAULT 0,
              prompt_tokens INTEGER NOT NULL DEFAULT 0,
              completion_tokens INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (day, key_id, model));
            INSERT INTO request_logs(ts, model, status) VALUES(111, 'glm-5.2', 200);
            '''
        )
        conn.commit()
        conn.close()

        self._reopen(legacy)
        for table in ('request_logs', 'usage_daily'):
            cols = {r[1] for r in db.query(f'PRAGMA table_info({table})')}
            self.assertIn('credit', cols, f'{table} 应被补上 credit 列')
        row = db.query_one('SELECT model, credit FROM request_logs')
        self.assertEqual(row['model'], 'glm-5.2', '迁移不应丢数据')
        self.assertIsNone(row['credit'], '老数据的新列应为 NULL')

    def test_migration_is_idempotent(self) -> None:
        path = pathlib.Path(self._tmp.name) / 'again.db'
        self._reopen(path)
        for _ in range(3):
            db._conn.close()
            self._reopen(path)
        cols = [r[1] for r in db.query('PRAGMA table_info(request_logs)')]
        self.assertEqual(cols.count('credit'), 1, '重复迁移不应产生重复列')


class CreditRecording(unittest.TestCase):
    """扣费入库与累计。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = pathlib.Path(self._tmp.name) / 'rec.db'
        db._conn = None
        db.connect()
        db.execute(
            'INSERT INTO api_keys(name, key_hash, prefix, enabled, expires_at, max_ips, '
            'ip_allowlist, models, quota, used_tokens, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('k', _uniq_hash(), 'wbk_abc', 1, None, 0, '[]', '[]', 0, 0, int(time.time())),
        )
        self.key = {'id': 1}

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def _record(self, credit):
        gateway._record(self.key, '1.2.3.4', 'glm-5.2', 'glm-5.2', 200,
                        100, 50, 10, 'ua', None, False, credit=credit)

    def test_credit_stored_and_null_distinguished(self) -> None:
        self._record(1.25)
        self._record(None)
        rows = db.query('SELECT credit FROM request_logs ORDER BY id')
        self.assertEqual(float(rows[0]['credit']), 1.25)
        self.assertIsNone(rows[1]['credit'], '未返回扣费应存 NULL 而非 0')

    def test_credited_zero_is_a_real_zero(self) -> None:
        """上游明确返回 0（免费号）要存 0，不能与缺失混淆。"""
        self._record(0)
        self.assertEqual(float(db.query_one('SELECT credit FROM request_logs')['credit']), 0.0)

    def test_daily_accumulates_credit(self) -> None:
        self._record(1.25)
        self._record(0.75)
        row = db.query_one('SELECT requests, credit FROM usage_daily')
        self.assertEqual(int(row['requests']), 2)
        self.assertAlmostEqual(float(row['credit']), 2.0, places=6)

    def test_no_credit_does_not_break_usage(self) -> None:
        """上游未返回扣费时，请求数/Tokens 仍要正常累计。"""
        self._record(None)
        row = db.query_one('SELECT requests, prompt_tokens, completion_tokens, credit FROM usage_daily')
        self.assertEqual(int(row['requests']), 1)
        self.assertEqual(int(row['prompt_tokens']), 100)
        self.assertEqual(float(row['credit']), 0.0)

    def test_rebuild_preserves_credit(self) -> None:
        """重建统计不能把扣费清零。"""
        self._record(3.5)
        self._record(1.5)
        db.rebuild_usage_from_logs()
        row = db.query_one('SELECT credit FROM usage_daily')
        self.assertAlmostEqual(float(row['credit']), 5.0, places=6)


if __name__ == '__main__':
    unittest.main()

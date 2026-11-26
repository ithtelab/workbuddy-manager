"""登录态失效标记（issue #490）。

判定要克制：只认**明确的 401**、连续两次才置位、非 401 的临时失败不动标记、
查询成功或重登成功立即清除。列表侧把它变成布尔字段 `needs_relogin` 并把积分
快照置空 —— 旧快照会让用户以为账号还能用。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from server import config, db
from server.services import credits, wb2api


class SessionDeadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 's.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    # ── 判定 ──────────────────────────────────────────────
    def test_exact_401_only(self) -> None:
        self.assertTrue(credits._is_upstream_401('查询失败 code=401'))
        # 4010 之类的其它业务码不能被当成 401
        self.assertFalse(credits._is_upstream_401('查询失败 code=4010'))
        self.assertFalse(credits._is_upstream_401('查询失败 code=403'))
        # 网络/代理异常不算登录态失效
        self.assertFalse(credits._is_upstream_401('查询异常: ConnectTimeout'))
        self.assertFalse(credits._is_upstream_401('查询失败 code=401（代理认证失败）'))

    def test_single_failure_does_not_mark(self) -> None:
        credits.note_session_failure('u1')
        self.assertEqual(set(), credits.session_dead_uids(),
                         '一次 401 不应直接判死（可能是网关抖动）')

    def test_two_failures_mark(self) -> None:
        credits.note_session_failure('u1')
        credits.note_session_failure('u1')
        self.assertEqual({'u1'}, credits.session_dead_uids())

    def test_clear_on_success(self) -> None:
        credits.note_session_failure('u1')
        credits.note_session_failure('u1')
        self.assertEqual({'u1'}, credits.session_dead_uids())
        credits.clear_session_dead('u1')
        self.assertEqual(set(), credits.session_dead_uids())

    def test_forget_clears_mark(self) -> None:
        credits.note_session_failure('u1')
        credits.note_session_failure('u1')
        credits.forget('u1')
        self.assertEqual(set(), credits.session_dead_uids(),
                         '账号删除后标记必须一起清掉，否则 uid 复用时误报')

    def test_ttl_expiry(self) -> None:
        credits.note_session_failure('u1')
        credits.note_session_failure('u1')
        # 把置位时刻拨回 TTL 之外
        marks = credits._session_dead_raw()
        marks['u1']['at'] = time.time() - credits._SESSION_DEAD_TTL - 1
        credits._save_session_dead(marks)
        self.assertEqual(set(), credits.session_dead_uids())

    # ── 列表合并 ───────────────────────────────────────────
    def test_merge_pool_status_flags_and_blanks_credits(self) -> None:
        uid = 'a' * 32
        self.addCleanup(self._reset_session_cache)
        wb2api._SESSION_DEAD_CACHE = {'at': time.time(), 'set': {uid}}
        accounts = [{'uid': uid, 'file': 'workbuddy-a.json'}]
        status = {'accounts': [{'uid': uid, 'credits': 1234}]}
        out = wb2api.merge_pool_status(accounts, status)
        self.assertTrue(out[0]['needs_relogin'])
        self.assertIsNone(out[0]['credits'], '失效账号不能继续展示旧快照')
        self.assertIn('登录态失效', out[0]['invalid_reason'])

    def test_merge_pool_status_defaults_false(self) -> None:
        self.addCleanup(self._reset_session_cache)
        wb2api._SESSION_DEAD_CACHE = {'at': time.time(), 'set': set()}
        accounts = [{'uid': 'b' * 32, 'file': 'workbuddy-b.json'}]
        status = {'accounts': [{'uid': 'b' * 32, 'credits': 10}]}
        out = wb2api.merge_pool_status(accounts, status)
        self.assertFalse(out[0]['needs_relogin'])
        self.assertEqual(10, out[0]['credits'])

    @staticmethod
    def _reset_session_cache() -> None:
        wb2api._SESSION_DEAD_CACHE = {'at': 0.0, 'set': set()}


if __name__ == '__main__':
    unittest.main()

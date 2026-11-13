"""密钥每日 token 限额：与总额度各自独立，按本地日期计，跨天自动恢复。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc  # noqa: E402


class KeyDailyQuotaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'k.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def _fresh(self, token: str) -> dict:
        key = keysvc.resolve(token)
        self.assertIsNotNone(key)
        return key

    def test_default_is_unlimited(self) -> None:
        made = keysvc.create_key('daily-default')
        self.assertEqual(made['daily_quota'], 0)
        key = self._fresh(made['key'])
        keysvc.touch(key, '1.1.1.1', tokens=10**9)
        self.assertIsNone(keysvc.validate(self._fresh(made['key']), '1.1.1.1', 'm'))

    def test_blocks_after_daily_limit_and_resets_next_day(self) -> None:
        made = keysvc.create_key('daily', daily_quota=1000)
        token = made['key']
        with mock.patch.object(db, 'day_of', return_value='2026-11-13'):
            keysvc.touch(self._fresh(token), '1.1.1.1', tokens=600)
            key = self._fresh(token)
            self.assertEqual(key['daily_used_tokens'], 600)
            self.assertIsNone(keysvc.validate(key, '1.1.1.1', 'm'))
            keysvc.touch(key, '1.1.1.1', tokens=500)
            key = self._fresh(token)
            self.assertEqual(key['daily_used_tokens'], 1100)
            rej = keysvc.validate(key, '1.1.1.1', 'm')
            self.assertIsNotNone(rej)
            self.assertEqual(rej.status, 429)
            self.assertEqual(rej.code, 'daily_quota_exhausted')
        with mock.patch.object(db, 'day_of', return_value='2026-11-14'):
            key = self._fresh(token)
            self.assertEqual(key['daily_used_tokens'], 0)
            self.assertIsNone(keysvc.validate(key, '1.1.1.1', 'm'))
            keysvc.touch(key, '1.1.1.1', tokens=7)
            self.assertEqual(self._fresh(token)['daily_used_tokens'], 7)
            # 总用量照常累计，不受每日归零影响
            self.assertEqual(self._fresh(token)['used_tokens'], 1107)

    def test_update_and_reset(self) -> None:
        made = keysvc.create_key('daily-upd')
        updated = keysvc.update_key(made['id'], {'daily_quota': 50})
        self.assertEqual(updated['daily_quota'], 50)
        self.assertEqual(keysvc.update_key(made['id'], {'daily_quota': -3})['daily_quota'], 0)
        keysvc.update_key(made['id'], {'daily_quota': 50})
        keysvc.touch(self._fresh(made['key']), '1.1.1.1', tokens=80)
        self.assertIsNotNone(keysvc.validate(self._fresh(made['key']), '1.1.1.1', 'm'))
        self.assertTrue(keysvc.reset_usage(made['id']))
        self.assertIsNone(keysvc.validate(self._fresh(made['key']), '1.1.1.1', 'm'))


if __name__ == '__main__':
    unittest.main()

"""数据库压缩（issue #492 第二步）：VACUUM INTO + 原子替换 + 管理员接口。

覆盖四类事实：

  · 删行后 `freelist_count` 上升、主库文件不缩；压缩后文件变小、数据完整；
  · staging 完整性校验失败 / 磁盘不足时，**原库一个字节都不动**；
  · 维护任务在跑时压缩返回 busy，不并发写盘；
  · `GET/POST /api/maintenance/database*` 只对管理员开放，成功压缩留审计。

压缩走真实 SQLite（不 mock VACUUM），只有两条失败分支用 mock 制造。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault('WB_DATA_DIR', tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402

import server.security as security  # noqa: E402
from server import config, db  # noqa: E402
from server.main import app  # noqa: E402
from server.services import accountrisk, dbmaintenance  # noqa: E402


class _TempDbCase(unittest.TestCase):
    """每个用例一个独立数据目录；不动仓库里的 data/。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'manager.db'
        db._conn = None
        db.connect()
        accountrisk.initialize()
        self.now = int(time.time())

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        finally:
            db._conn = None
            config.DB_PATH = self._orig_db
            self._tmp.cleanup()

    def _insert_request_logs(self, count: int, *, start: int = 0) -> None:
        blob = 'x' * 2048
        db.executemany(
            'INSERT INTO request_logs(ts, model, ua) VALUES(?, ?, ?)',
            [(self.now, 'm', blob) for _ in range(start, start + count)],
        )

    def _count(self, table: str) -> int:
        return int(db.query_one(f'SELECT COUNT(*) AS n FROM "{table}"')['n'])

    def _staging_files(self) -> list[str]:
        return [p.name for p in Path(config.DB_PATH).parent.iterdir()
                if '.compact-' in p.name]

    def _db_size(self) -> int:
        return Path(config.DB_PATH).stat().st_size


class CompactDatabaseTest(_TempDbCase):
    def test_compact_shrinks_file_and_keeps_data(self) -> None:
        self._insert_request_logs(1500)
        db.execute('INSERT INTO checkin_logs(ts, uid, kind) VALUES(?, ?, ?)',
                   (self.now, 'u1', 'checkin'))
        db.execute('INSERT INTO task_logs(ts, uid, kind, dedup_key) VALUES(?, ?, ?, ?)',
                   (self.now, 'u1', 'checkin', 'k1'))

        # WAL 里的页要先并回主库，否则看到的是「主库还没变」而不是「删行不缩」。
        dbmaintenance._checkpoint()
        size_after_insert = self._db_size()
        db.execute('DELETE FROM request_logs WHERE id <= 1000')
        dbmaintenance._checkpoint()
        # 删行不改主库大小，只把页放进 freelist。
        self.assertEqual(self._db_size(), size_after_insert,
                         'SQLite DELETE 不应缩小主库文件（测试前提）')
        freelist = int(db.query_one('PRAGMA freelist_count')[0])
        self.assertGreater(freelist, 0, '删行后 freelist_count 应上升')

        result = dbmaintenance.compact_database()

        self.assertLess(result['after_bytes'], result['before_bytes'])
        self.assertEqual(result['saved_bytes'],
                         result['before_bytes'] - result['after_bytes'])
        self.assertGreaterEqual(result['duration_ms'], 0)
        self.assertEqual(self._count('request_logs'), 500)
        self.assertEqual(self._count('checkin_logs'), 1)
        self.assertEqual(self._count('task_logs'), 1)
        check = [str(r[0]) for r in db.query('PRAGMA integrity_check')]
        self.assertEqual(check, ['ok'])
        self.assertEqual(self._staging_files(), [], '压缩后不应留下 staging')
        self.assertLessEqual(int(db.query_one('PRAGMA freelist_count')[0]), freelist,
                             'VACUUM 后 freelist 不应高于压缩前')

        last = db.get_setting(dbmaintenance._LAST_RESULT_KEY)
        self.assertEqual(last['kind'], 'compact')
        self.assertTrue(last['ok'])

    def test_small_database_still_compacts(self) -> None:
        # <1MB 也照常执行；重建后可能多出一个页（saved 为负），不把「小」当错误。
        result = dbmaintenance.compact_database()
        self.assertGreater(result['before_bytes'], 0)
        self.assertGreater(result['after_bytes'], 0)
        self.assertEqual(result['saved_bytes'],
                         result['before_bytes'] - result['after_bytes'])

    def test_integrity_failure_does_not_replace(self) -> None:
        self._insert_request_logs(300)
        dbmaintenance._checkpoint()
        before_rows = self._count('request_logs')
        before_size = self._db_size()

        with mock.patch.object(dbmaintenance, '_integrity_ok',
                               return_value=(False, 'simulated corruption')):
            with self.assertRaises(dbmaintenance.CompactError):
                dbmaintenance.compact_database()

        self.assertEqual(self._count('request_logs'), before_rows, '原库数据被改动了')
        self.assertEqual(self._db_size(), before_size, '原库文件被替换了')
        self.assertEqual(self._staging_files(), [], '失败后应清理 staging')
        self.assertEqual([str(r[0]) for r in db.query('PRAGMA integrity_check')], ['ok'])

    def test_insufficient_disk_space_does_not_start(self) -> None:
        self._insert_request_logs(200)
        dbmaintenance._checkpoint()
        before_rows = self._count('request_logs')
        before_size = self._db_size()

        with mock.patch.object(dbmaintenance.shutil, 'disk_usage',
                               return_value=SimpleNamespace(free=0)):
            with self.assertRaises(dbmaintenance.InsufficientDiskSpace):
                dbmaintenance.compact_database()

        self.assertEqual(self._count('request_logs'), before_rows)
        self.assertEqual(self._db_size(), before_size)
        self.assertEqual(self._staging_files(), [])

    def test_busy_when_another_maintenance_runs(self) -> None:
        self.assertTrue(dbmaintenance._run_guard.acquire(blocking=False))
        try:
            with self.assertRaises(dbmaintenance.MaintenanceBusy):
                dbmaintenance.compact_database()
        finally:
            dbmaintenance._run_guard.release()


class MaintenanceApiTest(_TempDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        super().tearDown()

    def _as(self, role: str) -> None:
        app.dependency_overrides[security.current_user] = \
            lambda: {'username': f'{role}-user', 'role': role}

    def test_non_admin_is_forbidden(self) -> None:
        self._as('viewer')
        self.assertEqual(self.client.get('/api/maintenance/database').status_code, 403)
        self.assertEqual(
            self.client.post('/api/maintenance/database/compact', json={}).status_code, 403)
        self.assertEqual(self._count('audit_logs'), 0, '被拒绝的请求不应产生审计')

    def test_admin_can_read_status(self) -> None:
        self._insert_request_logs(5)
        self._as('admin')
        r = self.client.get('/api/maintenance/database')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertGreater(body['db_bytes'], 0)
        self.assertIn('wal_bytes', body)
        for name in ('request_logs', 'tap_turns', 'checkin_logs',
                     'account_risk_events', 'task_logs'):
            self.assertIn(name, body['tables'])
        self.assertEqual(body['tables']['request_logs'], 5)
        self.assertIn('last', body)

    def test_admin_compact_returns_before_after_and_audits(self) -> None:
        self._insert_request_logs(1200)
        db.execute('DELETE FROM request_logs WHERE id <= 900')
        self._as('admin')

        r = self.client.post('/api/maintenance/database/compact', json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertLess(body['after_bytes'], body['before_bytes'])
        self.assertEqual(self._count('request_logs'), 300)

        row = db.query_one(
            'SELECT actor, action, target FROM audit_logs ORDER BY id DESC LIMIT 1')
        self.assertEqual(row['actor'], 'admin-user')
        self.assertEqual(row['action'], 'database.compact')
        self.assertEqual(row['target'], 'manager.db')

        status = self.client.get('/api/maintenance/database').json()
        self.assertEqual(status['last']['kind'], 'compact')
        self.assertTrue(status['last']['ok'])

    def test_compact_disk_error_is_507_and_audited(self) -> None:
        self._insert_request_logs(50)
        self._as('admin')
        with mock.patch.object(dbmaintenance.shutil, 'disk_usage',
                               return_value=SimpleNamespace(free=0)):
            r = self.client.post('/api/maintenance/database/compact', json={})
        self.assertEqual(r.status_code, 507, r.text)
        self.assertIn('磁盘空间不足', r.json()['detail'])
        row = db.query_one(
            'SELECT action, detail FROM audit_logs ORDER BY id DESC LIMIT 1')
        self.assertEqual(row['action'], 'database.compact')
        self.assertIn('压缩未开始', row['detail'])


if __name__ == '__main__':
    unittest.main()

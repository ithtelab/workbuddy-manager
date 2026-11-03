"""账号回填只诊断本该关联、且已超过正常等待期的请求。"""
from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from server import db
from server.routers import gateway
from server.services import accountlog


class PendingCandidatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('CREATE TABLE request_logs '
                          '(ts INTEGER, model TEXT, mapped_model TEXT, account TEXT, '
                          'account_expected INTEGER, status INTEGER)')
        self.addCleanup(self.conn.close)
        patch = mock.patch.object(db, '_conn', self.conn)
        patch.start()
        self.addCleanup(patch.stop)

    def add(self, ts=900, model='m', mapped='', expected=None, status=200, account=None):
        self.conn.execute('INSERT INTO request_logs VALUES(?, ?, ?, ?, ?, ?)',
                          (ts, model, mapped, account, expected, status))
        self.conn.commit()

    def test_empty_models_do_not_count(self) -> None:
        self.add(model='', mapped='')
        self.add(model=None, mapped=None)
        self.assertFalse(accountlog._has_pending_accounts(700, 940))

    def test_mapped_model_alone_counts(self) -> None:
        self.add(model='', mapped='actual')
        self.assertTrue(accountlog._has_pending_accounts(700, 940))

    def test_fresh_and_old_records_do_not_count(self) -> None:
        self.add(ts=999)
        self.add(ts=699)
        self.assertFalse(accountlog._has_pending_accounts(700, 940))

    def test_local_rejections_do_not_count(self) -> None:
        self.add(expected=0, status=429)
        self.assertFalse(accountlog._has_pending_accounts(700, 940))

    def test_upstream_failures_and_legacy_rows_count(self) -> None:
        self.add(expected=1, status=503)
        self.assertTrue(accountlog._has_pending_accounts(700, 940))
        self.conn.execute('DELETE FROM request_logs')
        self.add(expected=None, status=400)
        self.assertTrue(accountlog._has_pending_accounts(700, 940))

    def test_filled_rows_do_not_count(self) -> None:
        self.add(account='A(1)')
        self.assertFalse(accountlog._has_pending_accounts(700, 940))


class RecordExpectationTest(unittest.TestCase):
    def test_record_marks_local_rejection_without_using_status(self) -> None:
        with mock.patch.object(db, 'add_request_log') as add:
            gateway._record(None, '127.0.0.1', 'm', 'm', 429, 0, 0, 0, None,
                            'local rejection', False, account_expected=False)
            self.assertEqual(add.call_args.kwargs['account_expected'], 0)
            gateway._record(None, '127.0.0.1', 'm', 'm', 429, 0, 0, 1, None,
                            'upstream rejection', False)
            self.assertEqual(add.call_args.kwargs['account_expected'], 1)

    def test_model_list_is_not_expected_to_have_chat_account(self) -> None:
        with mock.patch.object(db, 'add_request_log') as add:
            gateway._record(None, '127.0.0.1', '', '', 200, 0, 0, 0, None, None, False)
        self.assertEqual(add.call_args.kwargs['account_expected'], 0)

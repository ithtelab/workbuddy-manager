"""账号回填的**自诊断**别再误报（issue #232 的假警报）。

现象：请求日志页弹出黄条「日志来自 docker（workbuddy2api），但账号列仍为空：
解析出了请求行，但不对上本地请求记录」，而表格里账号列明明全是账号。

成因：回填只更新 `account IS NULL` 的行（`db.attach_request_accounts` 的 SQL
带这个条件，设计上「宁可漏填，不可填错」）。采集器每 15 秒读最后 500 行，正常
情况下这些行对应的记录**早就填过了**，于是本轮 `filled == 0`；而 `status()`
拿 `filled` 当唯一判据，就把它判成「对不上」。

修法：多记一个 `already`（这批条目在窗口内**确实存在**本地记录，只是已填过），
只有 `filled == 0 且 already == 0` 才算真对不上。
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


def _line(ts: int, model: str = 'deepseek-v4.1-flash', acct: str = 'uid=3a3a19b1') -> str:
    """按上游 logging.go 的排版拼一条带 docker UTC 前缀的请求行。"""
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(ts)) + '.123456789Z'
    clock = time.strftime('%H:%M:%S', time.localtime(ts))
    return (f'{stamp} | #001 | {clock} | {model}        | stream | 200 | {acct}   '
            f'| TTFB=103ms   | tok=20     | 15.3tok/s   | total=1.3s |')


class BackfillDiagnosticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from server import config, db
        cls._tmp = tempfile.TemporaryDirectory()
        cls._orig = (config.DB_PATH, config.WB2API_MODE)
        config.DB_PATH = Path(cls._tmp.name) / 'diag.db'
        config.WB2API_MODE = 'docker'
        db._conn = None
        db.connect()

    @classmethod
    def tearDownClass(cls) -> None:
        from server import config, db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.WB2API_MODE = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        from server import db
        from server.services import accountlog
        db.execute('DELETE FROM request_logs')
        accountlog._last.update({'checked_at': 0, 'lines': 0, 'parsed': 0,
                                 'filled': 0, 'already': 0, 'error': ''})
        self.ts = int(time.time()) - 5

    def _collect(self, lines: list[str]):
        from server.services import accountlog
        with mock.patch.object(accountlog.wb2api, 'read_container_logs',
                               lambda *a, **kw: (lines, None)), \
             mock.patch.object(accountlog.updater, 'can_control_docker', lambda: True):
            return accountlog._collect_once()

    def _status(self) -> dict:
        from server.services import accountlog
        with mock.patch.object(accountlog.updater, 'can_control_docker', lambda: True):
            return accountlog.status()

    def _log(self, ts: int, account: str | None = None) -> int:
        from server import db
        model = 'deepseek-v4.1-flash'
        db.add_request_log(ts=ts, model=model, mapped_model=model, status=200,
                           stream=1, latency_ms=1000)
        rid = db.query_one('SELECT id FROM request_logs ORDER BY id DESC')['id']
        if account:
            db.execute('UPDATE request_logs SET account = ? WHERE id = ?', (account, rid))
        return rid

    def test_already_filled_is_not_an_alarm(self) -> None:
        """**核心回归**：这一批请求早就回填过 → 本轮 filled=0，但绝不能报警。"""
        self._log(self.ts, account='190****5013(5bd9dc6e)')
        filled = self._collect([_line(self.ts)])
        self.assertEqual(filled, 0, '已经填过的行不该被重复计入 filled')

        info = self._status()
        self.assertEqual(info['parsed'], 1)
        self.assertEqual(info['filled'], 0)
        self.assertEqual(info['already'], 1, '要能说清「其实有对应记录、只是已填过」')
        self.assertEqual(info['hint'], '', '这种正常状态不能弹黄条')

    def test_empty_account_still_gets_filled(self) -> None:
        """窗口内有未填的行 → 照常回填，且不报警。"""
        rid = self._log(self.ts)
        self.assertEqual(self._collect([_line(self.ts)]), 1)
        from server import db
        self.assertEqual(
            db.query_one('SELECT account FROM request_logs WHERE id = ?', (rid,))['account'],
            '3a3a19b1')
        self.assertEqual(self._status()['hint'], '')

    def test_really_unmatched_still_warns(self) -> None:
        """一条都对不上时，黄条仍然要出现——误报修好不等于把真问题也藏起来。"""
        self._log(self.ts - 600)          # 10 分钟前的记录，窗口外
        self._collect([_line(self.ts)])
        info = self._status()
        self.assertEqual(info['filled'], 0)
        self.assertEqual(info['already'], 0)
        self.assertIn('匹配窗口', info['hint'])

    def test_already_counts_only_matching_model(self) -> None:
        """同时间、不同模型的记录不算「已存在」——判据要和回填同口径。"""
        from server import db
        db.add_request_log(ts=self.ts, model='cn:glm-5.3-flash',
                           mapped_model='cn:glm-5.3-flash', status=200, stream=1,
                           latency_ms=1000)
        accountlog_entries = [{'ts': self.ts, 'model': 'deepseek-v4.1-flash'}]
        self.assertEqual(db.count_entries_present(accountlog_entries), 0)

    def test_already_matches_mapped_model_too(self) -> None:
        """模型映射过的行（mapped_model 不同）也要认——回填两边都看。"""
        from server import db
        db.add_request_log(ts=self.ts, model='deepseek-v4.1-flash',
                           mapped_model='cn:deepseek-v4.1-flash', status=200, stream=1,
                           latency_ms=1000)
        self.assertEqual(
            db.count_entries_present([{'ts': self.ts, 'model': 'deepseek-v4.1-flash'}]), 1)

    def test_status_is_json_serialisable(self) -> None:
        """`/api/logs/account-backfill` 直接返回它，别塞进不可序列化的对象。"""
        self._collect([_line(self.ts)])
        json.dumps(self._status(), ensure_ascii=False)


if __name__ == '__main__':
    unittest.main()

"""请求日志的「账号」回填（issue #69）。

为什么单独一个文件：这里的正确性**靠肉眼看不出来**——账号填错了，界面上照样
显示一个像模像样的名字（比如「Moonquakes(299e342b)」），只是它不是这一次的
那一个。两种错法都不会报错：

  1. **时区**：上游日志的 docker 时间戳是 UTC（`...Z`）。若按本地时区解析，
     在东八区会整体偏移 8 小时 → 匹配到 8 小时前那条请求的账号，或者干脆
     匹配不上（表现为「账号列一直是空的」）。
  2. **匹配窗口**：窗口开太大，会把邻近请求的账号抢过来；开太小又永远填不上。

另一件要钉住的是「填不上时的行为」：回填是旁路，取不到就该留空，
**不能猜**——猜错比留空更糟，因为它看起来是对的。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 上游日志的原样样本（含中英文昵称的列宽对齐）
LINE_CN = ('2026-09-23T06:58:13.123895379Z | #971 | 14:58:13 | deepseek-v4.1-flash'
           '        | stream | 200 | uid=3a3a19b1       | TTFB=1083ms   | tok=20'
           '     | 15.3tok/s   | total=1.3s |')
LINE_EN = ('2026-09-23T06:58:21.402929620Z | #972 | 14:58:21 | deepseek-v4.1-flash'
           '        | stream | 200 | uid=299e342b   | TTFB=1031ms   | tok=17'
           '     | 13.5tok/s   | total=1.3s |')

# 上面两行的时间戳（UTC）——用于断言时区解析正确
TS_CN = 1790146693
TS_EN = 1790146701


class ParseTest(unittest.TestCase):
    """解析上游日志行。"""

    def test_parses_account_and_model(self) -> None:
        from server.services import accountlog
        got = accountlog.parse_request_lines([LINE_CN])
        self.assertEqual(len(got), 1, '正常的对话行必须被解析出来')
        self.assertEqual(got[0]['account'], '3a3a19b1')
        self.assertEqual(got[0]['model'], 'deepseek-v4.1-flash')

    def test_event_identity_survives_tail_and_distinguishes_subseconds(self) -> None:
        from server.services import accountlog
        other = LINE_CN.replace('123895379Z', '223895379Z')
        first, second = accountlog.parse_request_lines([LINE_CN, other])
        self.assertEqual(first['event_id'], accountlog.parse_request_lines([LINE_CN])[0]['event_id'])
        self.assertNotEqual(first['event_id'], second['event_id'])

    def test_timestamp_is_utc(self) -> None:
        """**核心**：docker 前缀是 UTC，解析必须按 UTC。

        按本地时区解会整体偏移（东八区差 8 小时），结果是账号被填到别的
        请求上——或者一条也填不上，而两种都不报错。
        """
        from server.services import accountlog
        got = accountlog.parse_request_lines([LINE_CN, LINE_EN])
        self.assertEqual([g['ts'] for g in got], [TS_CN, TS_EN])

    def test_skips_native_without_mtime(self) -> None:
        """原生行没有日期；没有 mtime 作为锚点时只能跳过，不能猜。"""
        from server.services import accountlog
        bare = LINE_CN.split(' ', 1)[1]
        self.assertEqual(accountlog.parse_request_lines([bare]), [])

    def test_native_timestamp_from_mtime_anchor(self) -> None:
        """原生模式用文件 mtime 锚定最后一行，按行内时钟回推。"""
        import datetime
        from server.services import accountlog
        lines = [
            '| #973 | 14:58:20 | deepseek-v4.1-flash | stream | 200 | A(aaaaaaaa) | TTFB=1ms | tok=1 | 1tok/s | total=1.0s |',
            '| #974 | 14:58:22 | deepseek-v4.1-flash | stream | 200 | B(bbbbbbbb) | TTFB=1ms | tok=1 | 1tok/s | total=1.0s |',
        ]
        mtime = datetime.datetime(2026, 9, 23, 14, 58, 22).timestamp()
        got = accountlog.parse_request_lines_with_mtime(lines, mtime)
        self.assertEqual([g['account'] for g in got], ['A(aaaaaaaa)', 'B(bbbbbbbb)'])
        self.assertEqual([g['ts'] - int(mtime) for g in got], [-2, 0])

    def test_native_midnight_rollover(self) -> None:
        """跨午夜时向左回推要减一天。"""
        import datetime
        from server.services import accountlog
        lines = [
            '| #1 | 23:59:59 | m | stream | 200 | A(1) | TTFB=1ms | tok=1 | 1tok/s | total=1s |',
            '| #2 | 00:00:01 | m | stream | 200 | B(2) | TTFB=1ms | tok=1 | 1tok/s | total=1s |',
        ]
        mtime = datetime.datetime(2026, 9, 24, 0, 0, 1).timestamp()
        got = accountlog.parse_request_lines_with_mtime(lines, mtime)
        self.assertEqual(got[0]['ts'], int(mtime) - 2)
        self.assertEqual(got[1]['ts'], int(mtime))


    def test_skips_non_chat_lines(self) -> None:
        """上游日志里混着调度器/池状态等行，不能被当成请求。"""
        from server.services import accountlog
        noise = [
            '2026-09-23T06:58:30.000000000Z 2026/09/23 14:58:30 [pool] 恢复来源=本地 state.json',
            '2026-09-23T06:58:31.000000000Z [watch] auths 目录监听已启用（每 5s 检查一次）',
            '2026-09-23T06:58:32.000000000Z school: ok (scripts/school_open_day_2026.py)',
            '',
        ]
        self.assertEqual(accountlog.parse_request_lines(noise), [])

    def test_skips_placeholder_account(self) -> None:
        """账号列是 `-` 时（上游没选出账号的异常情形）不解析，避免填个破折号进去。"""
        from server.services import accountlog
        line = LINE_CN.replace('uid=3a3a19b1', '          -          ')
        self.assertEqual(accountlog.parse_request_lines([line]), [])


class AttachTest(unittest.TestCase):
    """把账号回填到 request_logs（db 层）。"""

    @classmethod
    def setUpClass(cls) -> None:
        from server import config, db
        cls._tmp = tempfile.TemporaryDirectory()
        cls._orig = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / 'acct.db'
        db._conn = None
        db.connect()

    @classmethod
    def tearDownClass(cls) -> None:
        from server import config, db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        from server import db
        db.execute('DELETE FROM request_logs')
        db.execute('DELETE FROM request_account_matches')

    def _log(self, ts: int, model: str = 'deepseek-v4.1-flash') -> int:
        from server import db
        db.add_request_log(ts=ts, model=model, mapped_model=model, status=200,
                           stream=1, latency_ms=1000)
        return db.query_one('SELECT id FROM request_logs ORDER BY id DESC')['id']

    def _account(self, row_id: int) -> str | None:
        from server import db
        return db.query_one('SELECT account FROM request_logs WHERE id = ?',
                            (row_id,))['account']

    def test_fills_matching_row(self) -> None:
        from server import db
        rid = self._log(TS_CN)
        n = db.attach_request_accounts(
            [{'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': '6509授(3a3a19b1)'}])
        self.assertEqual(n, 1)
        self.assertEqual(self._account(rid), '6509授(3a3a19b1)')

    def test_matches_nearest_when_several_in_window(self) -> None:
        """窗口内有多条时选**最接近**的那条。

        本端记的 ts 是秒级（`int()` 截断），上游给的是纳秒 —— 两者永远不相等，
        所以只能就近匹配；不选最近的话，同一秒附近的请求会互相串号。

        两条候选的**距离必须不同**（差 1 秒 vs 差 3 秒），否则「最近」本身
        就有歧义，测不出排序是否真的生效。
        """
        from server import db
        far = self._log(TS_CN - 3)      # 与查询点差 3 秒（仍在 3 秒窗口边界内）
        near = self._log(TS_CN - 1)     # 与查询点差 1 秒
        n = db.attach_request_accounts(
            [{'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'A(11111111)'}])
        self.assertEqual(n, 1)
        self.assertEqual(self._account(near), 'A(11111111)', '应选差 1 秒的那条')
        self.assertIsNone(self._account(far), '不该动差 3 秒的那条')

    def test_does_not_overwrite_already_filled(self) -> None:
        """已填过的行不再改。

        采集器每 15 秒读一次 `--tail`，同一行会被反复读到。若不限制
        `account IS NULL`，后读到的（可能匹配到了邻近的另一条）就会把先前的
        正确值覆盖掉——**宁可漏填，不可填错**。
        """
        from server import db
        rid = self._log(TS_CN)
        db.attach_request_accounts(
            [{'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'FIRST(11111111)'}])
        n = db.attach_request_accounts(
            [{'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'SECOND(22222222)'}])
        self.assertEqual(n, 0, '已填过的行不该被再次匹配')
        self.assertEqual(self._account(rid), 'FIRST(11111111)')

    def test_event_is_not_reused_for_neighbor_on_next_poll(self) -> None:
        from server import db
        first = self._log(TS_CN)
        neighbor = self._log(TS_CN + 1)
        entry = {'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'A(1)',
                 'event_id': 'event-1', 'source': 'docker:upstream'}
        self.assertEqual(db.attach_request_accounts([entry, entry]), 1)
        self.assertEqual(db.attach_request_accounts([entry]), 0)
        self.assertEqual(self._account(first), 'A(1)')
        self.assertIsNone(self._account(neighbor))

    def test_consumption_survives_connection_restart(self) -> None:
        from server import db
        self._log(TS_CN)
        neighbor = self._log(TS_CN + 1)
        entry = {'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'A(1)'}
        self.assertEqual(db.attach_request_accounts([entry]), 1)
        db._conn.close()
        db._conn = None
        self.assertEqual(db.attach_request_accounts([entry]), 0)
        self.assertIsNone(self._account(neighbor))

    def test_first_poll_after_upgrade_does_not_steal_legacy_event(self) -> None:
        from server import db
        first = self._log(TS_CN)
        db.execute('UPDATE request_logs SET account = ? WHERE id = ?', ('A(1)', first))
        neighbor = self._log(TS_CN + 1)
        entry = {'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'A(1)',
                 'event_id': 'legacy-event'}
        self.assertEqual(db.attach_request_accounts([entry]), 0)
        self.assertEqual(db.attach_request_accounts([entry]), 0)
        self.assertIsNone(self._account(neighbor))
        self.assertEqual(db.query_one('SELECT count(*) n FROM request_account_matches')['n'], 1)

    def test_clear_request_logs_keeps_consumed_events(self) -> None:
        from server import db
        self._log(TS_CN)
        entry = {'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'A(1)'}
        self.assertEqual(db.attach_request_accounts([entry]), 1)
        db.execute('DELETE FROM request_logs')
        neighbor = self._log(TS_CN + 1)
        self.assertEqual(db.attach_request_accounts([entry]), 0)
        self.assertIsNone(self._account(neighbor))

    def test_consumption_failure_rolls_back_account_update(self) -> None:
        from server import db
        row = self._log(TS_CN)
        db.execute("CREATE TRIGGER reject_account_match BEFORE INSERT ON request_account_matches "
                   "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        try:
            entry = {'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'A(1)'}
            self.assertEqual(db.attach_request_accounts([entry]), 0)
            self.assertIsNone(self._account(row))
            self.assertEqual(db.query_one('SELECT count(*) n FROM request_account_matches')['n'], 0)
        finally:
            db.execute('DROP TRIGGER reject_account_match')
        self.assertEqual(db.attach_request_accounts([entry]), 1)

    def test_different_event_does_not_skip_closer_filled_row(self) -> None:
        from server import db
        first = self._log(TS_CN)
        neighbor = self._log(TS_CN + 1)
        db.execute('UPDATE request_logs SET account = ? WHERE id = ?', ('A(1)', first))
        entry = {'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'B(2)'}
        self.assertEqual(db.attach_request_accounts([entry]), 0)
        self.assertIsNone(self._account(neighbor))

    def test_concurrent_polls_consume_event_once(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from server import db
        self._log(TS_CN)
        neighbor = self._log(TS_CN + 1)
        entry = {'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'A(1)',
                 'event_id': 'shared-event'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(db.attach_request_accounts, [[entry], [entry]]))
        self.assertEqual(sum(counts), 1)
        self.assertIsNone(self._account(neighbor))

    def test_unmatched_event_can_retry_after_local_record_arrives(self) -> None:
        from server import db
        entry = {'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'A(1)'}
        self.assertEqual(db.attach_request_accounts([entry]), 0)
        row = self._log(TS_CN)
        self.assertEqual(db.attach_request_accounts([entry]), 1)
        self.assertEqual(self._account(row), 'A(1)')

    def test_equidistant_candidates_are_not_guessed(self) -> None:
        from server import db
        left = self._log(TS_CN - 1)
        right = self._log(TS_CN + 1)
        self.assertEqual(db.attach_request_accounts(
            [{'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'A(1)'}]), 0)
        self.assertIsNone(self._account(left))
        self.assertIsNone(self._account(right))

    def test_local_rejection_is_not_a_candidate_but_upstream_error_is(self) -> None:
        from server import db
        db.add_request_log(ts=TS_CN, model='m', status=429, account_expected=0)
        rejected = db.query_one('SELECT id FROM request_logs ORDER BY id DESC')['id']
        db.add_request_log(ts=TS_CN + 1, model='m', status=429, account_expected=1)
        upstream = db.query_one('SELECT id FROM request_logs ORDER BY id DESC')['id']
        self.assertEqual(db.attach_request_accounts(
            [{'ts': TS_CN, 'model': 'm', 'account': 'A(1)'}]), 1)
        self.assertIsNone(self._account(rejected))
        self.assertEqual(self._account(upstream), 'A(1)')

    def test_distinct_events_with_one_second_local_lag_both_match(self) -> None:
        """合成事件：上游 100/101，本地 101/102；已关联第一条不能挡住第二条。"""
        from server import db
        first = self._log(TS_CN + 1)
        second = self._log(TS_CN + 2)
        entries = [{'ts': TS_CN + i, 'model': 'deepseek-v4.1-flash',
                    'account': f'account-{i}', 'event_id': f'event-{i}'} for i in range(2)]
        self.assertEqual(db.attach_request_accounts(entries), 2)
        self.assertEqual(self._account(first), 'account-0')
        self.assertEqual(self._account(second), 'account-1')
        self.assertEqual(db.attach_request_accounts(entries), 0)

    def test_distinct_events_with_same_account_are_not_collapsed(self) -> None:
        from server import db
        self._log(TS_CN)
        self._log(TS_CN + 1)
        entries = [{'ts': TS_CN + i, 'model': 'deepseek-v4.1-flash',
                    'account': 'A(1)', 'event_id': f'event-{i}'} for i in range(2)]
        self.assertEqual(db.attach_request_accounts(entries), 2)

    def test_model_must_match(self) -> None:
        """模型对不上的不填：同一秒可能有别的模型的请求。"""
        from server import db
        rid = self._log(TS_CN, model='deepseek-v4-pro')
        n = db.attach_request_accounts(
            [{'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'X(11111111)'}])
        self.assertEqual(n, 0)
        self.assertIsNone(self._account(rid))

    def test_outside_window_is_left_null(self) -> None:
        """超出窗口的留空——**不猜**。"""
        from server import db
        rid = self._log(TS_CN)
        n = db.attach_request_accounts(
            [{'ts': TS_CN + 60, 'model': 'deepseek-v4.1-flash', 'account': 'Y(11111111)'}])
        self.assertEqual(n, 0)
        self.assertIsNone(self._account(rid), '取不到就该留空，不能填一个看起来像的')

    def test_matches_mapped_model_too(self) -> None:
        """两边模型名都可能对得上：本端存的是**映射后**的名字，这里两者都试。"""
        from server import db
        db.add_request_log(ts=TS_CN, model='my-alias', mapped_model='deepseek-v4.1-flash',
                           status=200, stream=1, latency_ms=1000)
        rid = db.query_one('SELECT id FROM request_logs ORDER BY id DESC')['id']
        n = db.attach_request_accounts(
            [{'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': 'M(11111111)'}])
        self.assertEqual(n, 1)
        self.assertEqual(self._account(rid), 'M(11111111)')

    def test_bad_entries_are_ignored(self) -> None:
        """畸形输入不能让回填抛异常（它是旁路，异常会打断整轮采集）。"""
        from server import db
        rid = self._log(TS_CN)
        n = db.attach_request_accounts([
            {'ts': 'not-a-number', 'model': 'x', 'account': 'A(1)'},
            {'ts': TS_CN, 'model': 'deepseek-v4.1-flash', 'account': ''},
            {'model': 'x', 'account': 'A(1)'},
            {},
        ])
        self.assertEqual(n, 0)
        self.assertIsNone(self._account(rid))

    def test_account_text_is_sanitized(self) -> None:
        """账号名来自外部日志，入库前清洗换行——否则能在日志页伪造出额外行。"""
        from server import db
        rid = self._log(TS_CN)
        db.attach_request_accounts(
            [{'ts': TS_CN, 'model': 'deepseek-v4.1-flash',
              'account': 'evil\ninjected(12345678)'}])
        got = self._account(rid)
        self.assertNotIn('\n', got or '')
        self.assertIn('injected', got or '')

    def test_blank_model_never_matches_blank_rows(self) -> None:
        """模型名为空的条目必须被丢掉，而不是匹配到「模型列也是空」的历史行。

        评审补的：`model = ''` 在 SQL 里是个**合法**条件，会命中那些模型为空的行
        （早期记录、或上游没回模型的试探请求）。真发生了就是把账号填到一条毫不相干
        的日志上——而且看起来完全正常。宁可漏填。
        """
        from server import db
        blank = self._log(TS_CN, model='')
        n = db.attach_request_accounts(
            [{'ts': TS_CN, 'model': '', 'account': '张叔叔(299e342b)'}])
        self.assertEqual(n, 0, '空模型名不该匹配任何行')
        self.assertIsNone(self._account(blank))

    def test_bool_ts_is_ignored(self) -> None:
        """`ts=True` 会被当成 epoch 1 —— 不是时间戳就别参与匹配。"""
        from server import db
        rid = self._log(TS_CN)
        n = db.attach_request_accounts(
            [{'ts': True, 'model': 'deepseek-v4.1-flash', 'account': 'A(12345678)'}])
        self.assertEqual(n, 0)
        self.assertIsNone(self._account(rid))


if __name__ == '__main__':
    unittest.main()

"""任务日志采集入库与去重的回归测试（使用独立临时数据库）。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server import upstreamsvc  # noqa: E402
from server.services import tasklog, wb2api  # noqa: E402

SAMPLE = [
    'listening on :7863',
    '2026-09-11T09:00:01.111111111Z 2026/09/11 09:00:01 travel 89374120: depart ok location=7',
    '2026-09-11T09:00:02.222222222Z 2026/09/11 09:00:02 travel 89374120: claim ok record=12 reward=100',
    '2026-09-11T09:00:03.333333333Z 2026/09/11 09:00:03 travel 89374120: adopt ok (+300 credits)',
    '2026-09-11T10:00:00.444444444Z 2026/09/11 10:00:00 activity 89374120: streak days=3',
    '2026-09-11T21:00:00.555555555Z 2026/09/11 21:00:00 travel 91203877: skip (daily limit reached)',
    '2026-09-11T22:00:00.666666666Z 2026/09/11 22:00:00 keepalive 91203877: 连续 3 次 12153 session dead — 禁用',
    '2026-09-11T22:30:00.777777777Z 2026/09/11 22:30:00 checkin 91203877: context deadline exceeded',
]


class CollectPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'test.db'
        db._conn = None          # 强制用新的临时库
        db.connect()
        self._orig_reader = wb2api.read_container_logs
        wb2api.read_container_logs = lambda limit=200, timestamps=True: SAMPLE

    def tearDown(self) -> None:
        wb2api.read_container_logs = self._orig_reader
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def test_first_collect_parses_and_persists(self) -> None:
        parsed, added = asyncio.run(tasklog._collect_once())
        # 7 行任务日志（'listening on' 被忽略）
        self.assertEqual(parsed, 7)
        self.assertEqual(added, 7)

    def test_second_collect_is_deduplicated(self) -> None:
        asyncio.run(tasklog._collect_once())
        parsed, added = asyncio.run(tasklog._collect_once())
        self.assertEqual(parsed, 7)
        self.assertEqual(added, 0, '重复采集同一条日志不应重复入库')
        self.assertEqual(db.task_log_stats()['total'], 7)

    def test_credit_totals(self) -> None:
        asyncio.run(tasklog._collect_once())
        stats = db.task_log_stats()
        # 100 (claim) + 300 (adopt)
        self.assertEqual(stats['total_credits'], 400)
        self.assertEqual(stats['by_kind']['travel']['credits'], 400)
        self.assertEqual(stats['by_kind']['travel']['count'], 4)

    def test_levels_classified(self) -> None:
        asyncio.run(tasklog._collect_once())
        by_msg = {l['message']: l['level'] for l in db.list_task_logs(limit=50)}
        self.assertEqual(by_msg['claim ok record=12 reward=100'], 'credit')
        self.assertEqual(by_msg['adopt ok (+300 credits)'], 'credit')
        self.assertEqual(by_msg['depart ok location=7'], 'ok')
        self.assertEqual(by_msg['streak days=3'], 'ok')
        self.assertEqual(by_msg['skip (daily limit reached)'], 'info')
        self.assertEqual(
            by_msg['连续 3 次 12153 session dead — 禁用'], 'error'
        )

    def test_filter_by_kind_and_uid(self) -> None:
        asyncio.run(tasklog._collect_once())
        travel = db.list_task_logs(limit=50, kind='travel')
        self.assertEqual(len(travel), 4)
        self.assertTrue(all(l['kind'] == 'travel' for l in travel))

        one = db.list_task_logs(limit=50, uid='89374120')
        self.assertTrue(all(l['uid'] == '89374120' for l in one))
        self.assertEqual(len(one), 4)

    def test_clear(self) -> None:
        asyncio.run(tasklog._collect_once())
        db.clear_task_logs()
        self.assertEqual(db.task_log_stats()['total'], 0)


class GroupContainerCollection(unittest.TestCase):
    """多分组部署：每个分组的上游容器都要采集（issue #389）。"""

    GROUP_LINES = [
        '2026-09-12T09:00:01.111111111Z 2026/09/12 09:00:01 checkin 分组号01(abc12345): ok credits=100',
        '2026-09-12T09:00:02.222222222Z 2026/09/12 09:00:02 checkin 分组号01(abc12345): 已签到',
    ]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'groups.db'
        db._conn = None
        db.connect()
        self._orig_reader = wb2api.read_container_logs
        self.calls: list[str | None] = []

        def reader(limit=200, timestamps=True, container=None):
            self.calls.append(container)
            return self.GROUP_LINES if container else SAMPLE

        wb2api.read_container_logs = reader

    def tearDown(self) -> None:
        wb2api.read_container_logs = self._orig_reader
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def test_reads_default_and_group_containers(self) -> None:
        upstreamsvc.create_upstream('分组', 'http://127.0.0.1:7863',
                                    container='wb-group')
        parsed, added = asyncio.run(tasklog._collect_once())
        self.assertEqual(parsed, 9, '默认容器 7 条 + 分组容器 2 条')
        self.assertEqual(added, 9)
        self.assertEqual(self.calls, [None, 'wb-group'])
        messages = [l['message'] for l in db.list_task_logs(limit=50)]
        self.assertIn('ok credits=100', messages)
        self.assertIn('已签到', messages)

    def test_same_container_as_default_is_read_once(self) -> None:
        upstreamsvc.create_upstream('重复', 'http://127.0.0.1:7863',
                                    container=config.WB2API_CONTAINER)
        asyncio.run(tasklog._collect_once())
        self.assertEqual(self.calls, [None], '默认容器与分组同名时不应读两遍')

    def test_disabled_group_container_still_read(self) -> None:
        # 分组被停用只影响新请求的受理；已经产生的任务日志仍应保留，
        # 否则用户停用分组后历史签到记录会突然停止采集。
        upstreamsvc.create_upstream('停用组', 'http://127.0.0.1:7863',
                                    enabled=False, container='wb-group')
        asyncio.run(tasklog._collect_once())
        self.assertIn('wb-group', self.calls)


class NicknameResolution(unittest.TestCase):
    """上游日志只带 uid 前 8 位；接口要能解析出昵称。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'n.db'
        db._conn = None
        db.connect()
        self._orig_list = wb2api.list_auth_accounts

    def tearDown(self) -> None:
        wb2api.list_auth_accounts = self._orig_list
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def _resolve(self, row_uid: str, accounts: list[dict]) -> str:
        wb2api.list_auth_accounts = lambda auth_dir=None: accounts  # type: ignore[assignment]
        db.clear_task_logs()
        db.add_task_logs([{
            'ts': 1, 'uid': row_uid, 'kind': 'travel', 'level': 'ok',
            'credits': 0, 'message': 'depart ok location=1',
            'dedup_key': f'nick-{row_uid}',
        }])
        # 复用路由里的解析逻辑
        from server.routers import accounts as accounts_router
        res = accounts_router.task_logs(limit=10, uid=None, kind=None, user={})
        return res['logs'][0].get('nickname', '')

    def test_full_uid_matches(self) -> None:
        got = self._resolve(
            '9b212d8c-f5f7-4ad6-aa20-1d576508c8c1',
            [{'uid': '9b212d8c-f5f7-4ad6-aa20-1d576508c8c1', 'nickname': '黑天鹅'}],
        )
        self.assertEqual(got, '黑天鹅')

    def test_truncated_prefix_matches(self) -> None:
        """上游截断后的 8 位前缀也要能对上昵称。"""
        got = self._resolve(
            '9b212d8c',
            [{'uid': '9b212d8c-f5f7-4ad6-aa20-1d576508c8c1', 'nickname': '黑天鹅'}],
        )
        self.assertEqual(got, '黑天鹅')

    def test_unknown_uid_returns_empty(self) -> None:
        got = self._resolve('ffffffff', [{'uid': 'aaaaaaaa-1111', 'nickname': '别人'}])
        self.assertEqual(got, '')


class Pagination(unittest.TestCase):
    """任务/签到日志的分页与筛选后计数。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'page.db'
        db._conn = None
        db.connect()
        now = int(time.time())
        # 每条相隔 1 天，这样 days 筛选才有区分度（用 600 秒间隔会全落在 24h 内）
        db.add_task_logs([
            {'ts': now - i * 86400, 'uid': 'u1',
             'kind': 'activity' if i % 2 else 'travel',
             'level': 'ok', 'credits': 0, 'message': f'm{i}', 'dedup_key': f'pk{i}'}
            for i in range(120)
        ])
        for i in range(35):
            db.add_checkin_log('u1' if i % 2 else 'u2', 'n', 'manual-batch', True, 0, 'ok')

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def test_task_pages_do_not_overlap(self) -> None:
        p1 = db.list_task_logs(limit=20, offset=0)
        p2 = db.list_task_logs(limit=20, offset=20)
        self.assertEqual(len(p1), 20)
        self.assertEqual(len(p2), 20)
        self.assertFalse({r['id'] for r in p1} & {r['id'] for r in p2}, '两页不应重叠')

    def test_can_reach_last_page(self) -> None:
        """分页的意义就是能翻到全部历史（原先硬截断在 500 条）。"""
        total = db.count_task_logs()
        self.assertEqual(total, 120)
        last = db.list_task_logs(limit=20, offset=100)
        self.assertEqual(len(last), 20, '最后一页应能取到剩余 20 条')

    def test_offset_beyond_end_is_empty(self) -> None:
        self.assertEqual(db.list_task_logs(limit=20, offset=9999), [])

    def test_count_matches_kind_filter(self) -> None:
        travel = db.count_task_logs(kind='travel')
        self.assertEqual(travel, 60)
        # 筛选后的 count 必须与「取满全部」的结果条数一致
        self.assertEqual(len(db.list_task_logs(limit=500, kind='travel')), travel)

    def test_days_filter(self) -> None:
        all_n = db.count_task_logs()
        recent = db.count_task_logs(days=1)
        self.assertLess(recent, all_n, '时间范围应缩小结果集')
        self.assertGreater(recent, 0)

    def test_stats_respect_days(self) -> None:
        """概览必须与列表同一时间范围，否则数字对不上。"""
        st = db.task_log_stats(days=1)
        self.assertEqual(st['total'], db.count_task_logs(days=1))

    def test_checkin_pagination(self) -> None:
        self.assertEqual(db.count_checkin_logs(), 35)
        first = db.list_checkin_logs(limit=20, offset=0)
        second = db.list_checkin_logs(limit=20, offset=20)
        self.assertEqual(len(first), 20)
        self.assertEqual(len(second), 15, '最后一页取剩余')
        self.assertFalse({r['id'] for r in first} & {r['id'] for r in second})

    def test_limit_is_capped(self) -> None:
        """limit 上限收紧到 500，避免一次拉全量。"""
        self.assertLessEqual(len(db.list_task_logs(limit=99999)), 500)
        self.assertLessEqual(len(db.list_checkin_logs(limit=99999)), 500)


if __name__ == '__main__':
    unittest.main()

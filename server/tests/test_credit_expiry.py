"""积分到期时间（CycleEndTime）的解析与透出。

为什么要有这个文件：到期时间算错是**静默**的 —— 界面照样显示一个漂亮的
倒计时，只是数字是错的，用户据此以为还有一周，实际当天就作废了。两种典型
错法都没法靠肉眼发现：

  1. **时区**：腾讯下发的是 UTC+8 墙钟串，不带时区标记。若按本机时区解析
     （time.mktime / datetime.fromtimestamp），在 UTC 容器上会整体偏移 8 小时，
     在西半球偏移更多。上游为此显式用 time.ParseInLocation(layout, s, UTC+8)。
  2. **字段名**：`PackageEndTime` 看着更像「套餐到期」，但上游实测（其注释写着
     「CN/global 两域字段全集均无 PackageEndTime，旧判据恒 miss 致 Expiring 恒 0」）
     真实响应里只有 `CycleEndTime`。认错字段的结果是倒计时永远不出现。

另外把上游 packageRemainUsed 的三条判据（含脏数据钳位）移植过来锁死口径：
余额口径与到期时间出自同一个函数，改一个不影响另一个这种假设不成立。
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import tencent  # noqa: E402

_CN = timezone(timedelta(hours=8))


def _stamp(epoch: int) -> str:
    """把绝对时刻写成腾讯下发的形态：UTC+8 墙钟串。"""
    return datetime.fromtimestamp(epoch, _CN).strftime('%Y-%m-%d %H:%M:%S')


class PackageExpiryParseTest(unittest.TestCase):
    """CycleEndTime → 绝对时刻。"""

    def test_parses_utc8_wallclock(self) -> None:
        """`2026-09-20 12:00:00` 是 UTC+8 的中午，即 04:00 UTC。"""
        got = tencent._package_expiry({'CycleEndTime': '2026-09-20 12:00:00'})
        self.assertEqual(got, int(datetime(2026, 9, 20, 12, 0, tzinfo=_CN).timestamp()))
        self.assertEqual(
            datetime.fromtimestamp(got, timezone.utc).strftime('%H:%M'),
            '04:00',
            'UTC+8 的 12:00 必须是 UTC 的 04:00',
        )

    def test_naive_datetime_never_reaches_timestamp(self) -> None:
        """把「朴素 datetime 直接取时间戳」这条错路堵死。

        这是本文件的核心断言。容器时区通常不是 UTC+8（官方镜像按 UTC 跑），
        朴素 datetime 的 `.timestamp()` 按**本机**时区解释，于是同一份腾讯响应
        在不同机器上得出不同的到期时间 —— 而界面上看不出来，倒计时照样在走，
        只是错的。

        做法：把 `tencent.datetime` 换成一个「没有时区就拒绝取时间戳」的子类。
        实现只要老老实实先贴上 UTC+8，就正常工作；一旦退回
        `datetime.strptime(s, fmt).timestamp()`，立刻报错并指出原因。

        不用改 TZ / 子进程的原因：Windows 上 time.tzset() 不存在，而通过
        subprocess env 注入的 TZ 在 Windows Python 里实测不生效（只有 MSYS
        启动时翻译过的才认）。依赖本机时区的测试在两种环境下会给出一致的
        假绿，不如直接盯住代码行为。
        """
        from unittest import mock

        class _Guarded(datetime):
            def timestamp(self) -> float:
                if self.tzinfo is None:
                    raise AssertionError(
                        '朴素 datetime 直接取时间戳会按本机时区解释，'
                        '必须先 replace(tzinfo=UTC+8)'
                    )
                return super().timestamp()

        class _DT:
            """只暴露 strptime 的替身，逼实现走这条路（真实代码也只用了它）。"""

            @staticmethod
            def strptime(s: str, fmt: str) -> _Guarded:
                return _Guarded.strptime(s, fmt)  # type: ignore[return-value]

        raw = {'CycleEndTime': '2026-12-31 23:59:59'}
        with mock.patch.object(tencent, 'datetime', _DT):
            got = tencent._package_expiry(raw)
        self.assertEqual(got, int(datetime(2026, 12, 31, 23, 59, 59, tzinfo=_CN).timestamp()))

    def test_the_guard_actually_fires_on_naive_timestamps(self) -> None:
        """反证上一条的守卫不是摆设：朴素 datetime 取时间戳必须被它拦下。

        没有这条，守卫可能因为「构造得不对、根本没被走到」而永远绿。
        """
        class _Guarded(datetime):
            def timestamp(self) -> float:
                if self.tzinfo is None:
                    raise AssertionError('guard fired')
                return super().timestamp()

        naive = _Guarded.strptime('2026-12-31 23:59:59', '%Y-%m-%d %H:%M:%S')
        with self.assertRaises(AssertionError):
            naive.timestamp()
        aware = naive.replace(tzinfo=_CN)
        self.assertIsInstance(aware.timestamp(), float, '贴了时区就该正常工作')

    def test_missing_or_invalid_returns_none(self) -> None:
        """字段缺失/空/解析不了 → None，且**不抛异常**（不能因它毁掉整次积分查询）。"""
        for item in (
            {},
            {'CycleEndTime': None},
            {'CycleEndTime': ''},
            {'CycleEndTime': '   '},
            {'CycleEndTime': '2026/09/20 12:00:00'},
            {'CycleEndTime': '0000-00-00 00:00:00'},
            {'CycleEndTime': 1726800000},
        ):
            with self.subTest(item=item):
                self.assertIsNone(tencent._package_expiry(item))

    def test_tolerates_surrounding_spaces(self) -> None:
        self.assertEqual(
            tencent._package_expiry({'CycleEndTime': ' 2026-09-20 12:00:00 '}),
            tencent._package_expiry({'CycleEndTime': '2026-09-20 12:00:00'}),
        )

    def test_field_name_is_cycle_end_time(self) -> None:
        """锁字段名：PackageEndTime 在真实响应里不存在，认它倒计时会永远不显示。"""
        self.assertIsNone(tencent._package_expiry({'PackageEndTime': '2026-09-20 12:00:00'}))
        self.assertIsNotNone(tencent._package_expiry({'CycleEndTime': '2026-09-20 12:00:00'}))


class PackageRemainTest(unittest.TestCase):
    """单套餐余额口径 —— 逐条对齐上游 packageRemainUsed 与其测试。"""

    def test_dirty_remain_over_size_is_clamped(self) -> None:
        """上游 TestUserResourceDetailedSharesPackageRemainUsed：600>500 必须钳到 500。

        只钳负值的旧口径会高估余额，让用户以为还有 600 可用。
        """
        self.assertEqual(tencent._package_remain(
            {'CycleCapacitySize': 500, 'CycleCapacityRemain': 600, 'CycleCapacityUsed': 0}
        ), 500)

    def test_negative_remain_clamped_to_zero(self) -> None:
        self.assertEqual(tencent._package_remain(
            {'CycleCapacitySize': 500, 'CycleCapacityRemain': -20, 'CycleCapacityUsed': 520}
        ), 0)

    def test_used_overrides_when_remain_disagrees(self) -> None:
        """used 与 size-remain 不一致时以 used 为准（上游同序：先算 remain 再被 used 修正）。"""
        self.assertEqual(tencent._package_remain(
            {'CycleCapacitySize': 1000, 'CycleCapacityRemain': 900, 'CycleCapacityUsed': 800}
        ), 200)

    def test_falls_back_to_capacity_when_no_cycle(self) -> None:
        """上游 TestUserResourceDetailedTotalMatchesLegacy 形态 2：Cycle 三零 → 回退 Capacity。"""
        self.assertEqual(tencent._package_remain(
            {'CapacitySize': 300, 'CapacityRemain': 300, 'CapacityUsed': 0}
        ), 300)

    def test_three_shapes_total_matches_upstream_legacy(self) -> None:
        """上游 TestUserResourceDetailedTotalMatchesLegacy：17 + 300 + 0 = 317。

        三种真实套餐形态混在一起，聚合结果必须与上游逐位相等——否则我们自己
        算出的余额与上游 /status 的 credits 会长期不一致。
        """
        accounts = [
            # 形态 1：纯 Cycle 活跃（当天有消耗）
            {'CycleCapacitySize': 500, 'CycleCapacityRemain': 17, 'CycleCapacityUsed': 482,
             'CapacitySize': 500, 'CapacityRemain': 500, 'CapacityUsed': 0},
            # 形态 2：Cycle 三零（从未使用）→ 回退 Capacity
            {'CapacitySize': 300, 'CapacityRemain': 300, 'CapacityUsed': 0},
            # 形态 3：已结束的包（全套 0/100/100）
            {'CycleCapacitySize': 100, 'CycleCapacityRemain': 0, 'CycleCapacityUsed': 100,
             'CapacitySize': 100, 'CapacityRemain': 0, 'CapacityUsed': 100},
        ]
        self.assertEqual(sum(tencent._package_remain(a) for a in accounts), 317)

    def test_non_numeric_values_are_zero(self) -> None:
        for item in (
            {'CycleCapacitySize': '500', 'CycleCapacityRemain': None},
            {'CycleCapacitySize': True, 'CycleCapacityRemain': 100},
            {'CycleCapacityRemain': 'abc'},
        ):
            with self.subTest(item=item):
                self.assertEqual(tencent._package_remain(item), 0)


class FetchCreditsExpiriesTest(unittest.TestCase):
    """fetch_credits 的到期列表：只收有余额的套餐，按时间升序。"""

    def _fetch(self, accounts: list, monkeypatch_env=None):
        """直接把账单响应的信封喂进解析路径，绕开网络。"""
        from unittest import mock

        payload = {'code': 0, 'data': {'Response': {'Data': {'Accounts': accounts}}}}

        class _Resp:
            status_code = 200

            def json(self):
                return payload

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                return _Resp()

        from server import config
        with mock.patch.object(config, 'http_client', lambda *a, **kw: _Client()):
            import asyncio
            return asyncio.run(tencent.fetch_credits({'access_token': 'TOK', 'realm': 'cn'}))

    def test_only_funded_packages_appear(self) -> None:
        """余额为 0 的套餐不进列表：它到期与否不影响任何决策，列出来只会干扰。"""
        now = int(time.time())
        ok, _, _, expiries = self._fetch([
            {'CycleCapacitySize': 100, 'CycleCapacityRemain': 0, 'CycleCapacityUsed': 100,
             'CycleEndTime': _stamp(now + 86400)},
            {'CycleCapacitySize': 500, 'CycleCapacityRemain': 120, 'CycleCapacityUsed': 380,
             'CycleEndTime': _stamp(now + 3 * 86400)},
        ])
        self.assertTrue(ok)
        self.assertEqual(len(expiries), 1)
        self.assertEqual(expiries[0]['amount'], 120)

    def test_sorted_ascending_by_expiry(self) -> None:
        """升序是前端的隐含契约：它直接取 [0] 当「最近一笔」显示倒计时。"""
        now = int(time.time())
        ok, credits, _, expiries = self._fetch([
            {'CycleCapacitySize': 100, 'CycleCapacityRemain': 10, 'CycleEndTime': _stamp(now + 30 * 86400)},
            {'CycleCapacitySize': 100, 'CycleCapacityRemain': 20, 'CycleEndTime': _stamp(now + 86400)},
            {'CycleCapacitySize': 100, 'CycleCapacityRemain': 30, 'CycleEndTime': _stamp(now + 7 * 86400)},
        ])
        self.assertTrue(ok)
        self.assertEqual([e['amount'] for e in expiries], [20, 30, 10], '必须按到期时间升序')
        self.assertEqual(credits, 60, '余额合计不受排序影响')

    def test_packages_without_expiry_are_omitted(self) -> None:
        """没有 CycleEndTime 的套餐（永不过期）不进列表，余额照常计入合计。"""
        ok, credits, _, expiries = self._fetch([
            {'CycleCapacitySize': 100, 'CycleCapacityRemain': 42},
        ])
        self.assertTrue(ok)
        self.assertEqual(credits, 42)
        self.assertEqual(expiries, [])

    def test_amount_uses_same_clamping_as_total(self) -> None:
        """列表里的额度与合计走同一口径，不能一个钳一个不钳。"""
        now = int(time.time())
        ok, credits, _, expiries = self._fetch([
            {'CycleCapacitySize': 500, 'CycleCapacityRemain': 600, 'CycleCapacityUsed': 0,
             'CycleEndTime': _stamp(now + 86400)},
        ])
        self.assertTrue(ok)
        self.assertEqual(credits, 500)
        self.assertEqual(expiries[0]['amount'], 500)

    def test_negative_package_contributes_zero_not_negative(self) -> None:
        """坏套餐（负余额）贡献 0，而不是从合计里倒扣。

        上游是**逐个钳到 0 再累加**，不是先加再钳。顺序反了的话，一个 -100 的
        脏套餐会把合计拉低 100，同一个账号我们显示的余额比上游少，用户对不上账。
        """
        now = int(time.time())
        ok, credits, _, expiries = self._fetch([
            {'CapacitySize': 500, 'CapacityRemain': -100, 'CapacityUsed': 600},
            {'CycleCapacitySize': 100, 'CycleCapacityRemain': 50, 'CycleCapacityUsed': 50,
             'CycleEndTime': _stamp(now + 86400)},
        ])
        self.assertTrue(ok)
        self.assertEqual(credits, 50, '-100 只能算 0，不能从 50 里扣')
        self.assertEqual(len(expiries), 1, '负余额的套餐不该出现在到期列表里')

    def test_all_negative_totals_zero(self) -> None:
        ok, credits, _, expiries = self._fetch([
            {'CapacitySize': 100, 'CapacityRemain': -30, 'CapacityUsed': 130},
        ])
        self.assertTrue(ok)
        self.assertEqual(credits, 0)
        self.assertEqual(expiries, [])

    def test_failure_returns_empty_list_not_none(self) -> None:
        """失败路径也要给列表（空），否则前端得处理两个「没有数据」的形态。"""
        import asyncio
        ok, credits, msg, expiries = asyncio.run(tencent.fetch_credits({}))
        self.assertFalse(ok)
        self.assertIsNone(credits)
        self.assertEqual(expiries, [])


class CreditsCacheCarriesExpiriesTest(unittest.TestCase):
    """到期列表必须与余额同缓存 —— 两者的时效性一致。"""

    def setUp(self) -> None:
        import tempfile
        from server import config, db
        from server.services import credits

        self.credits = credits
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'c.db'
        db._conn = None
        db.connect()
        credits.invalidate()
        self._orig_fetch = tencent.fetch_credits

    def tearDown(self) -> None:
        from server import config, db
        tencent.fetch_credits = self._orig_fetch  # type: ignore[assignment]
        self.credits.invalidate()
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def test_cached_hit_returns_same_expiries(self) -> None:
        import asyncio
        exp = [{'at': 1800000000, 'amount': 120}]

        async def fake(auth):
            return True, 1000, 'ok', exp

        tencent.fetch_credits = fake  # type: ignore[assignment]
        auth = {'uid': 'u1'}
        first = asyncio.run(self.credits.get_credits(auth))
        second = asyncio.run(self.credits.get_credits(auth))
        self.assertFalse(first[3], '首次应为实时查询')
        self.assertTrue(second[3], '第二次应命中缓存')
        self.assertEqual(first[5], exp)
        self.assertEqual(second[5], exp, '命中缓存也要给出到期列表')

    def test_expiries_not_shared_between_accounts(self) -> None:
        """缓存按 uid 隔离：串号会让 A 的到期时间显示在 B 上。"""
        import asyncio
        calls = {'n': 0}

        async def fake(auth):
            calls['n'] += 1
            return True, 100 * calls['n'], 'ok', [{'at': calls['n'], 'amount': calls['n']}]

        tencent.fetch_credits = fake  # type: ignore[assignment]
        a = asyncio.run(self.credits.get_credits({'uid': 'ua'}))
        b = asyncio.run(self.credits.get_credits({'uid': 'ub'}))
        self.assertEqual(a[5][0]['at'], 1)
        self.assertEqual(b[5][0]['at'], 2)

    def test_invalidate_clears_expiries_too(self) -> None:
        import asyncio
        seq = [{'at': 1, 'amount': 1}]

        async def fake(auth):
            return True, 5, 'ok', seq

        tencent.fetch_credits = fake  # type: ignore[assignment]
        asyncio.run(self.credits.get_credits({'uid': 'u1'}))
        self.credits.invalidate('u1')
        seq = [{'at': 2, 'amount': 2}]
        got = asyncio.run(self.credits.get_credits({'uid': 'u1'}))
        self.assertEqual(got[5], [{'at': 2, 'amount': 2}], '失效后必须重新查')


class PackageCreatedTest(unittest.TestCase):
    """上游 CreateTime（毫秒）→ 到账秒；缺失/非法返回 0，交给调用方回退。"""

    def test_milliseconds_to_seconds(self) -> None:
        self.assertEqual(tencent._package_created({'CreateTime': 1762710000123}), 1762710000)

    def test_missing_or_invalid_is_zero(self) -> None:
        for item in ({}, {'CreateTime': None}, {'CreateTime': 'x'},
                     {'CreateTime': 0}, {'CreateTime': -1}):
            self.assertEqual(tencent._package_created(item), 0)


if __name__ == '__main__':
    unittest.main()

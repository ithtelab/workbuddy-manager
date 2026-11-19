"""DeepSeek 官方价估算（issue #380）的回归测试。

钉住四件事：

  1. **峰谷判定**按北京时间、按工作日、按节假日表——三者错一个，费用就差一倍。
     容器里通常是 UTC，用 `localtime` 会把窗口整体错开 8 小时，所以这里特意用
     绝对时间戳（而不是本机时间）来断言。
  2. **价目版本**按调用时刻生效：改价不影响已经发生的账，早于最早一版则算不出。
  3. **未知模型返回 None 而不是 0**：没收录价目的模型（glm / kimi）算不出费用，
     显示「—」；返回 0 会让人以为这些调用免费。
  4. **缓存口径**：命中部分按命中价、其余按未命中价；上游没给命中数时整段按
     未命中价（高估方向，绝不低估）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.services import pricing  # noqa: E402

CST = timezone(timedelta(hours=8))


def ts_of(y: int, m: int, d: int, hour: int = 0, minute: int = 0) -> float:
    """北京时间 → 绝对时间戳。"""
    return datetime(y, m, d, hour, minute, tzinfo=CST).timestamp()


# 2026 年的日历参照（用于断言，不要用「今天」）：
#   2026-10-06 周二 —— 国庆假期（10-01 ~ 10-07）
#   2026-10-08 周四 —— 普通工作日
#   2026-10-10 周六 —— 周末
PEAK_WEEKDAY = ts_of(2026, 10, 8, 10, 0)
OFFPEAK_LUNCH = ts_of(2026, 10, 8, 12, 30)
OFFPEAK_EVENING = ts_of(2026, 10, 8, 19, 0)
HOLIDAY_WEEKDAY = ts_of(2026, 10, 6, 10, 0)
WEEKEND = ts_of(2026, 10, 10, 10, 0)


class PeakWindowTest(unittest.TestCase):
    """峰谷判定：北京时间 + 工作日 + 节假日表。"""

    def test_peak_inside_windows(self) -> None:
        self.assertTrue(pricing.is_peak(ts_of(2026, 10, 8, 9, 0)))
        self.assertTrue(pricing.is_peak(ts_of(2026, 10, 8, 11, 59)))
        self.assertTrue(pricing.is_peak(ts_of(2026, 10, 8, 14, 0)))
        self.assertTrue(pricing.is_peak(ts_of(2026, 10, 8, 17, 59)))

    def test_off_peak_outside_windows(self) -> None:
        self.assertFalse(pricing.is_peak(ts_of(2026, 10, 8, 8, 59)))
        self.assertFalse(pricing.is_peak(ts_of(2026, 10, 8, 12, 0)))   # 午休
        self.assertFalse(pricing.is_peak(ts_of(2026, 10, 8, 18, 0)))   # 下班
        self.assertFalse(pricing.is_peak(ts_of(2026, 10, 8, 3, 0)))

    def test_weekend_is_off_peak(self) -> None:
        self.assertFalse(pricing.is_peak(WEEKEND))

    def test_holiday_is_off_peak_even_on_weekday(self) -> None:
        # 2026-10-06 是周二，但属于国庆假期 → 空闲价
        self.assertFalse(pricing.is_peak(HOLIDAY_WEEKDAY))
        # 假期结束后的工作日恢复高峰
        self.assertTrue(pricing.is_peak(PEAK_WEEKDAY))

    def test_holiday_table_covers_2026_and_flags_2027(self) -> None:
        missing = pricing.missing_holiday_years(PEAK_WEEKDAY)
        self.assertIn(2027, missing)
        self.assertNotIn(2026, missing)


class UnitPriceTest(unittest.TestCase):
    def test_model_name_normalisation(self) -> None:
        self.assertEqual(pricing.normalize_model('cn:deepseek-v4.1-flash'), 'deepseek-flash')
        self.assertEqual(pricing.normalize_model('global:deepseek-v4.1-flash'), 'deepseek-flash')
        self.assertEqual(pricing.normalize_model('deepseek-v4-flash'), 'deepseek-flash')
        self.assertEqual(pricing.normalize_model('deepseek-v4-pro'), 'deepseek-v4-pro')
        self.assertIsNone(pricing.normalize_model('glm-5.3'))
        self.assertIsNone(pricing.normalize_model(''))

    def test_peak_and_off_peak_rates(self) -> None:
        peak = pricing.unit_prices('deepseek-v4.1-flash', PEAK_WEEKDAY)
        off = pricing.unit_prices('deepseek-v4.1-flash', OFFPEAK_LUNCH)
        self.assertIsNotNone(peak)
        self.assertIsNotNone(off)
        self.assertEqual(peak['cache_miss'], 2.0)
        self.assertEqual(off['cache_miss'], 1.0)
        self.assertEqual(peak['output'], 8.0)
        self.assertEqual(off['output'], 4.0)
        self.assertEqual(peak['cache_hit'], 0.04)
        self.assertEqual(off['cache_hit'], 0.02)
        self.assertTrue(peak['peak'])
        self.assertFalse(off['peak'])

    def test_pro_is_more_expensive(self) -> None:
        flash = pricing.unit_prices('deepseek-flash', PEAK_WEEKDAY)
        pro = pricing.unit_prices('deepseek-v4-pro', PEAK_WEEKDAY)
        self.assertEqual(pro['cache_miss'], 9.0)
        self.assertEqual(pro['output'], 27.0)
        self.assertGreater(pro['output'], flash['output'])

    def test_unknown_model_has_no_price(self) -> None:
        self.assertIsNone(pricing.unit_prices('glm-5.3', PEAK_WEEKDAY))
        self.assertIsNone(pricing.cost_of('glm-5.3', 1000, 1000, ts=PEAK_WEEKDAY))

    def test_before_first_version_is_unknown(self) -> None:
        # 价目表最早一版 2026-09-01；更早的调用无从计价（不猜）
        self.assertIsNone(pricing.unit_prices('deepseek-flash', ts_of(2026, 8, 1, 10, 0)))


class CostFormulaTest(unittest.TestCase):
    def test_plain_input_output(self) -> None:
        # 1M 未命中输入（2 元）+ 1M 输出（8 元）= 10 元（flash，高峰）
        self.assertAlmostEqual(
            pricing.cost_of('deepseek-flash', 1_000_000, 1_000_000, ts=PEAK_WEEKDAY),
            10.0, places=6)
        # 同一笔在空闲时段是一半
        self.assertAlmostEqual(
            pricing.cost_of('deepseek-flash', 1_000_000, 1_000_000, ts=OFFPEAK_LUNCH),
            5.0, places=6)

    def test_cache_hit_gets_its_own_rate(self) -> None:
        # 0.5M 命中（0.04 元/M）+ 0.5M 未命中（2 元/M）+ 0 输出 = 1.02 元
        got = pricing.cost_of('deepseek-flash', 1_000_000, 0,
                              cache_hit=500_000, ts=PEAK_WEEKDAY)
        self.assertAlmostEqual(got, 1.02, places=6)
        # 不拆缓存的话会是 2 元——差 2 倍，正是要拆的理由
        self.assertAlmostEqual(
            pricing.cost_of('deepseek-flash', 1_000_000, 0, ts=PEAK_WEEKDAY), 2.0, places=6)

    def test_cache_miss_field_wins_when_given(self) -> None:
        got = pricing.cost_of('deepseek-flash', 1_000_000, 0,
                              cache_hit=0, cache_miss=1_000_000, ts=PEAK_WEEKDAY)
        self.assertAlmostEqual(got, 2.0, places=6)

    def test_zero_tokens_cost_nothing(self) -> None:
        self.assertEqual(pricing.cost_of('deepseek-flash', 0, 0, ts=PEAK_WEEKDAY), 0.0)


class VersionTest(unittest.TestCase):
    """改价：追加一版，历史不变。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'p.db'
        db._conn = None
        db.connect()
        pricing.invalidate()

    def tearDown(self) -> None:
        pricing.set_override(None)
        pricing.invalidate()
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig_db

    def test_override_takes_effect_from_its_date(self) -> None:
        before = pricing.cost_of('deepseek-flash', 1_000_000, 0, ts=PEAK_WEEKDAY)
        self.assertAlmostEqual(before, 2.0, places=6)

        pricing.set_override({
            'currency': 'CNY',
            'versions': [{
                'effective_from': '2026-11-01',
                'verified_at': '2026-11-02',
                'models': {'deepseek-flash': {
                    'cache_hit': {'peak': 0.08, 'off_peak': 0.04},
                    'cache_miss': {'peak': 4.0, 'off_peak': 2.0},
                    'output': {'peak': 16.0, 'off_peak': 8.0},
                }},
            }],
        })

        # 10-08 的调用仍按旧价（历史不变）
        self.assertAlmostEqual(
            pricing.cost_of('deepseek-flash', 1_000_000, 0, ts=PEAK_WEEKDAY), 2.0, places=6)
        # 11-05 的调用按新价
        self.assertAlmostEqual(
            pricing.cost_of('deepseek-flash', 1_000_000, 0, ts=ts_of(2026, 11, 5, 10, 0)),
            4.0, places=6)

    def test_describe_exposes_versions_and_flags(self) -> None:
        info = pricing.describe(PEAK_WEEKDAY)
        self.assertEqual(info['currency'], 'CNY')
        self.assertTrue(info['versions'])
        self.assertFalse(info['has_override'])
        self.assertIn('deepseek-flash', info['priced_models'])
        self.assertIsNotNone(info['versions'][0]['age_days'])
        self.assertEqual(info['stale_after_days'], 90)


class DbIntegrationTest(unittest.TestCase):
    """落库：请求日志带费用，日/小时用量把费用累加起来。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'i.db'
        db._conn = None
        db.connect()
        pricing.invalidate()

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig_db

    def test_request_log_stores_cost(self) -> None:
        db.add_request_log(
            ts=int(PEAK_WEEKDAY), key_id=None, ip='127.0.0.1',
            model='deepseek-v4.1-flash', mapped_model='deepseek-v4.1-flash',
            status=200, prompt_tokens=1_000_000, completion_tokens=0,
            latency_ms=10, first_token_ms=5, ua='test', error=None, stream=1,
            credit=None, realm='cn', cache_hit_tokens=None, cache_miss_tokens=None,
            cache_write_tokens=None,
        )
        row = db.query_one('SELECT cost FROM request_logs ORDER BY id DESC LIMIT 1')
        self.assertAlmostEqual(float(row['cost']), 2.0, places=6)

    def test_unpriced_model_leaves_cost_null(self) -> None:
        db.add_request_log(
            ts=int(PEAK_WEEKDAY), key_id=None, ip='127.0.0.1',
            model='glm-5.3', mapped_model='glm-5.3', status=200,
            prompt_tokens=1000, completion_tokens=1000, latency_ms=10,
            first_token_ms=None, ua='test', error=None, stream=0, credit=None,
            realm='cn', cache_hit_tokens=None, cache_miss_tokens=None,
            cache_write_tokens=None,
        )
        row = db.query_one('SELECT cost FROM request_logs ORDER BY id DESC LIMIT 1')
        self.assertIsNone(row['cost'])

    def test_bump_usage_accumulates_cost(self) -> None:
        db.bump_usage(1, 'deepseek-flash', 1_000_000, 0, None, realm='cn',
                      cache_hit=None, cache_miss=None, ts=PEAK_WEEKDAY)
        daily = db.query_one('SELECT SUM(cost) AS c FROM usage_daily')
        self.assertAlmostEqual(float(daily['c']), 2.0, places=6)
        hourly = db.query_one('SELECT SUM(cost) AS c FROM usage_hourly')
        self.assertAlmostEqual(float(hourly['c']), 2.0, places=6)


if __name__ == '__main__':
    unittest.main()

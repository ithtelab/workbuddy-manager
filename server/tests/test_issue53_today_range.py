"""用量页的时段口径（issue #53）。

## 报告者指出的问题

同一屏两种口径：上方四张卡片是「今日 / 本周」，而下面的趋势图与两张分解表跟的是
时段选择器，选择器**默认近 30 天**、选项里也没有「今日」。于是很容易把 30 天的
数字当成今天的（他的原话：「很容易把近 30 天的数字当成今天的」）。

## 修法

选择器加「今日」（值 `1`）并设为默认，趋势图 / 按模型 / 按密钥一起跟随。

## 这里钉住的

  · 默认值是 1（今日），不是 30——这是本次要改的行为本身；
  · 四个取值（今日 / 7 / 30 / 90）传的是**同一个 days**（一个参数、三处跟随），
    不出现「图表与分解表各算各的」；
  · 后端「近 N 天且含今天」的语义：`days=1` = 今天一天。这是「今日」能用一个
     天数表达的依据——若哪天 `_since` 改成「今天往前 N 天」（不含今天），
     `days=1` 就会变成「昨天到今天」，界面的「今日」会静默地多算一天。
"""
from __future__ import annotations

import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import stats  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_PAGE = _ROOT / 'web' / 'app' / '(main)' / 'stats' / 'page.tsx'
_LOCALES = _ROOT / 'web' / 'lib' / 'i18n' / 'locales'


class TodayOptionTest(unittest.TestCase):
    """前端：默认今日、四个选项、三处跟随。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = _PAGE.read_text(encoding='utf-8')

    def test_default_range_is_today(self) -> None:
        m = re.search(r"useState\('(\d+)'\)", self.src)
        self.assertIsNotNone(m, '没找到时段 state 的初始值')
        self.assertEqual(m.group(1), '1', '时段默认值不是「今日」')

    def test_today_option_present(self) -> None:
        self.assertIn('<SelectItem value="1">', self.src)
        self.assertIn("t('stats.today')", self.src)

    def test_all_four_options_share_one_days_value(self) -> None:
        """四个选项都是同一个 days 的取值——不出现「图表另算一份」。"""
        opts = re.findall(r'<SelectItem value="(\d+)">', self.src)
        self.assertEqual(opts, ['1', '7', '30', '90'])
        # 一个 d 同时喂给 daily / byModel / byKey（三处跟随）
        # 自定义日期（issue #257）通过第三个参数传范围；三处仍共用同一个 d。
        for call in ('statsApi.daily(d, realm, range)', 'statsApi.byModel(d, realm, range)',
                     'statsApi.byKey(d, realm, range)'):
            self.assertIn(call, self.src, f'{call} 没有跟随时段')

    def test_fallback_matches_default(self) -> None:
        """`Number(days) || X` 的兜底值要与默认值一致，否则非法值时口径会跳变。"""
        m = re.search(r'return Number\(days\) \|\| (\d+);', self.src)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), '1')

    def test_subtitle_follows_the_window(self) -> None:
        """副标题必须跟着**实际粒度**走，不能写死。

        最早的实现里它写死「按天聚合」，于是「今日」那一屏（只有一个数据点）
        还写着「按天聚合」——正是用户反馈的「今日还是柱状图，不太对劲」的一半。
        现在图的粒度与副标题都从 `chartSpecFor()` 来，两者不可能再脱钩。
        """
        import re
        self.assertRegex(
            self.src,
            r"spec\.granularity === 'hour'[\s\S]{0,80}stats\.hourlyAgg",
            '副标题没有跟随粒度 —— 今日会写着「按天聚合」',
        )
        self.assertIn("stats.dailyAgg'", self.src, '多日仍然要说「按天聚合」')


class TodayPhrasesTest(unittest.TestCase):
    """五语言都要有「今日」与「按小时（当日汇总）」，不允许漏翻。"""

    def test_phrases_in_all_locales(self) -> None:
        import json
        for loc in ('zh-CN', 'zh-TW', 'en', 'ja', 'ko'):
            d = json.loads((_LOCALES / f'{loc}.json').read_text(encoding='utf-8'))
            for key in ('today', 'hourlyAgg'):
                self.assertIn(key, d.get('stats', {}), f'{loc} 缺 stats.{key}')
                self.assertTrue(str(d['stats'][key]).strip(), f'{loc} 的 stats.{key} 是空的')


class SinceSemanticsTest(unittest.TestCase):
    """后端：「近 N 天且含今天」——「今日」= days=1 的依据。"""

    def test_today_is_the_start_day(self) -> None:
        today = time.strftime('%Y-%m-%d', time.localtime())
        self.assertEqual(stats._since(1), today,
                         'days=1 不再是「今天一天」——界面的「今日」会多算一天')

    def test_seven_days_includes_today(self) -> None:
        start = stats._since(7)
        want = time.strftime('%Y-%m-%d', time.localtime(time.time() - 6 * 86400))
        self.assertEqual(start, want)

    def test_out_of_range_is_clamped(self) -> None:
        """越界值不能炸（既有约束：超大 int 在 SQLite 绑定时会溢出）。"""
        self.assertEqual(stats._since(0), stats._since(1))
        self.assertEqual(stats._since(99999999), stats._since(3650))
        self.assertEqual(stats._since('abc'), stats._since(30))


if __name__ == '__main__':
    unittest.main()

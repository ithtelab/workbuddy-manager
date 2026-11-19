"""跑 `web/lib/usage-chart.test.mjs`（趋势图粒度/形态的纯函数测试）。

与 `test_account_list` 同一套路：Node 直接跑 `.ts` 源码（type stripping），
没有 node 或版本过低时 skip，不阻塞后端套件。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'usage-chart.test.mjs'
_NODE = shutil.which('node')
_MIN_MAJOR = 22
_MAJOR = None
if _NODE:
    try:
        _out = subprocess.run([_NODE, '--version'], capture_output=True, text=True, timeout=20)
        _MAJOR = int((_out.stdout or 'v0').strip().lstrip('v').split('.')[0])
    except Exception:  # noqa: BLE001
        _MAJOR = None


class UsageChartBehaviourTest(unittest.TestCase):
    """趋势图的粒度/形态选择与序列补齐 —— 跑 Node 侧的真实实现。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_chart_spec_and_series(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')

        def run_once() -> subprocess.CompletedProcess:
            return subprocess.run(
                [_NODE, '--experimental-strip-types', str(_SCRIPT)],
                capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
            )

        proc = run_once()
        # Node 偶发进程级崩溃（整套测试并跑时遇到过）：与断言无关，只重试异常退出码
        if proc.returncode not in (0, 1):
            proc = run_once()
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'趋势图逻辑不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class UsageChartInvariantTest(unittest.TestCase):
    """源码级不变式：图必须**按范围换粒度**，不许退回「一律按天」。"""

    PAGE = _ROOT / 'web' / 'app' / '(main)' / 'stats' / 'page.tsx'
    LIB = _ROOT / 'web' / 'lib' / 'usage-chart.ts'

    def test_page_uses_the_granularity_table(self) -> None:
        code = self.PAGE.read_text(encoding='utf-8')
        self.assertIn('chartSpecFor(', code,
                      '统计页没有用 chartSpecFor —— 范围与粒度又脱钩了（今日会退回一根柱子）')
        self.assertIn('hourlySeries(', code, '没有用小时序列（今日仍然按天画）')
        self.assertIn('dailySeries(', code, '没有用按天序列（多日范围不再补零）')
        # 面积形态必须真的被渲染出来，不能只在纯函数里定义了却没人用
        self.assertIn('Area', code, '面积形态没接上（30/90 天仍会挤成一片柱子）')

    def test_today_branch_fetches_hourly(self) -> None:
        """`days === '1'` 时才请求小时端点 —— 其它范围不该白打一次请求。"""
        code = self.PAGE.read_text(encoding='utf-8')
        self.assertIn('statsApi.hourly(', code, '没有请求小时端点')
        self.assertRegex(
            code, r"days === '1'[\s\S]{0,120}statsApi\.hourly\(",
            '小时端点不是在「今日」分支里取的（要么永远在取、要么从不取）',
        )

    def test_lib_stays_runnable_without_node_modules(self) -> None:
        code = self.LIB.read_text(encoding='utf-8')
        import re
        bad = [m.group(0) for m in re.finditer(r'^import\s+(?!type\b)[^;]+;', code, re.M)]
        self.assertEqual(bad, [], f'usage-chart.ts 引入了运行时 import（Node 测试会挂）：{bad}')


if __name__ == '__main__':
    unittest.main()

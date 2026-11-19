"""跑 `web/lib/credits-flow-chart.test.mjs`（积分收支图数据选择与轴参数的纯函数测试）。

与 `test_usage_chart` 同一套路：Node 直接跑 `.ts` 源码（type stripping），
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
_SCRIPT = _ROOT / 'web' / 'lib' / 'credits-flow-chart.test.mjs'
_NODE = shutil.which('node')
_MIN_MAJOR = 22
_MAJOR = None
if _NODE:
    try:
        _out = subprocess.run([_NODE, '--version'], capture_output=True, text=True, timeout=20)
        _MAJOR = int((_out.stdout or 'v0').strip().lstrip('v').split('.')[0])
    except Exception:  # noqa: BLE001
        _MAJOR = None


class CreditsFlowChartBehaviourTest(unittest.TestCase):
    """积分收支图的序列选择与轴参数 —— 跑 Node 侧的真实实现。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_series_axis_and_top(self) -> None:
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
        self.assertEqual(proc.returncode, 0, f'积分收支图数据不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


if __name__ == '__main__':
    unittest.main()

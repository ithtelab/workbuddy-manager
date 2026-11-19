"""到期积分「按天归并」的行为测试（前端逻辑，借 Node 执行）。

为什么值得有：这段逻辑最容易错在**日切用的时区**——用 UTC 日切时，东八区会把
当天 08:00 前的到期算进前一天，分组算错而界面上完全看不出来（数字照样有、
日期也像个日期，只是归到了前一天）。这类错误没有运行时断言就只能等用户发现。

为什么用 Python 包一层：本仓库的测试套件是 Python 的（`pytest server/tests/`），
而这段逻辑在前端。没有 Node（或版本太旧）时**跳过**而不是失败——后端开发者
不该因为机器上没装 Node 就跑不了整个测试套件。

跑的是 `web/lib/display-prefs.test.mjs`，它直接 import `.ts` 源码（Node ≥ 22.6
的 type stripping），因此测的是**真实实现**，不是复制一份逻辑。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'display-prefs.test.mjs'
_NODE = shutil.which('node')

# 只跑 .mjs，不碰 .ts —— Node 的 type stripping 从 22.6 起才有
_MIN_MAJOR = 22


def _node_major() -> int | None:
    if not _NODE:
        return None
    try:
        out = subprocess.run([_NODE, '--version'], capture_output=True,
                             text=True, timeout=15).stdout.strip()
        return int(out.lstrip('v').split('.')[0])
    except Exception:  # noqa: BLE001
        return None


_MAJOR = _node_major()


class DisplayPrefsTest(unittest.TestCase):
    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_group_expiries_by_day(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')
        proc = subprocess.run(
            [_NODE, '--experimental-strip-types', str(_SCRIPT)],
            capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
        )
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'按天归并的行为不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')

    def test_script_covers_timezone_trap(self) -> None:
        """实现必须用**本地日历日**，而不是 UTC 日切。

        这条不变式在 UTC 机器上不会失败（两种写法结果相同），只在非 UTC 环境
        才暴露，所以用源码级断言钉住它——不能被「顺手简化」成 `at / 86400`。

        断言前先**剥掉注释**：实现里正解释着「不要用 at / 86400」，
        直接搜全文会把这句说明本身当成违规（这个坑第一次写就踩了）。
        """
        src = (_ROOT / 'web' / 'lib' / 'display-prefs.ts').read_text(encoding='utf-8')
        code = re.sub(r'/\*.*?\*/', '', src, flags=re.S)   # 块注释
        code = re.sub(r'//[^\n]*', '', code)               # 行注释
        self.assertIn('getFullYear()', code,
                      '按天归并必须用本地日历日（getFullYear/getMonth/getDate）')
        self.assertNotIn('86400', code,
                         '不能用 UTC 日切：东八区会把当天 08:00 前的到期算进前一天')


if __name__ == '__main__':
    unittest.main()


class CreditBadgeDayMergeTest(unittest.TestCase):
    """账号行到期角标按本地自然日合并（issue #434）。

    源码级守卫：同一天到期的多笔要合并成「当天合计」，否则只显示最早一笔会把
    「当天要没多少」低估（实测 90 + 100 + 100 只显示 90）。悬停明细必须保持逐包，
    那是对账用的。
    """

    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[2]
        self.src = (root / 'web' / 'components' / 'common' / 'accounts'
                    / 'CreditCountdown.tsx').read_text(encoding='utf-8')

    def test_badge_uses_day_grouping(self) -> None:
        self.assertIn('groupExpiriesByDay(expiries)[0]', self.src,
                      '角标必须先按自然日合并再取第一天')
        self.assertNotIn('expiries?.[0]', self.src,
                         '不能直接用第一笔（同日多笔会被低估）')

    def test_tooltip_keeps_per_package_lines(self) -> None:
        self.assertIn('all.map((e) =>', self.src,
                      '悬停明细必须逐包列出，不能被日合并')

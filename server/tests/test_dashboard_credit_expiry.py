"""仪表盘到期积分只统计当前版本的行为测试（前端逻辑，借 Node 执行）。

为什么值得有：余额已经按版本过滤，到期明细却扫过全部 uid。国内版更早的一笔
就会出现在国际版卡片上——金额、时刻、紧急色一起串。刷新账号换的是同一份
跨版本的 map，不按名单收的话刷新也改不掉。

跑的是 `web/lib/dashboard-credits.test.mjs`，它直接 import `.ts` 源码（Node ≥ 22.6
的 type stripping），因此测的是**真实实现**，不是复制一份逻辑。源码断言只补充
「页面真的把这份名单交给了那个函数」，不代替上面的行为测试。

为什么用 Python 包一层：本仓库的测试套件是 Python 的（`pytest server/tests/`），
而这段逻辑在前端。没有 Node（或版本太旧）时**跳过**而不是失败——后端开发者
不该因为机器上没装 Node 就跑不了整个测试套件。
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
_SCRIPT = _ROOT / 'web' / 'lib' / 'dashboard-credits.test.mjs'
_PAGE = _ROOT / 'web' / 'app' / '(main)' / 'dashboard' / 'page.tsx'
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


def _code(path: Path) -> str:
    """读出源码并剥掉注释（实现里正解释着「不要扫全表」，搜全文会误判）。"""
    src = path.read_text(encoding='utf-8')
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


class DashboardCreditExpiryTest(unittest.TestCase):
    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_scoped_expiries(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')
        proc = subprocess.run(
            [_NODE, '--experimental-strip-types', str(_SCRIPT)],
            capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
        )
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'按版本收口的到期明细不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')

    def test_dashboard_uses_scoped_collector(self) -> None:
        """最近一笔和按天归并都必须用当前版本的名单，且名单或元数据变了都重算。

        行为测试证明函数本身会过滤；这一条防止页面又改回扫 `creditsMeta` 全表，
        或者算出了名单却只在其中一条路上用。
        """
        code = _code(_PAGE)
        match = re.search(
            r'useMemo\(\s*\(\)\s*=>\s*collectScopedExpiries\(\s*creditTotals\.usableAccounts\s*,\s*creditsMeta\s*\)'
            r'\s*,\s*\[([^\]]*)\]',
            code,
        )
        self.assertIsNotNone(
            match,
            '仪表盘没有用当前版本可用账号的到期明细，'
            '或者没有放进会随名单与元数据重算的 useMemo',
        )
        deps = match.group(1)
        self.assertIn('creditTotals', deps, '可用账号名单变了，到期明细却不重算')
        self.assertIn('partitionCredits(scoped, liveCredits)', code, '积分统计未按当前版本过滤')
        self.assertIn('creditsMeta', deps, '刷新积分后元数据变了，到期明细却不重算')
        self.assertRegex(
            code, r'scopedExpiries\[0\]',
            '最近一笔没有取自当前版本的到期列表',
        )
        self.assertIn(
            'groupExpiriesByDay(scopedExpiries)', code,
            '按天归并没有用当前版本的到期列表',
        )
        self.assertNotIn(
            'Object.values(creditsMeta)', code,
            '又在扫全部版本的积分元数据——国际版会重新看到国内版的到期',
        )
        self.assertIn(
            "(a.realm ?? 'cn') === realm", code,
            'realm 为空的存量账号不再按国内版过滤',
        )


if __name__ == '__main__':
    unittest.main()

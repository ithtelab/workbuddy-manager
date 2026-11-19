"""首页快照的截断数与「查看全部账号」入口的阈值必须同一个数（PR #120）。

背景：健康快照只画前 N 条（`interleave(...).slice(0, N)`），N 是个写死的小数
（9）。快照被截断时用户看不到剩下的账号，所以 #120 加了一个跳账号页的入口。
它成立的前提只有一个：**入口的阈值和截断数是同一个数**。

这个前提一旦不同步，坏法很具体：把截断改成 12 而阈值还留着 9 —— 这时快照能画
12 条，只要账号正好 10~12 个，入口不会出现，用户看到 12 格以为「就这些」，而池
子里还有账号；反过来阈值比截断小，则会出现「点了入口进去，账号数并不比首页多」
的假入口。两种都是**只有真人点一遍才发现**的问题，类型系统和 i18n 守卫都看不见
（键在不在是 test_web_i18n 的事，这里管的是数值）。

所以这里只钉一条不变量：两个数字相等，各自都能被解析出来。另外用一份**故意写错
的合成源码**反向验证守卫本身有分辨力——否则正则哪天匹配不上，测试会以「找不到」
的方式静默变绿。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_DASHBOARD = _ROOT / 'web' / 'app' / '(main)' / 'dashboard' / 'page.tsx'

# 截断写法：interleave(scopedSlices).slice(0, 9)
_SLICE = re.compile(r'interleave\(\s*\w+\s*\)\s*\.\s*slice\(\s*0\s*,\s*(\d+)\s*\)')
# 入口写法：scoped.length > 9 && ( … <Link href="/accounts"> … )
_ENTRY = re.compile(
    r'(\w+)\.length\s*>\s*(\d+)\s*&&\s*(?=[\s\S]{0,400}?'
    r'<Link\s+href="/accounts")')


def _strip_comments(src: str) -> str:
    """注释里会**提到**这两个数字（解释为什么是 9），不剥掉会误判成两处写法。"""
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


def _limits(src: str) -> tuple[int | None, int | None, str | None]:
    """返回 (快照截断数, 入口阈值, 入口所在的那个变量名)。"""
    code = _strip_comments(src)
    slice_m = _SLICE.search(code)
    entry_m = _ENTRY.search(code)
    return (int(slice_m.group(1)) if slice_m else None,
            int(entry_m.group(2)) if entry_m else None,
            entry_m.group(1) if entry_m else None)


class SnapshotLimitSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.src = _DASHBOARD.read_text(encoding='utf-8')

    def test_both_numbers_are_parseable(self) -> None:
        """先证明正则抓得到东西——否则下面那条相等断言是空转。"""
        limit, threshold, var = _limits(self.src)
        self.assertIsNotNone(limit, '没能在 dashboard/page.tsx 里找到 slice(0, N)')
        self.assertIsNotNone(threshold, '没能找到「查看全部账号」入口的阈值判断')
        self.assertEqual(var, 'scoped',
                         f'入口阈值应基于 scoped（当前账号全集），实际是 {var}')

    def test_entry_threshold_equals_snapshot_limit(self) -> None:
        limit, threshold, _ = _limits(self.src)
        self.assertEqual(
            limit, threshold,
            f'快照截断 {limit} 条，而入口阈值是 {threshold}：两者必须相等，'
            '否则要么有账号却看不到入口，要么入口点进去并没有更多账号')

    def test_entry_navigates_to_accounts_page(self) -> None:
        """入口必须是**能跳转**的链接，不是一个点了没反应的空按钮。"""
        code = _strip_comments(self.src)
        m = _ENTRY.search(code)
        assert m is not None
        window = code[m.end():m.end() + 400]
        self.assertIn('<Link href="/accounts"', window)
        self.assertIn("t('dashboard.viewAllAccounts')", window)

    def test_guard_is_not_vacuous(self) -> None:
        """反向验证：把两个数字改不一样（模拟漏改一处），守卫必须能看出来。"""
        broken = """const snapshot = useMemo(() => interleave(scopedSlices).slice(0, 12), [x]);
          {scoped.length > 9 && (
            <Button asChild><Link href="/accounts">{t('dashboard.viewAllAccounts')}</Link></Button>)}"""
        limit, threshold, _ = _limits(broken)
        self.assertEqual((limit, threshold), (12, 9))
        self.assertNotEqual(limit, threshold, '本守卫对「两处不同步」没有分辨力')


if __name__ == '__main__':
    unittest.main()

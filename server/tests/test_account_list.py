"""账号列表的检索 / 筛选 / 排序 / 分页（前端逻辑，借 Node 执行）+ 两页的结构不变式。

为什么值得有：这批改的是**操作手感**，错了不会报错，只是列表看起来不对——

  · 排序把「没有积分数据」的账号当成 0 分排到最前 → 看着像「这些号都没钱了」，
    而事实是我们根本不知道它们有多少积分（与模型中心按倍率排序同一口径）；
  · 排序顺手改了入参数组 → React 拿到的还是同一个引用，点了排序没反应；
  · 分页页码越界不夹回 → 删掉一个账号后停在空页上，显示「第 3 / 2 页」+ 空表；
  · 筛选把列表换掉了、空态却还按「一个账号都没有」说 → 账号明明在，只是被他自己
    刚才的筛选挡住了，界面却说「暂无账号」。

前三条由 `web/lib/account-list.test.mjs` 的行为测试盖住，后一条只能在**源码形状**上
断言（行为测试证明不了「页面真的用了那个结果」）。跑的是真实实现，不是复制一份逻辑。

为什么用 Python 包一层：本仓库的测试套件是 Python 的（`pytest server/tests/`），
而这段逻辑在前端。没有 Node（或版本太旧）时**跳过**而不是失败。

（日志页那六个筛选项是否真的进了取数依赖，属「并发取数」的依赖分组语义，断言在
`test_async_state.py::test_logs_keeps_rows_while_paging`，不在本文件。）
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'account-list.test.mjs'
_PURE = _ROOT / 'web' / 'lib' / 'account-list.ts'
_ACCOUNTS = _ROOT / 'web' / 'app' / '(main)' / 'accounts' / 'page.tsx'
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
    """读出源码并**剥掉注释**（实现里正解释着「不要这么写」，搜全文会误判）。"""
    src = path.read_text(encoding='utf-8')
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


class AccountListBehaviourTest(unittest.TestCase):
    """跑 Node 侧的行为测试：检索 / 筛选 / 排序 / 分页的真实实现。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_filter_sort_paginate(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')

        def run_once() -> subprocess.CompletedProcess:
            return subprocess.run(
                [_NODE, '--experimental-strip-types', str(_SCRIPT)],
                capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
            )

        proc = run_once()
        # Node 偶发进程级崩溃（整套测试并跑时遇到过 0xC0000409）：不是断言失败，也与
        # 被测逻辑无关，却会把整批测试染红。只对异常退出码重试一次；rc=1（脚本自己
        # 判失败）不重试，免得这条守卫变成「跑两遍总能绿」的空转。
        if proc.returncode not in (0, 1):
            proc = run_once()
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'账号列表逻辑不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class AccountListInvariantTest(unittest.TestCase):
    """行为测试盖不到的前提与结构不变式。"""

    def test_module_stays_runnable_without_node_modules(self) -> None:
        """`account-list.ts` 不得出现**运行时** import。

        这是上面那条 Node 测试能跑起来的前提：`.mjs` 直接 `import './account-list.ts'`，
        而 Node 的 ESM 解析**要求带扩展名**，仓库的 app 代码却一律不写扩展名。
        一旦本模块 import 了别的模块的**值**，解析就会失败——症状是「Cannot find
        module」，看起来像路径写错，而真正的修法是「不要在这里复用那个值」
        （例如「账号 → 4 组」的映射改成由调用方以 `groupOf` 传进来）。
        """
        offenders = [
            line.strip() for line in _code(_PURE).splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(
            offenders, [],
            'account-list.ts 出现了运行时 import：\n  ' + '\n  '.join(offenders)
            + '\n本模块被 web/lib/account-list.test.mjs 直接 import，引入运行时 import '
              '会让那条测试以「找不到模块」失败。要复用别的模块的函数，请把那个'
              '模块也做成零运行时依赖的，或者让调用方把值传进来。',
        )

    def test_list_renders_the_filtered_rows(self) -> None:
        """列表必须渲染**筛选 + 分页之后**的行，两种布局都是。

        这是这批改动里最容易「看起来做完、其实没用」的一处：把搜索框、状态筛选、
        排序、页码条都加上去，而列表仍然 `visible.map(...)` —— 那么每个控件都成了
        **装饰**：能点、能选、界面纹丝不动，而且不报错。用户会以为筛选没生效或者
        号池里就这几个号。

        断言「出现两次」而不是「出现过」：手机端卡片与桌面端表格是两个 map，
        只改一处的话另一种布局下筛选依然是装饰。
        """
        code = _code(_ACCOUNTS)
        self.assertEqual(
            code.count('paged.rows.map('), 2,
            '账号列表没有（或只有一处）渲染筛选+分页之后的行——手机端卡片与桌面端'
            '表格都要用 paged.rows。仍然渲染 visible 的话，搜索/筛选/排序/翻页'
            '四个控件全是装饰：能点、能选、列表不动，而且不报错。',
        )

    def test_realm_empty_state_is_not_the_no_accounts_lie(self) -> None:
        """「当前版本没有账号」必须与「一个账号都没有」分开说。

        切到国际版而池子里只有国内版账号时，原来说的是「暂无账号」——读起来像号池
        是空的，而账号明明在，只是版本不对。这正是批次 3 要修的那类谎话，而且它跟
        取数无关（数据取到了，是**筛选**把它滤空了），所以批次 1 的加载态修不了它。

        判据落在「有没有这个分支」上：必须存在一个只由 `visible.length === 0`
        （而不是 `merged.length === 0`）触发的空态。
        """
        code = _code(_ACCOUNTS)
        self.assertIn(
            'noAccountsInRealm', code,
            '没有区分「当前版本没有账号」与「一个账号都没有」：前者会被渲染成'
            '「暂无账号」，而账号其实在池子里，只是版本不对',
        )
        self.assertRegex(
            code, r'noAccountsInRealm\s*\?',
            '`noAccountsInRealm` 算了却没有用在分支上',
        )
        self.assertIn(
            "t('accounts.emptyRealmTitle'", code,
            '「当前版本没有账号」没有自己的文案',
        )

    def test_filtered_empty_state_offers_a_way_back(self) -> None:
        """被筛选滤空时，必须说清是筛选造成的，并给一键清除的入口。

        说「暂无账号」比说错版本更糟：账号就在下面，只是被用户自己刚才的筛选挡住了。
        而只说「没有匹配」而不给清除入口，用户得自己回忆刚才改了什么、逐个还原。
        """
        code = _code(_ACCOUNTS)
        self.assertRegex(
            code, r'noMatch\s*\?',
            '没有「被筛选滤空」这一支：会把「筛选没匹配到」说成「暂无账号」',
        )
        self.assertIn(
            "t('accounts.noMatchTitle'", code,
            '「被筛选滤空」没有自己的文案',
        )
        self.assertIn(
            'clearListFilters', code,
            '滤空时没有一键清除筛选的入口——用户只能自己逐个还原刚才改过的条件',
        )


if __name__ == '__main__':
    unittest.main()

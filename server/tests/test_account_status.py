"""账号池状态判定与「跨分组聚合」纯逻辑的行为测试（前端逻辑，借 Node 执行）。

为什么值得有：这段逻辑错起来**界面不报错**，只是数字变得不对——

  · 池状态取不到（上游 `connected: false`）时判成「不在池里」→ 每个账号都显示
    「未加载 / 账号文件可能有问题」，而事实是连不上上游。有心跳的页面 30 秒后
    会自己「恢复」，用户会以为账号随机坏掉（这条误报曾经真实发生过）；
  · 合并时不打分组标记 → 两个分组的同名账号文件撞成同一个 React key，列表少
    一条、或者状态串到别的号上；
  · 跨组先拼账号、再拿某一组的 /status 去并 → 其余组的账号整批被标成「未加载」；
  · 把「没配账号目录的分组」也算进分母 → 「共 N 个分组」虚高，而那几组的账号
    接口必然 409；
  · 快照「一组接一组」平铺 → 前 9 格被默认分组占满，其余组一条都看不到。

五条都不会报错，只会让用户看到一个**看起来很正常**的错数字——正是上游
issue #94 问题 2 的形态（仪表盘只报默认分组，数字合理、图表正常，用户不会怀疑，
也永远不会自己变对）。

为什么用 Python 包一层：本仓库的测试套件是 Python 的（`pytest server/tests/`），
而这段逻辑在前端。没有 Node（或版本太旧）时**跳过**而不是失败——后端开发者
不该因为机器上没装 Node 就跑不了整个测试套件。

跑的是 `web/lib/account-status.test.mjs`，它直接 import `.ts` 源码（Node ≥ 22.6
的 type stripping），因此测的是**真实实现**，不是复制一份逻辑。这段逻辑之所以
能放在 `web/lib/account-status.ts`，是因为该模块**没有任何运行时 import**
（只有 `import type`，编译后被抹掉）——那个 .mjs 只需 Node 就能跑，不需要
`web/node_modules`。下面有一条断言专门钉住这个前提：它是「测试跑得起来」的
前提，破了之后表现为「找不到模块」，很容易被误判成路径写错。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'account-status.test.mjs'
_PURE = _ROOT / 'web' / 'lib' / 'account-status.ts'
_DASHBOARD = _ROOT / 'web' / 'app' / '(main)' / 'dashboard' / 'page.tsx'
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
    """读出源码并**剥掉注释**。

    源码级断言必须先剥注释：实现里正解释着「不要这么写」，直接搜全文会把这句
    说明本身当成违规。（`test_display_prefs.py` 的时区守卫第一次写就踩了这个坑。）
    """
    src = path.read_text(encoding='utf-8')
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)   # 块注释
    return re.sub(r'//[^\n]*', '', src)               # 行注释


def _body(code: str, header: str) -> str:
    """取一个模块级函数的函数体（从 `header` 那一行到下一个顶格的 `}`）。

    顶层函数的收尾 `}` 一定在行首；函数体内部的花括号都有缩进，所以这个界碑是
    可靠的，不需要做括号配对。
    """
    at = code.find(header)
    if at < 0:
        raise AssertionError(f'找不到 {header}')
    end = code.find('\n}', at)
    if end < 0:
        raise AssertionError(f'{header} 没有找到收尾的顶格 }}')
    return code[at:end]


class AccountStatusBehaviourTest(unittest.TestCase):
    """跑 Node 侧的行为测试：池状态合并、可用性分档与跨分组聚合的真实实现。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_availability_and_aggregation(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')
        proc = subprocess.run(
            [_NODE, '--experimental-strip-types', str(_SCRIPT)],
            capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
        )
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'账号状态/聚合逻辑不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class AccountStatusInvariantTest(unittest.TestCase):
    """行为测试盖不到的前提与结构不变式。"""

    def test_module_stays_runnable_without_node_modules(self) -> None:
        """`account-status.ts` 不得出现**运行时** import。

        这是上面那条 Node 测试能跑起来的前提：`.mjs` 直接 `import './account-status.ts'`，
        而 Node 的 ESM 解析**要求带扩展名**，仓库的 app 代码却一律不写扩展名。
        一旦本模块 import 了别的模块的**值**（不只是类型），解析就会失败——症状是
        「Cannot find module」，看起来像路径写错，而真正的修法是「把被依赖的模块
        也做成零运行时依赖的」或者「不要在这里复用那个值」。

        `import type` 不算：它在编译后整条消失，Node 的 type stripping 也会抹掉它。
        """
        offenders = [
            line.strip() for line in _code(_PURE).splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(
            offenders, [],
            'account-status.ts 出现了运行时 import：\n  ' + '\n  '.join(offenders)
            + '\n本模块被 web/lib/account-status.test.mjs 直接 import（Node 的 ESM '
              '解析要求带扩展名，而 app 代码一律不写扩展名），引入运行时 import 会让'
              '那条测试以「找不到模块」失败。要复用别的模块的函数，请把那个模块也'
              '做成零运行时依赖的。',
        )


class DashboardGroupCoverageTest(unittest.TestCase):
    """仪表盘必须覆盖**全部**分组，而不是只报默认分组（上游 issue #94 问题 2）。

    这一批的结构不变式都在**源码形状**上，因为它们在行为测试里表达不出来：
    行为测试能证明「合并函数写对了」，证明不了「页面真的按分组各取了一份」。
    而后者才是这个 bug 的本体——`accountApi.list()` 不传分组时后端给的就是默认
    分组，代码看起来毫无问题，界面也不会报错，只是数字少了 N-1 个池子。

    每条都对应一次真实浏览器验收里能观察到的现象（见本批的 mock 后端验收：
    两个分组各 2 / 3 个账号，首页「账号总数」必须是 5）。
    """

    def test_every_group_is_queried(self) -> None:
        """账号 / 池状态 / 积分都必须**按分组**取，不能出现不传分组的调用。

        `accountApi.list()`、`upstreamApi.status()`、`refreshCredits(false)`
        不传分组时，后端 `routers/accounts.py::_group(None)` 落到默认分组——
        有多个分组时只报 1/N 个池子，而那个数看起来完全正常。这是本条要防的
        唯一一件事，所以断言写成「不许出现空参数的调用」，与变量名无关。
        """
        code = _code(_DASHBOARD)
        for pattern, api in (
            (r'accountApi\.list\(\s*\)', 'accountApi.list()'),
            (r'upstreamApi\.status\(\s*\)', 'upstreamApi.status()'),
            (r'refreshCredits\(\s*false\s*\)', 'refreshCredits(false)'),
        ):
            self.assertNotRegex(
                code, pattern,
                f'仪表盘又出现了不传分组的 {api}——它只返回默认分组，'
                '等于把「只报 1/N 个池子」那个谎话又演一遍（上游 issue #94 问题 2）',
            )
        # 反面：确实存在按分组取的地方（否则上面的断言可能是「压根没取数」换来的）
        self.assertIn('accountGroups(', code,
                      '仪表盘没有按分组扇出取数（accountGroups 是「哪些分组有账号」的判据）')

    def test_group_list_is_fetched_inside_the_fan_out(self) -> None:
        """分组清单必须在**扇出取数的那个函数里**一起取。

        这条是这个 bug 最容易复发的地方。看起来更「干净」的写法是：用一个独立的
        hook 取分组清单，扇出时读它的结果。但那两件事之间**没有先后关系**——
        扇出先跑完时清单还是空的，而它不会因为清单后到而重跑，于是静默退化成
        「只报默认分组」：界面正常、数字正常、只是少了几组。

        所以断言「扇出函数体里出现了 upstreamsApi.list()」而不是「页面里有
        upstreamsApi.list()」。
        """
        body = _body(_code(_DASHBOARD), 'async function loadPoolSnapshot')
        self.assertIn('upstreamsApi.list()', body,
                      '按分组扇出的取数函数没有自己取分组清单——若它依赖另一个 hook 的'
                      '结果，首帧清单还是空的，会静默退化成「只报默认分组」')
        self.assertIn('accountGroups(', body,
                      '扇出时没有筛掉「没配本地账号目录」的分组：那些分组没有账号，'
                      '而且账号接口会 409，纳进来只会多出必然失败的请求')

    def test_pool_status_is_merged_per_group(self) -> None:
        """池状态必须**逐组**合并（走 `tagGroup`），不能在页面里自己跨组拼。

        池状态合并的方向性很强：`mergePoolStatus` 是把「**这一份** /status 快照」
        并到「**这一份**账号列表」上。若先跨组拼账号、再拿某一组的 /status 去并，
        其余组的账号会因为「不在这份池里」被整批判成 `notLoaded`——界面把「问错了
        上游」说成「账号文件坏了」，而且有心跳的页面 30 秒后会自己「恢复」。
        """
        code = _code(_DASHBOARD)
        self.assertIn('tagGroup(', code,
                      '仪表盘没有用 tagGroup 逐组合并池状态与分组标记')
        self.assertNotIn(
            'mergePoolStatus(', code,
            '仪表盘自己调了 mergePoolStatus——跨组合并必须走 tagGroup，'
            '它同时负责「只并本组的池状态」与「打上分组标记」两件事，'
            '自己拼容易漏掉后一件（同名账号文件会撞成同一个 React key）',
        )

    def test_snapshot_key_carries_the_group(self) -> None:
        """快照的 React key 必须带分组。

        `file` 只在分组内唯一：两个分组可以有同名账号文件（`sub2api.json` 这类是
        扫码登录的默认文件名）。合并后还用 `a.file` 当 key，两条会撞成一个——
        列表少一条、或者状态串到别的号上，而两种都不会报错。
        """
        code = _code(_DASHBOARD)
        self.assertRegex(
            code, r'key=\{accountKey\(',
            '快照的 key 不是 accountKey(...)：合并多个分组后，同名账号文件会撞成'
            '同一个 key（列表少一条 / 状态串号）',
        )

    def test_all_groups_failing_is_reported_as_a_failure(self) -> None:
        """一个分组都没取到时必须**抛**，不能吞成空数组。

        吞掉的后果：「取不到」会被渲染成「暂无账号」——那是在断言「你一个账号都
        没有」。同一类坑在统计页已经踩过一次（上游统计把失败吞成
        `{available: false}`，于是五个接口全挂时整页错误态根本不出现）。

        逐个分组的失败**不**抛（那是 `allSettled` 的用途）：某组的上游没单独部署
        时，用户还要能看到另外几组，让整页变成错误态反而更糟。所以判据是
        「全部都失败」，不是「有一个失败」。
        """
        body = _body(_code(_DASHBOARD), 'async function loadPoolSnapshot')
        self.assertIn('allSettled', body,
                      '扇出没有逐组独立成败：某组的上游没部署时整页会变错误态，'
                      '用户连另外几组都看不到')
        # ⚠️ 判据必须落在 `every(` **之后**。
        #
        # 这里原本只写 `assertIn('throw', body)`，而那是**空转**的：函数体里
        # 本来就有一个 `if (acc.status === 'rejected') throw acc.reason;`（单组失败
        # 就抛，属另一件事）。把「全部失败才抛」整条删掉，那个 `throw` 还在，
        # 断言照样通过——变异测试实测确认过。
        # 「全部失败」的判据是 `slices.every((s) => s.error)`，所以只看它之后。
        after_every = body[body.find('every('):] if 'every(' in body else ''
        self.assertIn('throw', after_every,
                      '扇出把所有分组都失败吞成了空数组——那样「取不到」会被渲染成'
                      '「暂无账号」，等于告诉用户「你一个账号都没有」')

    def test_failed_group_keeps_its_upstream_status(self) -> None:
        """账号取不到时，**池状态若取到了要留下**，不能连带丢掉。

        两者是两个独立的请求，而「上游连得上、账号接口坏了」是很常见的现场
        （账号目录没配好、权限不对、文件坏了）。把状态一起丢掉，面板就会把
        「上游正常」说成「未获取到上游状态」，用户于是跑去查上游连接——方向是错的。

        ⚠️ 这条是**看截图**发现的：B1 段的断言当时全绿（账号数、提示文案都对），
        是归档的截图里甲组那一块写着「未获取到上游状态」才暴露出来。所以断言必须
        落在**面板显示什么**上，而不只是「账号数对不对」。
        """
        body = _body(_code(_DASHBOARD), 'async function loadPoolSnapshot')
        self.assertNotIn(
            'throw acc.reason', body,
            '账号取不到时直接抛了：那一组的池状态即使取到了也被丢掉，面板会把'
            '「上游正常、账号接口失败」说成「未获取到上游状态」，把用户引去查上游连接',
        )
        at = body.find("acc.status === 'rejected'")
        self.assertGreater(at, -1, '扇出里没有「账号取不到」的分支')
        branch = body[at:at + 400]
        self.assertIn('return', branch, '「账号取不到」的分支没有返回切片')
        self.assertIn('upstream', branch,
                      '「账号取不到」的分支没有把已取到的池状态带上——'
                      '面板会因此丢掉这一组的连接状态与池计数')

    def test_failed_pool_does_not_render_the_empty_state(self) -> None:
        """账号那一块没取到时，必须收起「暂无账号」，且不能渲染 0。

        首页四份数据里只有账号那一份挂掉时，整页错误态**不会**出现（其余三份
        有值，`isInitialFailed` 要求全部失败）。此时卡片会显示「账号总数 0」、
        快照会显示「暂无账号」——两句都是在断言「你一个账号都没有」。
        """
        code = _code(_DASHBOARD)
        self.assertRegex(
            code, r'poolFailed\s*=\s*.pool.\s+in\s+errors',
            '仪表盘没有「账号那一块没取到」这个判据',
        )
        self.assertRegex(
            code, r':\s*poolFailed\s*\?\s*null',
            '「暂无账号」没有在账号取数失败时收起来——那等于告诉用户'
            '「你一个账号都没有」，而事实是没取到',
        )
        self.assertIn('acctCard', code,
                      '账号类卡片没有「取不到时给 —」的处理：直接渲染 scoped.length '
                      '会得到 0，而 0 是在断言「一个账号都没有」')
        self.assertIn("t('dashboard.accountsLoadFailed')", code,
                      '账号那一块取不到时没有对应的说明文案')

    def test_partial_group_failure_is_stated(self) -> None:
        """有分组没取到时必须说明「下面的数字不含它们」。

        不说的话，聚合出来的数就又是一个「看起来正常、其实少了几组」的数——
        正是这次要修的形态。所以「部分失败」不能只是静默降级。
        """
        code = _code(_DASHBOARD)
        self.assertIn("t('dashboard.groupsPartial'", code,
                      '有分组取数失败时没有提示：聚合出来的数会静默少掉那几组，'
                      '和原来的「只报默认分组」是同一类谎话')
        self.assertRegex(
            code, r'failedGroups\s*=\s*slices\.filter\(',
            '没有从切片里挑出失败的那几组',
        )

    def test_scope_caption_only_when_multiple_groups(self) -> None:
        """统计范围说明只在**多于一个分组**时出现。

        单分组时那句话是废话，而绝大多数部署就是单分组——多出来的文案会把
        「正常」的界面改花，也让「有变化」失去信号意义。

        文案本身不是装饰：它是用户判断「这个数是不是全池的」的唯一依据，也正是
        issue 里那句「也希望界面 / 文档有明确说明」。
        """
        code = _code(_DASHBOARD)
        self.assertIn("t('dashboard.scopeAllGroups'", code,
                      '没有说明统计范围——用户无法判断首页的数字是不是全池的')
        # ⚠️ 判据要**紧贴文案**。原本写的是 `assertRegex(code, r'multiGroup\s*&&')`，
        # 那是空转的：`multiGroup &&` 在本文件里还出现在面板分隔线与快照胶囊上，
        # 把范围说明的条件删掉，另外两处照样满足它（变异测试实测确认过）。
        # 所以要求 `multiGroup && (` → `<p …>` → 紧跟着那句文案，三点连成一线。
        self.assertRegex(
            code,
            r"multiGroup\s*&&\s*\(\s*<p[^>]*>\s*\{t\('dashboard\.scopeAllGroups'",
            '统计范围说明没有加「多于一个分组」的条件：单分组时它会变成一句废话',
        )

    def test_upstream_panel_is_per_group_not_summed(self) -> None:
        """「反代上游」面板必须**按分组各画一块**，不能把各组的数加起来。

        两个分组可以指向**同一套上游实例**（新建分组时地址默认沿用默认分组的），
        求和会把同一套实例数两遍——那比原来的「只报默认分组」更糟，因为它是
        **错的**而不是缺的。每组一块、各显示各自的数，两块一样恰好说明这两个
        分组指着同一个地方。

        用 `poolTotals(` 而不是自己读 `realm_totals` 也是这条的一部分：那个函数
        编码了「一个分组的口径」以及「老上游没有 realm_totals 时退回顶层汇总并
        标注」两件事。
        """
        code = _code(_DASHBOARD)
        self.assertIn('poolTotals(', code,
                      '「反代上游」面板没有走 poolTotals（一个分组的口径）')
        # ⚠️ 判据必须钉住**面板里那一次** `slices.map`。
        # 原本写的是 `assertRegex(code, r'slices\.map\(')`，那是空转的：
        # `scopedSlices` 的定义里也有 `slices.map((s) => ({…}))`（第 242 行），
        # 把面板改成只画第一个分组，那一处照样满足它（变异测试实测确认过）。
        # 所以要求 `slices.map(…)` 之后不远处出现 `<UpstreamBlock`——那才是
        # 「每个分组一块」的渲染点。
        self.assertRegex(
            code,
            r'(?s)slices\.map\(.{0,400}?<UpstreamBlock',
            '「反代上游」面板没有按分组逐个渲染（要求 slices.map 之后出现 UpstreamBlock）',
        )


if __name__ == '__main__':
    unittest.main()

"""「并发取数」状态推导的行为测试（前端逻辑，借 Node 执行）。

为什么值得有：这段逻辑错起来**界面不报错**，只是变得不对劲——
  · 拿「请求在飞」当加载态 → 有心跳轮询的页面每 30 秒闪一次骨架；
  · 刷新时整体替换而不是合并 → 心跳里任何一个请求超时就把内容清空；
  · 首屏全失败时不报错 → 用户看到「暂无账号」，而数据其实是没取到。
三条都表现为「看起来只是卡了一下」，没人会为此提 issue。

为什么用 Python 包一层：本仓库的测试套件是 Python 的（`pytest server/tests/`），
而这段逻辑在前端。没有 Node（或版本太旧）时**跳过**而不是失败——后端开发者
不该因为机器上没装 Node 就跑不了整个测试套件。

跑的是 `web/lib/async-state.test.mjs`，它直接 import `.ts` 源码（Node ≥ 22.6
的 type stripping），因此测的是**真实实现**，不是复制一份逻辑。之所以把待测
逻辑单独放在 `web/lib/async-state.ts`，就是为了让它**零依赖**：那个 .mjs 只需
Node 就能跑，不需要 `web/node_modules`（hook 本体 import 了 react，直接测它
会把「没装前端依赖」变成测试失败，而不是跳过）。

除行为测试外，本文件还钉若干条源码级不变式——它们要么在行为测试里表达不出来
（顺序、依赖分组、文件是否存在），要么是「测试别空转」的前提。
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
_SCRIPT = _ROOT / 'web' / 'lib' / 'async-state.test.mjs'
_PURE = _ROOT / 'web' / 'lib' / 'async-state.ts'
_HOOK = _ROOT / 'web' / 'lib' / 'use-async-data.ts'
_MAIN = _ROOT / 'web' / 'app' / '(main)'
_LOGS = _MAIN / 'logs' / 'page.tsx'
_STATS = _MAIN / 'stats' / 'page.tsx'
_TASKS = _MAIN / 'tasks' / 'page.tsx'
_NODE = shutil.which('node')

# 接入状态系统的页面 → 它们在「数据还没到」时会说的那些谎话。
#
# 判据不是「有没有用 useAsyncAll」，而是「首屏守卫排在谎话前面没有」：内容区里的
# 空状态（「暂无日志」「暂无数据」）若在数据还没取到时渲染出来，等于告诉用户
# 「你没有数据」——而事实是还没取到。**顺序就是这条不变式的全部。**
_PAGES_WITH_LIES = {
    'dashboard/page.tsx': [
        "t('dashboard.noAccounts')",      # 「暂无账号」
        "t('dashboard.noCallData')",      # 「暂无调用数据」
    ],
    'logs/page.tsx': [
        "t('logs.emptyTitle')",           # 「暂无日志」
        "t('logs.pageInfo'",              # 「第 1 页 / 共 1 页」——数据没到时这个页码也是假的
    ],
    'stats/page.tsx': [
        "t('stats.emptyTitle')",          # 两张分解表的「暂无数据」
        "t('stats.noUsageData')",         # 趋势图的「暂无数据」
    ],
    'playground/page.tsx': [
        "t('playground.noModels')",       # 「暂无可用模型」
    ],
    'tasks/page.tsx': [
        "t('tasks.checkinEmpty')",        # 「暂无签到记录」——等于说「你没签过到」
        "t('tasks.noTaskRecords')",       # 「暂无自动任务记录」——等于说采集器没跑过
        "t('tasks.rawLogNote')",          # 上游原始日志的「这里没有记录不代表没执行」
    ],
}

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
    说明本身当成违规。（`test_display_prefs.py` 的时区守卫第一次写就踩了这个坑，
    这里照抄它的做法。）
    """
    src = path.read_text(encoding='utf-8')
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)   # 块注释
    return re.sub(r'//[^\n]*', '', src)               # 行注释


def _hook_args(src: str, nth: int = 0) -> list[str]:
    """取出第 nth 个 `useAsyncAll(` 调用的实参文本，按顶层逗号切成几段。

    为什么不用正则：实参里嵌着对象字面量、内联箭头函数、`try/catch` 语句块，
    正则数不清括号，会切到半个实参上去。这里做一次真正的括号配对扫描。

    返回空列表表示没找到那么多个调用。
    """
    needle = 'useAsyncAll('
    pos = -1
    for _ in range(nth + 1):
        pos = src.find(needle, pos + 1)
        if pos < 0:
            return []
    i = pos + len(needle)
    depth = 1
    args: list[str] = []
    cur = ''
    while i < len(src):
        ch = src[i]
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
            if depth == 0:
                break
        if ch == ',' and depth == 1:
            args.append(cur)
            cur = ''
        else:
            cur += ch
        i += 1
    args.append(cur)
    return [a.strip() for a in args]


class AsyncStateBehaviourTest(unittest.TestCase):
    """跑 Node 侧的行为测试：状态推导与逐项合并的真实实现。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_state_derivation(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')
        proc = subprocess.run(
            [_NODE, '--experimental-strip-types', str(_SCRIPT)],
            capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
        )
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'取数状态推导不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class AsyncStateInvariantTest(unittest.TestCase):
    """行为测试盖不到的不变式（顺序、文件存在性、测试是否空转）。"""

    def test_hook_uses_the_tested_module(self) -> None:
        """hook 必须真的调用 async-state 的两个函数。

        否则上面那个 .mjs 测的是「一份没人用的实现」——测试全绿，而页面上跑的是
        hook 里另写的一套。这种空转最难发现：断言全过，问题照旧。
        """
        code = _code(_HOOK)
        for fn in ('asyncFlags(', 'splitSettled('):
            self.assertIn(fn, code,
                          f'{_HOOK.name} 没有调用 {fn}——被测逻辑与线上逻辑已经脱节')

    def test_refresh_merges_instead_of_replacing(self) -> None:
        """刷新时必须把新值**合并**到已有值上，不能整体替换。

        整体替换的后果：心跳里任何一个请求超时，都会把已经显示出来的内容清空
        ——用户正看着的数字突然整页消失，而失败的可能只是 5 份数据里的 1 份。
        """
        code = _code(_HOOK)
        self.assertIn('...prev', code,
                      '刷新分支没有合并上一次的值：某个请求失败会把内容清空')
        self.assertIn('fresh.values', code, '没看到把本次结果并进去')

    def test_hook_separates_context_from_query(self) -> None:
        """hook 必须调用 `depMode`，并且把两组依赖的指纹**一路接到它面前**。

        同 `test_hook_uses_the_tested_module` 的道理：`depMode` 的行为测试若跑的是
        一份 hook 里没人用的实现，测试全绿而线上照旧——这种空转最难发现。

        所以这里钉的不是「调用了 depMode」这一句，而是整条链：
        `refreshDeps` → `refreshKey` → `next.query` → `depMode`。中间断任何一节，
        查询范围变化都不会触发重取——用户点「下一页」没反应，而测试照样绿。
        """
        code = _code(_HOOK)
        self.assertIn('depMode(', code,
                      'hook 没有调用 depMode——「换上下文」与「换查询范围」的区别没有生效')
        self.assertIn('refreshDeps', code,
                      'hook 没有接收「只重取、不清空」的那组依赖')
        self.assertRegex(code, r'depsKey\s*=\s*JSON\.stringify\(deps\)',
                         'hook 没把「数据上下文」序列化成指纹')
        self.assertRegex(code, r'refreshKey\s*=\s*JSON\.stringify\(refreshDeps\)',
                         'hook 没把「查询范围」序列化成指纹')
        self.assertRegex(code, r'next:\s*DepPrints\s*=\s*\{[^}]*context:\s*depsKey',
                         '交给 depMode 的指纹里没有「数据上下文」')
        self.assertRegex(code, r'next:\s*DepPrints\s*=\s*\{[^}]*query:\s*refreshKey',
                         '交给 depMode 的指纹里没有「查询范围」——这样它永远看不到查询'
                         '变化，翻页 / 改时段不会重取（点了没反应）')

    def test_logs_keeps_rows_while_paging(self) -> None:
        """日志页必须把「页码 / 天数」放进**第三**个参数（静默重取）。

        第二组依赖（deps）的语义是「换了一个数据上下文」——变了就清空重取、显示骨架。
        页码若被放进去，**每翻一页都会闪一次骨架**；而翻页是高频操作，看起来像
        页面在抽搐。所以 page / days 必须走第三组 refreshDeps，realm 走第二组。
        """
        code = _code(_LOGS)
        self.assertRegex(
            code, r'\[realm\],\s*\n\s*\[page, days\],',
            'logs 页的依赖分组不对：page/days 应作为第三个参数（变了只重取、不清空），'
            'realm 作为第二个参数（变了清空重取）。放错会让每次翻页都闪一次骨架。',
        )

    def test_stats_keeps_upstream_out_of_the_main_group(self) -> None:
        """用量统计页的上游那份数据必须**单独一个 hook**，不能和本页四份混在一起。

        上游统计的取数函数把失败吞成 `{available: false}` 而不是抛错——因为它取不到
        是常态（上游没起来、镜像太旧），不该为它弹提示。代价是它**永远算一次成功的
        取数**：混在一起的话，五份数据全挂时它照样有值，「一份都没取到」的判据就被
        顶掉了——页面不进整页错误态，反而照常渲染四张 0 卡片和「暂无数据」，正是
        这一批要修的那句谎话。

        这条不是从代码读出来的：真实浏览器验收时把五个接口全打成 500，整页错误态
        根本没出现，页面显示的是「部分数据加载失败 + 四张 0 + 暂无数据」。
        """
        code = _code(_STATS)
        self.assertIn('available: false', code,
                      '上游取数不再把失败吞成 {available: false}——本断言的前提变了，'
                      '请重新确认它是否还需要单独一个 hook')
        args = _hook_args(code, 0)
        self.assertGreaterEqual(len(args), 2, '找不到 stats 页第一个 useAsyncAll 调用')
        self.assertNotIn('upstream', args[0],
                         '上游统计被放回了本页四份数据那一组：它把失败吞成 '
                         '{available: false}，五份全挂时会被当成「有数据」，整页错误态'
                         '就不再出现')
        second = _hook_args(code, 1)
        self.assertTrue(second and 'upstream' in second[0],
                        'stats 页没有第二个 useAsyncAll 把上游统计单独接起来')

    def test_pages_guard_before_rendering_empty_states(self) -> None:
        """每个接入状态系统的页面，首屏守卫都必须排在「没有数据」那些话之前。

        这一条原本只盯 dashboard，现在扩到本批接入的全部页面——它们犯的是同一个错：
        内容区里的空状态（「暂无账号」「暂无日志」「暂无数据」「暂无可用模型」）在数据
        还没取到时就渲染出来，等于告诉用户「你没有数据」，而事实是还没取到。
        用户据此会去排查账号、排查配置，方向完全错了。

        所以断言的是**顺序**：守卫（骨架 / 错误分支）必须先返回，把那些话挡在后面。
        """
        for rel, lies in _PAGES_WITH_LIES.items():
            with self.subTest(page=rel):
                code = _code(_MAIN / rel)
                guard = code.find('isInitialFailed || isInitialLoading')
                self.assertGreaterEqual(guard, 0, f'{rel} 没有首屏守卫（骨架/错误分支）')
                self.assertIn('LoadError', code, f'{rel} 没有把加载失败显示出来')
                for lie in lies:
                    at = code.find(lie)
                    self.assertGreaterEqual(
                        at, 0, f'{rel} 里找不到 {lie}，断言前提不成立')
                    self.assertLess(
                        guard, at,
                        f'{rel} 的首屏守卫排在 {lie} 之后——数据没取到时用户会看到这句'
                        '「没有数据」，而事实是还没取到',
                    )

    def test_no_route_level_loading_tsx(self) -> None:
        """不要创建 `app/(main)/loading.tsx`。

        Next.js 官方 Platform Support 表里，`loading.js` 在 **Static export** 一栏
        是 **No**：它是 Suspense fallback，依赖服务端流式渲染，而本项目的生产产物
        正是静态导出（`npm run build:export` / Dockerfile），10 个页面又都是
        `'use client'` + `useEffect` 取数，构建期边界立即 resolve。

        也就是说这个文件在开发服务器上「看起来能用」，上线后是死的——最坏的一种
        失败：本地验证通过、线上没效果。首屏加载态请走 useAsyncAll 的
        isInitialLoading。
        """
        offender = _ROOT / 'web' / 'app' / '(main)' / 'loading.tsx'
        self.assertFalse(
            offender.exists(),
            '静态导出下 loading.tsx 不生效（官方 Platform Support: Static export → No），'
            '首屏加载态请用 useAsyncAll 的 isInitialLoading',
        )


class TasksPageHonestyTest(unittest.TestCase):
    """任务记录页「配件面板不该拖垮正片」的三条不变式。

    这一页的形状和前几页都不同，难点不在「有没有守卫」，而在**上游原始日志面板**：
    它有意容忍失败——上游只在失败与旅行/活跃时打日志、容器重建即丢，「取不到」是
    常态，用户不需要为此做任何事。所以它不能和两份正片数据共用一组判据：

      · 它失败了不该让「部分数据加载失败」挂出来；
      · 更要紧的是它**成功了**而两份正片全挂时——它会让 `hasData` 有值，整页错误态
        就不再出现，用户看到的仍是「暂无签到记录」。这正是本批要修的那句谎话，
        只是换了个触发路径。

    三条都能用源码形状钉住，每条对应一次真实浏览器验收里能观察到的现象。
    """

    def test_upstream_panel_is_a_separate_hook(self) -> None:
        """「上游原始日志」必须**单独一个 hook**，不能和两份正片混在一组。

        与 `stats` 页的上游统计同因（见 `test_stats_keeps_upstream_out_of_the_main_group`）：
        一份「取不到是常态」的配件数据，混进主组会顶掉「一份都没取到」的判据。

        真实浏览器验收里的表现：只把 `/api/upstream/logs` 打成 500，页面**不该**出现
        「部分数据加载失败」；只把两份正片打成 500，页面**必须**出现整页错误态
        （若配件混在主组里，这条会失败——它自己有值，于是「一份都没取到」不成立）。
        """
        code = _code(_TASKS)
        args = _hook_args(code, 0)
        self.assertGreaterEqual(len(args), 2, '找不到 tasks 页第一个 useAsyncAll 调用')
        self.assertIn('checkinLogs(', args[0], '正片那组里没有签到记录')
        self.assertIn('taskLogs(', args[0], '正片那组里没有自动任务记录')
        self.assertNotIn(
            'upstreamLogs(', args[0],
            '上游原始日志被放回了正片那一组：它「取不到是常态」，混在一起时它若成功、'
            '两份正片全挂，整页错误态就不会出现——用户看到的仍是「暂无签到记录」',
        )
        second = _hook_args(code, 1)
        self.assertTrue(
            second and 'upstreamLogs(' in second[0],
            'tasks 页没有第二个 useAsyncAll 把上游原始日志单独接起来',
        )

    def test_upstream_panel_keeps_refreshing_after_the_split(self) -> None:
        """拆成独立 hook 后，这块面板必须**自己有心跳**。

        原先它跟着主 load 一起每 30 秒重拉；拆开时若忘了给它一个心跳，这块面板会
        停在进页面那一刻的内容上，之后再也不会更新——而且**界面不会报任何错**，
        看起来只是「上游最近没打日志」。这种静默的功能退化最难发现。
        """
        code = _code(_TASKS)
        self.assertRegex(
            code, r'useHeartbeat\(\s*upstream\.reload\s*,',
            '上游原始日志面板没有自己的心跳：拆开之后它不再随主数据刷新，会停在'
            '进页面那一刻的内容上，而界面上看不出任何异常',
        )
        self.assertRegex(
            code, r'useHeartbeat\(\s*reload\s*,',
            '正片数据没有走 reload 心跳（应传 reload 而不是首屏那次取数）',
        )

    def test_failed_panel_does_not_render_the_empty_copy(self) -> None:
        """哪一块没取到，就在**那一块**挂常驻提示，不能落进「暂无记录」的文案。

        两者渲染出来是同一片空白，含义却正好相反：前者是「确实一条都没有」，后者是
        「不知道有没有」。任务记录页尤其要紧——它是用户核对积分收益的依据，把它说成
        「暂无」会让人以为后台采集器坏了，于是去排查一个根本不存在的问题。

        这里钉三件事：两块各自从 errors 里算出自己的失败标记；两处空态分支都被它挡
        住；两条提示文案都真的用上了（有文案没人用 = 失败时还是那句「暂无」）。

        真实浏览器验收（把 `/api/task-logs` 打成 500）里的表现是：出现「自动任务记录
        加载失败」，且**不**出现「暂无自动任务记录」。
        """
        code = _code(_TASKS)
        for key in ('checkin', 'tasks'):
            self.assertRegex(
                code, rf"{key}Failed\s*=\s*'{key}'\s*in\s*errors",
                f'任务记录页没有从 errors 里算出 {key}Failed——「没取到」与「确实为空」'
                '就分不开了',
            )
            self.assertIn(
                f'{key}Failed ?', code,
                f'任务记录页的 {key} 空态没有按 {key}Failed 分支——取数失败时会显示成'
                '「确实一条都没有」',
            )
        self.assertIn("t('tasks.checkinLoadFailed')", code,
                      '签到记录面板没有自己的失败文案，只能复用「暂无签到记录」')
        self.assertIn("t('tasks.taskLogLoadFailed')", code,
                      '自动任务面板没有自己的失败文案，只能复用「暂无自动任务记录」')

    def test_failed_panel_does_not_render_the_total(self) -> None:
        """取不到时，页脚的「共 N 条」也必须一起收起来。

        这是同一个谎话的**最后一处**，而且是最容易漏的一处：正文已经被行内提示挡住了，
        页脚却还挂着「共 0 条」——总数取不到时它就是 0，于是「加载失败」和「共 0 条」
        并排显示，互相打脸。用户读到的仍然是「一条都没有」。

        真实浏览器验收里靠这条抓：只把 `/api/checkin-logs` 打成 500 时，页面上**不得**
        出现「共 0 条」（另一块成功了，显示的是「共 2 条」，所以这个判据不含糊）。
        本批就是先在截图里看见这行字、才回头补的守卫——**断言全绿不等于界面上没有谎话**。
        """
        code = _code(_TASKS)
        for key, var in (('checkin', 'checkinLogs'), ('tasks', 'taskLogs')):
            self.assertRegex(
                code, rf'totalUnknown=\{{{key}Failed\s*&&\s*!{var}\.length\}}',
                f'任务记录页的 {key} 面板页脚没有在取数失败时收起来——会显示「共 0 条」，'
                '等于告诉用户「确实一条都没有」',
            )


if __name__ == '__main__':
    unittest.main()

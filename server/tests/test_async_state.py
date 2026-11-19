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
_SECURITY = _MAIN / 'security' / 'page.tsx'
_TASKS = _MAIN / 'tasks' / 'page.tsx'
_KEYS = _MAIN / 'keys' / 'page.tsx'
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
    'security/page.tsx': [
        "t('security.noRules')",          # 「暂无规则」——在这一页等于说没有任何网段被放行或拦截
        "t('security.noAccessLogs')",     # 「暂无访问记录」——等于说没人被拦过
        "t('security.noAudit')",          # 「暂无审计记录」
    ],
    'tasks/page.tsx': [
        "t('tasks.checkinEmpty')",        # 「暂无签到记录」——等于说「你没签过到」
        "t('tasks.noTaskRecords')",       # 「暂无自动任务记录」——等于说采集器没跑过
        "t('tasks.rawLogNote')",          # 上游原始日志的「这里没有记录不代表没执行」
    ],
    'keys/page.tsx': [
        "t('keys.emptyTitle')",           # 「暂无 API 密钥」——在讲凭据的页面上，
                                          # 这句读起来是「我的密钥被删了」，用户会顺手重建
    ],
    # 批次 4 起，设置页的外壳在 `layout.tsx`（7 个 Tab 变成 `/settings/<tab>`
    # 子路由，而子路由的 page 每次跳转都会重挂载——取数与表单状态必须放在
    # 不重挂载的 layout 上，见那个文件顶部的说明）。`page.tsx` 现在只是把
    # `/settings` 重定向到第一个 Tab，所以这条不变式跟着换到了新路径。
    'settings/layout.tsx': [
        "t('settings.usersEmpty')",       # 「暂无管理用户」——等于说系统里一个账号都没有
    ],
    'accounts/page.tsx': [
        "t('accounts.emptyTitle')",       # 「暂无账号」——等于说号池是空的
        "t('accounts.emptyRealmTitle'",   # 「<版本> 没有账号」——同样是「没有」，只是限定到版本
        "t('accounts.noMatchTitle')",     # 「没有匹配的账号」——筛选结果是空的，前提是清单已经取到
    ],
    'models/page.tsx': [
        "t('models.noModels')",           # 「暂无模型」——等于说腾讯那边没有可用模型
        "t('models.noMatch')",            # 「没有匹配的模型」——筛选结果是空的，前提是清单已经取到
    ],
    'red-packets/page.tsx': [
        "t('redPacket.empty')",           # 「暂无红包」——等于说红包发完了/被清了
    ],
}

# 「首屏守卫」的判据写法**不唯一**，取决于主数据是不是页面的全部数据：
#   · dashboard / logs / stats / playground / security / tasks —— 页面上要的那几份
#     就是全部，一份都没到就不该渲染，于是直接用 useAsyncAll 的
#     `isInitialFailed || isInitialLoading`；
#   · settings —— 主数据只有 `cfg` 一份（模型映射、用户各自渲染在自己的 Tab 里），
#     它没到就不能渲染那张配置表单，判据是 `if (!cfg)`。
# 两者表达的是同一件事（主数据没就绪就不往下渲染），所以这里接受任一写法。
_GUARD_MARKERS = (
    'isInitialFailed || isInitialLoading',
    'if (!cfg)',
)

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
        """日志页必须把「页码 / 天数 / 各筛选项」放进**第三**个参数（静默重取）。

        第二组依赖（deps）的语义是「换了一个数据上下文」——变了就清空重取、显示骨架。
        页码若被放进去，**每翻一页都会闪一次骨架**；而翻页是高频操作，看起来像
        页面在抽搐。所以 realm 走第二组，page / days / 各筛选项走第三组。

        反过来，筛选项**漏**进第三组就是 P1-4 的原始形态：五个控件里只有「天数」在
        依赖数组里，其余四个只在点「查询」时生效——同一排控件有两种脾气，能选、能改、
        列表不动、也不报错。批次 3 把四个筛选项接进了依赖，这条断言同时钉住两个方向。

        ⚠️ 局限：这里只检查**已知的六个**名字在不在第三组里。新加的第七个筛选控件不在
        名单上，所以这条断言抓不到它（要抓「新增了控件却忘了进依赖」得解析 JSX 与依赖
        数组的对应关系，不值当）。它抓的是**现有六个被摘掉**。
        """
        code = _code(_LOGS)
        args = _hook_args(code, 0)
        self.assertGreaterEqual(len(args), 3, '找不到 logs 页 useAsyncAll 的三个参数')
        context, query = args[1], args[2]
        self.assertIn('realm', context,
                      'logs 页没把 realm 放进第二组依赖（换了版本要清空重取）')
        for dep in ('page', 'days'):
            self.assertNotIn(
                dep, context,
                f'{dep} 被放进了第二组依赖：每次翻页 / 改时段都会清空重取、闪一次'
                '骨架，看起来像页面在抽搐',
            )
        missing = [d for d in ('page', 'days', 'keyId', 'status', 'modelQ', 'ipQ')
                   if d not in query]
        self.assertEqual(
            missing, [],
            f'这些筛选 / 翻页条件不在第三组依赖里：{missing}。不在依赖里的筛选项'
            '**改了不会重取**，控件成了装饰（P1-4 的原始形态）；文本类'
            '（modelQ / ipQ）要用**防抖落定值**，不是输入框的即时值。',
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
                # 取各候选写法里**最早**出现的位置：文件里可能同时出现两种（例如
                # settings 的注释里提到过另一种），取最早的那个才是真正的守卫。
                hits = [i for i in (code.find(m) for m in _GUARD_MARKERS) if i >= 0]
                guard = min(hits) if hits else -1
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


class SecurityPageHonestyTest(unittest.TestCase):
    """安全管控页「宁可说不知道，也不说没有」的三条不变式。

    这一页的特殊之处：它讲的是**访问控制**，而它原来犯的错是把失败静默丢掉，
    于是界面上那些断言性的话（「IP 访问控制：关闭」「暂无规则」「暂无访问记录」）
    全都在**没有任何依据**的情况下说了出来。管理员据此会得出「拦截没生效」
    「没人被拦过」这类结论——比显示空白危险得多，因为它看起来是有信息的。

    三条都不容易在行为测试里表达（要真浏览器），但都能用源码形状钉住；
    每条都对应一次真实浏览器验收里能观察到的现象，写在各自的 docstring 里。
    """

    def test_not_loaded_is_not_the_same_as_empty(self) -> None:
        """空表与「没取到」必须**分别**渲染，不能共用一句话。

        两者渲染出来是同一片空白，含义却正好相反：前者是「确实一条都没有」，
        后者是「不知道有没有」。共用一句话时，取数失败会被显示成前者——
        而安全页的「暂无规则」等于告诉管理员「没有任何网段被放行或拦截」。

        真实浏览器验收（把 `/api/security/logs` 打成 500）里，这一条的表现是：
        页面必须出现「未取到」，且**不**出现「暂无访问记录」。
        """
        code = _code(_SECURITY)
        for key in ('rules', 'logs', 'audit'):
            self.assertRegex(
                code, rf"{key}Failed\s*=\s*'{key}'\s*in\s*errors",
                f'安全页没有从 errors 里算出 {key}Failed——「没取到」与「确实为空」'
                '就分不开了',
            )
            self.assertIn(
                f'{key}Failed ?', code,
                f'安全页的 {key} 空状态没有按 {key}Failed 分支——取数失败时会显示成'
                '「确实一条都没有」',
            )
        self.assertIn("t('security.notLoaded')", code,
                      '安全页没有「未取到」这句话，失败时只能复用空表文案')

    def test_missing_config_does_not_render_a_switch(self) -> None:
        """配置没取到时，**不能**渲染出一个「关闭」的开关。

        这条是本批最要紧的一条：开关正是管理员判断拦截是否生效的依据。用假默认值
        兜底（原来的写法初值就是 `{enabled: false}`）等于把「不知道」说成「关着」——
        管理员会以为拦截被关了，进而去排查一个根本不存在的问题。

        真实浏览器验收（把 `/api/security/config` 打成 500）里的表现是：
        开关**一个都不渲染**，策略卡如实写「未取到」。

        这里钉两件事：不许用对象字面量兜底；必须有 `shownConfig === null` 的分支。
        """
        code = _code(_SECURITY)
        self.assertRegex(code, r'values\.config\s*\?\?\s*null',
                         '安全页的 config 不再以 null 表示「没取到」')
        self.assertNotRegex(
            code, r'values\.config\s*\?\?\s*\{',
            '安全页用假默认值兜底了 config——取数失败时会渲染出一个「关闭」的开关，'
            '等于把「不知道」说成「关着」。这一页的开关是判断拦截是否生效的依据。',
        )
        self.assertIn('shownConfig === null', code,
                      '策略卡没有区分「配置没取到」——要么渲染假开关，要么整块消失')

    def test_reload_resolves_after_the_request_lands(self) -> None:
        """`reload()` 必须返回**这一轮请求自己的 promise**，不能另包一个。

        安全页保存配置是乐观更新：先切到目标态让开关立刻响应，再 `await reload()`
        把服务端确认过的新值拉回来，最后才撤掉乐观值。若 reload 返回的不是那个
        promise（比如另写 `Promise.resolve(...)`、或者干脆不返回），撤乐观值会发生在
        `values.config` 还是**旧值**的时候，开关当场跳回原态——用户看到的是
        「点了没反应」，而服务端其实已经改成功了。

        真实浏览器验收里这条的表现是：`data-state` 的变化序列必须恰好是
        `checked → unchecked`，中途不许出现 `checked`。
        （只看最终状态是不够的——回弹之后那一轮刷新落地会把开关再变回 unchecked，
        只看终态会误判为通过。）
        """
        hook = _code(_HOOK)
        self.assertRegex(hook, r'reload:\s*\(\)\s*=>\s*Promise<boolean>',
                         'hook 的 reload 不再声明返回 Promise<boolean>')
        self.assertRegex(
            hook, r"reload\s*=\s*useCallback\(\s*\(\)\s*=>\s*run\(",
            'reload 没有直接把 run(...) 的 promise 返回出去——它必须是「这一轮请求'
            '落地后才 resolve」的那一个，否则乐观更新会在新值到达前就撤掉，'
            '开关跳回旧态',
        )
        self.assertIn('Object.keys(fresh.errors).length === 0', hook,
                      'reload 没有按「是否还有字段没取到」给出成败')

        code = _code(_SECURITY)
        save = code.find('async function saveConfig')
        self.assertGreaterEqual(save, 0, '安全页找不到 saveConfig')
        body = code[save:save + 900]
        self.assertIn('await reload()', body,
                      'saveConfig 没有等这一轮刷新落地')
        self.assertLess(
            body.find('await reload()'), body.find('setOptimistic(null)'),
            'saveConfig 在等刷新落地**之前**就撤掉了乐观值——那一刻 values.config '
            '还是旧值，开关会跳回去，用户以为没点到',
        )
        # 撤乐观值必须是**刷新成功**才做（维护者复核补）：写操作已经落库，这一次
        # 刷新失败就把显示退回旧值，开关照样跳回去、用户照样以为没保存上——只不
        # 过触发条件从「撤早了」换成了「刷新失败」。两种形态都要防。
        self.assertRegex(
            body, r'if\s*\(\s*await\s+reload\(\)\s*\)',
            'saveConfig 无条件撤掉乐观值：刷新失败时开关会跳回旧值，'
            '而写入其实已经成功（顶部常驻提示已说明列表没刷上）',
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


class KeysPageHonestyTest(unittest.TestCase):
    """密钥页「宁可说不知道，也不说没有」的不变式。

    这一页讲的是**凭据**，所以「暂无 API 密钥」比别的页面更危险。它不像「暂无日志」
    那样只是少了个列表——用户看到这几个字的第一反应是「我的密钥被删了」，第二反应
    是顺着旁边的「新建密钥」按钮重建一个。于是**建出重复密钥**，而重复密钥会让
    「按密钥限额」「按密钥统计用量」全都对不上，用户还很难联想到是这一步造成的。

    这正是 2026-09-16 那次修复要防的事（`keys.createdButRefreshFailed`：创建成功、
    列表却没刷新出来时必须说清「已经建好了，别重复建」）——只不过那次堵的是
    **写之后**那个门，这里堵的是**打开页面时**那个门。同一句谎话有两个入口。
    """

    def test_not_loaded_is_not_the_same_as_empty(self) -> None:
        """列表没取到时不得说「暂无 API 密钥」。

        两条防线，对应两种进入方式：

        1. **首屏那一次就失败**：`isInitialFailed` → 整页错误态 + 重试，内容区
           根本不渲染。这一条的**顺序**由
           `test_pages_guard_before_rendering_empty_states` 钉住。
        2. **之前取到过、这次刷新失败**：`isInitialFailed` 为假（`hasData` 还是真的），
           但 `errors` 里有 `keys`——手上这份列表可能已经旧了，这时说「你没有密钥」
           同样没有依据。`keysFailed` 就是为这一种留的。

        为什么是 `keysFailed` 而不是 `partialFailed`：`partialFailed` 的语义是
        「任一字段失败」。上游列表挂掉时密钥列表本身是好的，那种情况**应该**照常
        显示「暂无密钥」；用 `partialFailed` 会把一个正常的空列表也藏起来，
        用户反而以为页面坏了。
        """
        code = _code(_KEYS)
        self.assertRegex(
            code, r"keysFailed\s*=\s*'keys'\s*in\s*errors",
            '密钥页没有从 errors 里算出 keysFailed——「没取到」与「确实为空」就分不开了',
        )
        at = code.find("t('keys.emptyTitle')")
        self.assertGreaterEqual(at, 0, '找不到「暂无密钥」这句，断言前提不成立')
        # 只看这句话**近旁**的条件：全文搜 `!keysFailed` 的话，把它挪到别处
        # （比如某个跟列表无关的分支上）也会通过，等于没断言。
        near = code[max(0, at - 400):at]
        self.assertIn(
            '!keysFailed', near,
            '密钥页的空状态没有按 keysFailed 收窄——刷新失败时会显示成「暂无 API 密钥」，'
            '用户会以为密钥被删了，并顺手重建出重复密钥',
        )

    def test_failed_list_does_not_render_a_count(self) -> None:
        """列表没取到时，两个 tab 上的数字也要一起收起来。

        这是同一句谎话的另一半，也是最容易漏的一半：空状态已经被挡掉了，tab 上却
        还挂着「普通密钥 · 0」——列表取不到时 `keys` 就是空的，那个 0 没有依据，
        读起来仍然是「一个密钥都没有」。任务记录页的「共 0 条」正是同一处
        （PR #97 才补上），只是换了个位置。

        判据同样是 `keysFailed`（**这一份**失败没）而不是 `partialFailed`
        （有任一份失败没）：上游列表挂掉时密钥列表是好的、确实为空，那两个 0 是
        如实的，必须照常显示——真实浏览器验收里正好用这一对反过来验证判据。

        ⚠️ 窗口只能开得很窄（60 字符）。第一版写成 160，结果把守卫从 tabNormal 上
        摘掉、测试**照样绿**：`!keysFailed` 在 160 字外属于**另一个** tab，正则够得着
        它。这正是「守卫在别处也能通过」那类空转，靠变异测试才露出来。
        """
        code = _code(_KEYS)
        for key in ('tabNormal', 'tabPacket'):
            self.assertRegex(
                code, rf"t\('keys\.{key}'\)[\s\S]{{0,60}}?!keysFailed",
                f'密钥页 {key} 的数字没有按 keysFailed 收起来——列表没取到时它会显示'
                '「· 0」，等于说「一个密钥都没有」',
            )


if __name__ == '__main__':
    unittest.main()

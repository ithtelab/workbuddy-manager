"""设置页 10 个 Tab 的可寻址子路由（批次 4 的 P1-2）+ 结构不变式。

为什么值得有：这一批把「一页 2060 行的单页」改成 `/settings/<tab>`，做错了**界面
不报错**，只是行为悄悄变差——

  · 清单里加了 Tab 却没有对应的路由目录 → 点进去 404（本地只看首页永远发现不了）；
  · 有了目录却不在清单里 → 导航上看不见，那条路由成了暗门；
  · 外壳从 `layout.tsx` 挪回 `page.tsx` → 每切一次 Tab 都重挂载，重新拉一次配置、
    闪一次骨架，还会丢掉没保存的编辑（**这一条最隐蔽**：功能全都在，只是变卡）；
  · 导航从「真实链接」退回 `TabsTrigger` → 地址栏不再变，分享出去的链接永远落到
    第一个 Tab（用户会以为「这个面板没有深链」）；
  · 解析多认一段（`/settings/models/extra` 也当成 models）→ 一条不存在的 URL 安静
    地渲染成某个 Tab，用户以为分享对了。

第 5 条由 `web/lib/settings-tabs.test.mjs` 的行为测试盖住（跑的是真实实现）；其余
几条只能在**源码形状**上断言——行为测试证明不了「渲染层真的用了那份清单」，而它们
恰恰都是「算了却没用在分支上」型的错。

为什么用 Python 包一层：本仓库的测试套件是 Python 的（`pytest server/tests/`），
而这段逻辑在前端。没有 Node（或版本太旧）时**跳过**而不是失败。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'settings-tabs.test.mjs'
_PURE = _ROOT / 'web' / 'lib' / 'settings-tabs.ts'
_NAV = _ROOT / 'web' / 'components' / 'common' / 'layout' / 'SectionTabs.tsx'
_SETTINGS = _ROOT / 'web' / 'app' / '(main)' / 'settings'
_LAYOUT = _SETTINGS / 'layout.tsx'
_INDEX = _SETTINGS / 'page.tsx'
_VERIFY = _ROOT / 'dev' / 'verify_state_honesty.mjs'
_LOCALES = _ROOT / 'web' / 'lib' / 'i18n' / 'locales'
_NODE = shutil.which('node')

# 路线图里写死的数字：10 个 Tab。多一个少一个都要有人主动改这里（以及 CHANGELOG）。
_TAB_COUNT = 10

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
    src = re.sub(r'\{/\*.*?\*/}', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


def _tab_names() -> list[str]:
    """从 `settings-tabs.ts` 里读出清单，作为其余断言的**单一事实来源**。

    刻意不在这里再写一份副本：副本与实现漂移时，下面的断言会一起去断言那个错的
    副本，测试全绿而页面上 404——正是本文件要防的那类空转。
    """
    code = _code(_PURE)
    start = code.index('export const SETTINGS_TABS = [')
    end = code.index('] as const;', start)
    return re.findall(r"'([^']+)'", code[start:end])


class SettingsTabsBehaviourTest(unittest.TestCase):
    """跑 Node 侧的行为测试：路径解析与清单本身。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_settings_tabs_module(self) -> None:
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
        # （与 `test_dock_groups.py` / `test_account_list.py` 同一处理。）
        if proc.returncode not in (0, 1):
            proc = run_once()
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'设置页 Tab 逻辑不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class SettingsTabsInvariantTest(unittest.TestCase):
    """行为测试盖不到的前提与结构不变式。"""

    def setUp(self) -> None:
        self.tabs = _tab_names()
        self.assertEqual(
            len(self.tabs), _TAB_COUNT,
            f'设置页的 Tab 清单变成了 {len(self.tabs)} 项（路线图里是 {_TAB_COUNT} 项）：'
            f'{self.tabs}。数量变了就要同步改路由目录、CHANGELOG 与路线图——'
            '这条断言就是为了逼那一步发生。',
        )

    def test_module_stays_runnable_without_node_modules(self) -> None:
        """`settings-tabs.ts` 不得出现**运行时** import。

        这是上面那条 Node 测试能跑起来的前提：`.mjs` 直接 `import './settings-tabs.ts'`，
        而 Node 的 ESM 解析**要求带扩展名**，仓库的 app 代码却一律不写扩展名。
        一旦本模块 import 了别的模块的**值**（最容易顺手加的是 `@/lib/base-path`），
        解析就会失败——症状是「Cannot find module」，看起来像路径写错。
        所以路径解析走「匹配后缀」，不依赖 basePath。
        """
        offenders = [
            line.strip() for line in _code(_PURE).splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(
            offenders, [],
            'settings-tabs.ts 出现了运行时 import：\n  ' + '\n  '.join(offenders)
            + '\n本模块被 web/lib/settings-tabs.test.mjs 直接 import，引入运行时 import '
              '会让那条测试以「找不到模块」失败。',
        )

    def test_every_tab_has_a_route_directory(self) -> None:
        """清单里的**每一项**都要有真实存在的路由目录。

        少了就是「导航上有、点进去 404」——而且本地只看首页永远发现不了。反过来，
        多出来的目录（不在清单里）是暗门，导航上看不见。
        """
        self.assertTrue(_SETTINGS.is_dir(), f'找不到设置页目录: {_SETTINGS}')
        missing = [t for t in self.tabs if not (_SETTINGS / t / 'page.tsx').is_file()]
        self.assertEqual(
            missing, [],
            f'这些 Tab 在导航里，却没有 /settings/<tab> 的路由：{missing}。'
            '点进去会是 404。',
        )
        # 反向：目录里不该出现清单外的 Tab
        on_disk = sorted(
            p.name for p in _SETTINGS.iterdir()
            if p.is_dir() and (p / 'page.tsx').is_file()
        )
        self.assertEqual(
            on_disk, sorted(self.tabs),
            '设置页下的路由目录与 Tab 清单不一致——多出来的那条是暗门（导航上看不见），'
            '或者少了一条（导航上点了 404）。',
        )

    def test_upstream_route_is_not_gitignored(self) -> None:
        """设置页的路由目录不能被 `.gitignore` 吞掉。

        `.gitignore` 里那条 `upstream/` 原本是用来挡**仓库根目录**那份上游源码的
        （里面有 auths/ 与 config.json，绝不能提交）。但它没锚定，于是连带忽略了
        `web/app/(main)/settings/upstream/` —— 那个 Tab 的 page.tsx **提交不进去**，
        而本地测试照样绿（文件在磁盘上，只是没进 git）。#108 就是这么漏的：PR 自带
        的这条测试在分支上红了三条，`/settings/upstream` 在部署里是 404。
        """
        gi = (_ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
        loose = [line for line in gi if line.strip() == 'upstream/']
        self.assertEqual(
            loose, [],
            '未锚定的 `upstream/` 会吞掉任何同名子目录（web/app/(main)/settings/upstream/ '
            '就被吞过）。请写成 `/upstream/`，只挡仓库根目录那一份。',
        )
        # 根目录那份仍要被挡住（这是这条规则存在的理由，别在修的时候顺手删掉）
        self.assertTrue(
            any(line.strip() in ('/upstream/', 'upstream/') for line in gi),
            '.gitignore 里没有挡上游源码的规则了 —— auths/ 与 config.json 会被误提交',
        )

    def test_route_pages_render_nothing(self) -> None:
        """10 个子路由的 page **不渲染内容**，内容由外壳渲染。

        这不是疏忽，而是本批次的核心取舍：切 Tab 是一次路由跳转，App Router 会重挂载
        page，而设置页的表单状态（配置副本、乐观值、改动基线）必须活过这次跳转。
        内容一旦交给 page 渲染，就必须把几十项状态提到 context 再发下去——那会把本
        仓库最复杂的一页整体拆开。所以这里钉住「page 返回 null」，并另有一条断言钉住
        「取数发生在外壳里」。
        """
        for tab in self.tabs:
            code = _code(_SETTINGS / tab / 'page.tsx')
            with self.subTest(tab=tab):
                self.assertIn(
                    'return null', code,
                    f'/settings/{tab} 的 page 开始渲染内容了。要么它真的该渲染内容'
                    '（那就要把外壳的状态提到 context 里，并重跑切 Tab 不重取数的'
                    '验收），要么这是误改。',
                )

    def test_data_fetching_lives_in_the_layout(self) -> None:
        """取数必须在 `layout.tsx` 里，**不能**在任何一个子路由的 page 里。

        这是「切 Tab 不重新拉配置」的全部依据：page 会随路由重挂载，layout 不会。
        一旦取数落回 page（哪怕只是顺手加一个 `useAsyncAll`），表现是「每切一次 Tab
        都闪一次骨架、表单里没保存的编辑被冲掉」——功能全都在，只是变卡，没人会为此
        提 issue。
        """
        self.assertTrue(_LAYOUT.is_file(), f'找不到设置页外壳: {_LAYOUT}')
        self.assertIn(
            'useAsyncAll(', _code(_LAYOUT),
            '设置页的取数不在 layout.tsx 里了——它随子路由重挂载，切一次 Tab 就会'
            '重新拉一次配置并闪一次骨架。',
        )
        for tab in self.tabs:
            code = _code(_SETTINGS / tab / 'page.tsx')
            with self.subTest(tab=tab):
                self.assertNotIn(
                    'useAsyncAll(', code,
                    f'/settings/{tab} 的 page 里出现了取数：page 会随路由重挂载，'
                    '这一份数据每切一次 Tab 都会重取一次。',
                )

    def test_redirect_does_not_depend_on_data_state(self) -> None:
        """`/settings` 的重定向**不能**只在「配置取到了」时才发生。

        `/settings` 的规范化靠子路由的 page（`router.replace`），而 page 只有在
        `{children}` 被渲染时才会挂载。外壳有两条 return 分支（首屏守卫的早返回、
        正常分支），少渲染一条，就会出现「配置取不到时地址停在 `/settings`」——
        于是「我现在在哪个 Tab」变成**取决于数据取没取到**，用户刷新一次会再走一遍
        重定向。

        实测过：浏览器验收里那一步就是因此变成偶发红的（先以 MODE=8 进 `/settings`
        停在原地，再 reload 时又要走一次重定向，断言在重定向落地前求值）。
        """
        code = _code(_LAYOUT)
        # 只数**独占一行**的 `{children}`：函数签名里那个 `{children}` 是参数，
        # 不是「渲染出来」。
        found = sum(1 for line in code.splitlines() if line.strip() == '{children}')
        self.assertEqual(
            found, 2,
            f'外壳只在 {found} 条 return 分支里渲染了 children：`/settings` 的重定向'
            '挂在子路由的 page 上，漏掉哪条分支，那条路径下地址就不会被规范化。',
        )

    def test_shell_derives_the_tab_from_the_path(self) -> None:
        """外壳必须**从路径**得出当前 Tab，并回落到默认项。"""
        code = _code(_LAYOUT)
        self.assertRegex(
            code, r'settingsTabFromPath\(usePathname\(\)\)',
            '外壳没有从路径解析当前 Tab——那样地址栏与内容会各说各话。',
        )
        self.assertIn(
            '?? DEFAULT_SETTINGS_TAB', code,
            '解析不出 Tab 时没有回落：停在 /settings 那一层（重定向落地前的那一帧）'
            '会渲染出一个空白的 Tab 区域。',
        )

    def test_tabs_are_controlled_by_the_route(self) -> None:
        """`Tabs` 必须由路径**受控**，且导航不再是 `TabsList` / `TabsTrigger`。

        `defaultValue` 是这一批要修掉的那个形态：它把当前 Tab 藏在组件内部状态里，
        地址栏不动，链接分享出去只会落到第一个 Tab。
        """
        code = _code(_LAYOUT)
        self.assertIn(
            '<Tabs value={tab}>', code,
            'Tabs 不是受控的（或 value 不是从路径来的）：地址栏与内容会各说各话。',
        )
        self.assertNotIn(
            'defaultValue="upstream"', code,
            'Tabs 又用回了 defaultValue：当前 Tab 又变回组件内部状态，地址栏不再跟着变。',
        )
        for gone in ('<TabsList', '<TabsTrigger'):
            self.assertNotIn(
                gone, code,
                f'设置页又用回了 {gone}：它是「同一页内的按钮」，地址栏不会变，'
                '分享出去的链接永远落到第一个 Tab。导航请用 SectionTabs。',
            )

    def test_section_nav_is_links(self) -> None:
        """二级导航的每一项必须是**真实链接**，并带上可访问性与定位标记。"""
        self.assertTrue(_NAV.is_file(), f'找不到二级导航组件: {_NAV}')
        nav = _code(_NAV)
        self.assertIn(
            "from 'next/link'", nav,
            '二级导航没有用 <Link>：改用 onClick + router.push 会在「点下去」与'
            '「路由真的变了」之间留一帧，两个 Tab 同时是 active 的。',
        )
        self.assertIn(
            'aria-current', nav,
            '当前项没有 aria-current：读屏软件无法播报「这是当前页」。',
        )
        self.assertIn(
            'data-slot="section-tab"', nav,
            '导航项没有稳定标记，验收脚本定位不到它（它会退回去数 [role=tab]，'
            '而那已经不存在了——断言会变成恒真）。',
        )
        self.assertIn(
            'data-slot="section-tabs"', nav,
            '导航容器没有稳定标记',
        )
        self.assertNotIn(
            'TabsTrigger', nav,
            '二级导航又用回了 TabsTrigger——它渲染的是 button，地址栏不会变。',
        )

    def test_nav_is_rendered_with_the_active_item(self) -> None:
        """外壳要真的把导航渲染出来，并且把当前项告诉它。

        算了却没用（比如只传 items、不传 active）不会有任何报错：所有 Tab 都是
        未选中态，用户看不出自己在哪一页。
        """
        code = _code(_LAYOUT)
        self.assertIn('<SectionTabs items={tabItems} active=', code,
                      '外壳没有把导航渲染出来，或没有把当前项传下去（那样所有 Tab 都是未选中态）')
        self.assertRegex(
            code, r'active=\{settingsTabHref\(tab\)\}',
            '当前项不是由 tab 算出来的：高亮会停在某一项不动。',
        )
        # 标签键搬到了 `settings-tabs.ts`（命令面板也要用同一份）。外壳必须**从那里读**，
        # 而不是自己再写一份——两份漂移时，命令面板里会有一个 Tab 显示裸键名。
        self.assertIn(
            't(SETTINGS_TAB_LABEL_KEYS[name])', code,
            '外壳没有从 SETTINGS_TAB_LABEL_KEYS 取标签键 —— 它又自己抄了一份，'
            '与命令面板（⌘K）用的那份迟早会漂移。',
        )

    def test_index_redirects_to_the_default_tab(self) -> None:
        """`/settings` 要把地址换成默认 Tab 的规范路径。

        用 `replace` 而不是 `push`：两者是同一屏内容，留两条历史记录会让「后退」
        看起来点了没反应。
        """
        code = _code(_INDEX)
        self.assertRegex(
            code, r'router\.replace\(settingsTabHref\(DEFAULT_SETTINGS_TAB\)\)',
            '/settings 没有把地址换成默认 Tab 的规范路径：同一个页面会有两个地址，'
            '而且「后退」会退回一个看起来一样的页面。',
        )
        self.assertNotIn(
            'router.push(', code,
            '/settings 用的是 push：/settings 与 /settings/upstream 是同一屏内容，'
            '留两条历史记录会让「后退」看起来点了没反应。',
        )

    def test_acceptance_script_targets_the_new_markup(self) -> None:
        """浏览器验收脚本必须改用新的选择器。

        它还按 `[role=tab]` 数「上游配置」的话：那个元素已经不存在，计数恒为 0，
        于是「配置取不到时一个 Tab 都不渲染」这条断言**恒真**（空转），而反向的
        「配置取到了 → 表单照常渲染」会红。两条一起看很容易被误读成「页面坏了」。
        """
        self.assertTrue(_VERIFY.is_file(), f'找不到验收脚本: {_VERIFY}')
        code = _VERIFY.read_text(encoding='utf-8')
        self.assertIn(
            '[data-slot=section-tab]', code,
            '验收脚本没有改用新的导航标记（data-slot=section-tab）：它还在数 '
            '[role=tab]，而那个元素已经不存在——断言会恒真。',
        )
        self.assertNotIn(
            "[role=tab]'", code,
            '验收脚本里还留着按 [role=tab] 定位的写法：设置页的导航已经不是那个元素了。',
        )

    def test_locale_has_the_nav_label(self) -> None:
        """`nav.sectionTabs`（二级导航的 aria-label）必须在**每个**语言包里都存在。

        缺了不会报错：读屏软件读出一个裸键名。而 `test_web_i18n` 只查「各语言键集
        是否一致」，**不查引用是否存在**——只有这条能发现「代码引用了没人翻译的键」。
        """
        self.assertTrue(_LOCALES.is_dir(), f'找不到语言包目录: {_LOCALES}')
        missing: list[str] = []
        for path in sorted(_LOCALES.glob('*.json')):
            data = json.loads(path.read_text(encoding='utf-8'))
            node = data.get('nav')
            if not isinstance(node, dict) or not isinstance(node.get('sectionTabs'), str) \
                    or not node.get('sectionTabs').strip():
                missing.append(path.name)
        self.assertEqual(
            missing, [],
            f'以下语言包缺少 nav.sectionTabs，读屏软件会读出裸键名：{missing}',
        )

    def test_tab_labels_exist_in_all_locales(self) -> None:
        """二级导航上那 7 个**标签**用的键，必须在每个语言包里都有译文。

        同上的道理：键名写错一个字（`settings.tabModel` 少个 s）不会有任何报错，
        界面上那一项直接显示裸键名 `settings.tabModel`，而所有「键集一致」类的
        断言照样全绿——因为 5 个语言包都没这个键，它们彼此是一致的。

        键名不写死在测试里，而是从 `SETTINGS_TAB_LABEL_KEYS`（`settings-tabs.ts`）
        里读出来：写死的话，改实现时测试会跟着一起去断言那个错的副本。

        为什么读清单模块而不是外壳：标签键与清单本身是同一份数据的两面，都在
        `settings-tabs.ts` 里（外壳与命令面板都从那里读）。留在外壳里的话，这里
        读到的是「外壳那一份」，而命令面板用的是另一份——测试全绿，面板上却有一个
        Tab 显示裸键名。
        """
        code = _code(_PURE)
        start = code.index('export const SETTINGS_TAB_LABEL_KEYS')
        end = code.index('};', start)
        keys = re.findall(r":\s*'([^']+)'", code[start:end])
        self.assertEqual(
            len(keys), _TAB_COUNT,
            f'SETTINGS_TAB_LABEL_KEYS 里读到 {len(keys)} 个标签键（期望 {_TAB_COUNT}）：{keys}',
        )
        self.assertEqual(len(set(keys)), len(keys),
                         f'SETTINGS_TAB_LABEL_KEYS 里有重复的标签键：{keys}')
        self.assertTrue(_LOCALES.is_dir(), f'找不到语言包目录: {_LOCALES}')
        missing: list[str] = []
        for path in sorted(_LOCALES.glob('*.json')):
            data = json.loads(path.read_text(encoding='utf-8'))
            for dotted in keys:
                head, _, tail = dotted.partition('.')
                node = data.get(head)
                if not isinstance(node, dict) or not isinstance(node.get(tail), str) \
                        or not node.get(tail).strip():
                    missing.append(f'{path.name}: {dotted}')
        self.assertEqual(
            missing, [],
            '以下语言包缺少二级导航的标签文案，界面上会直接显示裸键名：\n  '
            + '\n  '.join(missing),
        )


if __name__ == '__main__':
    unittest.main()

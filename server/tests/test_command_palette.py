"""命令面板（⌘K，批次 5 ③）—— 行为测试借 Node 执行，结构不变式在源码上断言。

为什么值得有：这一批给「11 个业务页 + 设置页 10 个 Tab」加了一条键盘入口。做错了
**界面不报错**，只是那一项静默地到不了 / 到错地方：

  · 面板里漏了一项 → 那页从此只能靠底栏点进去，而 ⌘K 看起来一切正常；
  · 面板里多了一项 → 点下去 404，或者跳到一个不该有入口的地方；
  · 标签键抄错一个字母 → 那一行显示裸键名（`settings.tabModel`）；
  · 清单与底栏 / 二级导航 / 设置页 Tab **三份来源漂移** → 加了页面却忘了加进面板；
  · 把「退出登录」「重启容器」这类动作也放进来 → 敲两个字母 + 回车就执行了不可逆操作
    （这是本文件最想拦住的一条）；
  · 跳转时又套一层 `withBasePath` → 子路径部署下变成 `/wb/wb/tasks`；
  · 忘了处理输入法合成态 → 用拼音打 `zhanghao` 按回车会直接跳走，跳到哪条取决于
    当时高亮的是谁。

清单一致性由 `web/lib/command-palette.test.mjs` 与**本文件**两头夹：前者在 Node 里
比「href + 标签键」，后者再从底栏 / `section-nav` / `settings-tabs` **各自解析**出
一遍来对。两头都从真实来源取，不在这里另抄一份副本——复述副本的测试只会证明
「副本和自己一致」。

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
_WEB = _ROOT / 'web'
_MAIN = _WEB / 'app' / '(main)'
_SCRIPT = _WEB / 'lib' / 'command-palette.test.mjs'
_PURE = _WEB / 'lib' / 'command-palette.ts'
_COMPONENT = _WEB / 'components' / 'common' / 'layout' / 'CommandPalette.tsx'
_LAYOUT = _MAIN / 'layout.tsx'
_BAR = _WEB / 'components' / 'common' / 'layout' / 'ManagementBar.tsx'
_SECTION_NAV = _WEB / 'lib' / 'section-nav.ts'
_SETTINGS_TABS = _WEB / 'lib' / 'settings-tabs.ts'
_VERIFY = _ROOT / 'dev' / 'verify_state_honesty.mjs'
_LOCALES = _WEB / 'lib' / 'i18n' / 'locales'

_NODE = shutil.which('node')

# 只跑 .mjs，不碰 .ts —— Node 的 type stripping 从 22.6 起才有
_MIN_MAJOR = 22

# 面板里的条目数：底栏 8 个目的地 + 被吸收的 3 页 + 设置页 11 个 Tab（含代理、
# 监控与 Qoder——Qoder 融合批次从 10 → 11）。写死是**有意**的：数量变了说明
# 有人加了页面却没想清楚它该出现在哪，那一步值得被问一句。
# 三处来源的并集也在同一条断言里交叉核对。
_EXPECTED_TOTAL = 22

# 必须渲染出来的定位标记。验收脚本按它们断言；少一个就有一条断言恒真。
_REQUIRED_SLOTS = (
    'command-palette-trigger',
    'command-palette-input',
    'command-palette-item',
    'command-palette-empty',
    'command-palette-count',
)

_ENTRY_RE = re.compile(
    r"\{href:\s*'([^']+)',\s*labelKey:\s*'([^']+)',\s*groupKey:\s*'([^']+)',\s*"
    r"keywords:\s*'([^']*)'\}",
    re.S,
)
_SECTION_ITEM_RE = re.compile(
    r"\{key:\s*'([^']+)',\s*href:\s*'([^']+)',\s*labelKey:\s*'([^']+)',"
    r"\s*iconKey:\s*'([^']+)'\}"
)
# `title: t('nav.dashboard'),` … 同一项里的 `href: '/dashboard',`
_DOCK_ENTRY_RE = re.compile(
    r"\{\s*title:\s*t\('([^']+)'\),.*?href:\s*'([^']+)',", re.S
)
_COPY_KEY_RE = re.compile(r"t\('(palette\.[A-Za-z]+)'\)")


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
    src = re.sub(r'\{/\*.*?\*\}', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


def _block(path: Path, marker: str, terminator: str) -> str:
    code = _code(path)
    start = code.index(marker)
    return code[start:code.index(terminator, start)]


def _palette_entries() -> list[tuple[str, str, str, str]]:
    """从 `command-palette.ts` 解析出 `[(href, labelKey, groupKey, keywords), …]`。"""
    return _ENTRY_RE.findall(_block(_PURE, 'export const NAV_COMMANDS', '\n];'))


def _dock_entries() -> list[tuple[str, str]]:
    """底栏里的 `(href, 标签键)`。动作项没有 href，天然不会进来。

    正则里那条 `.*?` 是**非贪婪**的，所以它停在同一个条目的 `href:` 上，不会跨到
    下一项。动作项（快速添加 / 个人信息）因此天然落空——它们后面没有 `href:`。
    个人信息那一项里有一个 `<Link href="/settings">`，但那是 JSX 的双引号，
    匹配不到 `href:\\s*'`。
    """
    return [(href, key) for key, href in
            _DOCK_ENTRY_RE.findall(_block(_BAR, 'const dockItems = [', '\n  ];'))]


def _section_entries() -> list[tuple[str, str]]:
    """二级导航清单里的 `(href, 标签键)`。"""
    return [(m[1], m[2])
            for m in _SECTION_ITEM_RE.findall(_block(_SECTION_NAV, 'export const SECTION_NAV', '\n};'))]


def _settings_entries() -> list[tuple[str, str]]:
    """设置页 Tab 的 `(/settings/<tab>, 标签键)`。

    标签键从 `SETTINGS_TAB_LABEL_KEYS` 读（那是**唯一**一份：外壳与命令面板都从它
    取）。读外壳里那份是不行的——面板用的可能是另一份，而测试全绿。
    """
    code = _code(_SETTINGS_TABS)
    start = code.index('export const SETTINGS_TABS = [')
    tabs = re.findall(r"'([^']+)'", code[start:code.index('] as const;', start)])

    start = code.index('export const SETTINGS_TAB_LABEL_KEYS')
    block = code[start:code.index('};', start)]
    keys = dict(re.findall(r"(\w+):\s*'([^']+)'", block))
    return [(f'/settings/{tab}', keys[tab]) for tab in tabs if tab in keys]


def _locale(name: str) -> dict:
    return json.loads((_LOCALES / f'{name}.json').read_text(encoding='utf-8'))


class CommandPaletteBehaviourTest(unittest.TestCase):
    """跑 Node 侧的行为测试：检索与排序的真实实现。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_command_palette(self) -> None:
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
        # （与 `test_section_nav.py` / `test_reload_state.py` 同一处理。）
        if proc.returncode not in (0, 1):
            proc = run_once()
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'命令面板的检索逻辑不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class CommandPaletteInvariantTest(unittest.TestCase):

    def setUp(self) -> None:
        self.entries = _palette_entries()
        self.hrefs = [href for href, _, _, _ in self.entries]
        self.dock = _dock_entries()
        self.sections = _section_entries()
        self.settings = _settings_entries()
        # 解析失效 → 下面的断言会「一条都没查到」而静默通过。先钉住形状。
        self.assertEqual(
            len(self.dock), 8,
            f'从 ManagementBar 里解析出 {len(self.dock)} 个底栏目的地（期望 8）：{self.dock} —— '
            '`dockItems` 的写法变了？解析失效会让本类的交叉核对全部空转。',
        )
        self.assertEqual(
            len(self.sections), 6,
            f'从 section-nav.ts 里解析出 {len(self.sections)} 项（期望 6）：{self.sections}',
        )
        self.assertEqual(
            len(self.settings), 11,
            f'从 settings-tabs.ts 里解析出 {len(self.settings)} 个 Tab（期望 11）：{self.settings}',
        )
        self.assertGreaterEqual(
            len(self.entries), _EXPECTED_TOTAL,
            f'从 command-palette.ts 里只解析出 {len(self.entries)} 条 —— '
            '`NAV_COMMANDS` 的写法变了？解析失效会让本类的断言全部空转。',
        )

    def test_module_stays_runnable_without_node_modules(self) -> None:
        """`command-palette.ts` 不得出现**运行时** import。

        这是上面那条 Node 测试能跑起来的前提：`.mjs` 直接
        `import './command-palette.ts'`，而 Node 的 ESM 解析**要求带扩展名**，仓库的
        app 代码却一律不写扩展名。一旦 import 了别的模块的**值**（最容易顺手加的是
        `@/lib/i18n`），解析就会失败——症状是「Cannot find module」，看起来像路径
        写错。所以翻译函数**当参数传进来**，而不是 import。
        """
        offenders = [
            line.strip() for line in _code(_PURE).splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(
            offenders, [],
            'command-palette.ts 出现了运行时 import：\n  ' + '\n  '.join(offenders)
            + '\n本模块被 web/lib/command-palette.test.mjs 直接 import，引入运行时 '
              'import 会让那条测试以「找不到模块」失败。',
        )

    def test_palette_covers_exactly_the_real_destinations(self) -> None:
        """面板里的页面 == 底栏 + 二级导航 + 设置页 Tab 的并集，**一个不多一个不少**。

        三处来源各自解析一遍再合并，而不是照着清单复述：少了就是「那一页搜不到」，
        多了就是「搜得到但点下去 404 / 或本不该有入口」。两种都不会报错。
        """
        expected = sorted({h for h, _ in self.dock}
                          | {h for h, _ in self.sections}
                          | {h for h, _ in self.settings})
        self.assertEqual(
            len(expected), _EXPECTED_TOTAL,
            f'三处来源的并集是 {len(expected)} 项（期望 {_EXPECTED_TOTAL}）：{expected}',
        )
        self.assertEqual(
            sorted(self.hrefs), expected,
            '命令面板的清单与真实目的地不一致。\n'
            f'  面板里多出来的：{sorted(set(self.hrefs) - set(expected))}\n'
            f'  面板里缺的：{sorted(set(expected) - set(self.hrefs))}\n'
            '加了页面就要同步 `NAV_COMMANDS`（那是底栏 / 二级导航 / 设置页之外的第四处）。',
        )
        self.assertEqual(
            len(self.hrefs), len(set(self.hrefs)),
            f'面板里有重复的 href：{self.hrefs} —— 同一个页面会出现两行，'
            '而 `key` 相同会让 React 报警并可能只渲染一行。',
        )

    def test_label_keys_agree_with_every_source(self) -> None:
        """同一个 href 的标签键，在面板与它的来源里必须**逐字相同**。

        抄错一个字母不会报错：那一行显示裸键名（`settings.tabModel`），而所有
        「各语言键集一致」类的断言照样全绿——5 个语言包都没有这个键，它们彼此一致。
        """
        sources: dict[str, str] = {}
        for href, key in self.dock + self.sections + self.settings:
            sources.setdefault(href, key)
        wrong = [
            f'{href}: 面板用 {key}，来源用 {sources[href]}'
            for href, key, _, _ in self.entries
            if href in sources and sources[href] != key
        ]
        self.assertEqual(
            wrong, [],
            '以下页面的标签键在命令面板与它的来源里不一致（那一行会显示裸键名）：\n  '
            + '\n  '.join(wrong),
        )
        # 反向：来源里的每一项都必须在面板里找得到（并集断言已盖住，这里给出更具体的
        # 报错——「哪一处来源没被覆盖」比一串 href 好定位）。
        missing = [f'{href} ← {key}' for href, key in sources.items() if href not in set(self.hrefs)]
        self.assertEqual(
            missing, [],
            '这些真实目的地不在命令面板里（搜不到它们）：\n  ' + '\n  '.join(missing),
        )

    def test_every_item_has_a_real_route_directory(self) -> None:
        """面板里的每个 href 都要有真实存在的路由目录。

        漏了就是「搜得到、点下去 404」——而 404 页面不会说「你少建了个目录」。
        """
        missing = [
            f'{href} → 缺 web/app/(main){href}/page.tsx'
            for href in self.hrefs
            if not (_MAIN / href.strip('/') / 'page.tsx').is_file()
        ]
        self.assertEqual(
            missing, [],
            '命令面板里的这些项没有对应的路由目录（点进去会 404）：\n  ' + '\n  '.join(missing),
        )

    def test_every_item_carries_searchable_keywords(self) -> None:
        """每条都要有非空 keywords，且路径**在运行时**被并进可搜词。

        路径当关键词是**故意**的：「模型映射」这个 Tab 的中文名里没有 `models`，
        但它的路径是 `/settings/models`；不塞路径的话，习惯打路径的人一条都搜不到。

        但判据不是「每条的 keywords 字符串里都写着路径」——那会在
        `/red-packets`（keywords 写的是 `red packets redpackets`）这种地方误报，
        而它**照样搜得到**：路径由 `navCommands` 在翻译时并进去
        （`${seed.href} ${seed.keywords}`）。所以这里两头都钉：种子要有可搜词，
        合并那一步要真的存在。
        """
        bad = [href for href, _, _, kw in self.entries if not kw.strip()]
        self.assertEqual(
            bad, [],
            f'这些条目的 keywords 是空的（只能按中文名搜，英文用户搜不到）：{bad}',
        )
        self.assertRegex(
            _code(_PURE), r'keywords:\s*`\$\{seed\.href\}\s',
            '`navCommands` 没有把 `seed.href` 并进 keywords —— 那「按路径搜」'
            '（`/settings/models`）就失效了，而条目本身看起来完全正常。',
        )

    def test_palette_exposes_navigation_only(self) -> None:
        """面板里**只有跳转**，一条动作都没有。

        这是本文件最想拦住的一条。⌘K 的用法是「敲几个字 + 回车」，而回车之前的
        那个词往往只打了一半；在这种地方放「退出登录」「重启容器」这类不可逆操作，
        等于把**最容易误触的交互**接到**最危险的按钮**上。

        判据不是「数一数有没有别的条目」（那要靠人记得），而是**组件根本不该碰
        动作层**：它一旦 import 了 `@/lib/api` 或 `notify`，就说明有东西要被执行。
        """
        code = _code(_COMPONENT)
        for forbidden, why in (
            ("from '@/lib/api'", 'API 层：说明面板里有东西要**执行**，而不只是跳转'),
            ('notify', 'toast：同上，跳转不需要弹提示'),
            ('authApi', '认证动作（退出登录 / 吊销会话）'),
            ('systemApi', '系统动作（更新 / 重启）'),
        ):
            self.assertNotIn(
                forbidden, code,
                f'CommandPalette 里出现了 `{forbidden}`（{why}）。'
                '命令面板只做导航——不可逆操作不能挂在「敲两个字母 + 回车」上。',
            )
        # 入口侧也要有守卫：拿不到 href 就不该往下走。
        self.assertRegex(
            code, r'if\s*\(!item\??\.href\)\s*return;',
            'activate 没有守卫 `item.href` —— 没有 href 的条目会走进去，'
            '而那时「执行什么」完全没有定义。',
        )

    def test_component_is_mounted_globally_and_once(self) -> None:
        """面板挂在 `(main)` 外壳里，且**只挂一次**。

        挂在某一页里的话，其它页按 ⌘K 毫无反应；挂两次会让快捷键的开关互相抵消
        （按一下开、再按一下被另一份关掉，看起来像快捷键坏了）。
        """
        code = _code(_LAYOUT)
        uses = code.count('<CommandPalette')
        self.assertEqual(
            uses, 1,
            f'(main) 外壳里出现了 {uses} 次 <CommandPalette />。'
            '它必须在每一页都能按得出来，且只挂一份（两份会让快捷键开关互相抵消）。',
        )

    def test_component_delegates_matching_to_the_module(self) -> None:
        """检索与清单都只能来自 `@/lib/command-palette`，组件不得自己再写一套。

        组件里出现 `startsWith` / `includes(` 之类的匹配逻辑，就说明它在自己判定
        「哪条命中」——于是同一件事有两个说法（模块那份有 Node 行为测试，组件这份
        没有），改一处漏一处都不报错。

        判据取模块里那两个**私有**助手名（`normalize` / `termTier`）：它们一旦
        出现在组件里，就是「把匹配逻辑抄了一遍」的直接证据。不笼统地禁
        `toLowerCase`——组件用它是为了判快捷键（`event.key.toLowerCase() !== 'k'`），
        那是另一件事。
        """
        code = _code(_COMPONENT)
        for needed in ('navCommands(', 'matchCommands('):
            self.assertIn(
                needed, code,
                f'CommandPalette 没有用 {needed} —— 那它的清单 / 检索是哪来的？'
                '两份逻辑迟早不一致，而两处都不报错。',
            )
        for reimplemented in ('function normalize', 'termTier', 'function matchCommands'):
            self.assertNotIn(
                reimplemented, code,
                f'CommandPalette 里出现了 `{reimplemented}` —— 它在自己写匹配逻辑。'
                '匹配必须只来自 @/lib/command-palette（那里有行为测试）。',
            )

    def test_keyboard_and_aria_contract(self) -> None:
        """键盘与读屏契约：少一条都不会报错，只是「按了没反应」或读不出内容。

        这是**唯一**一批以键盘为主的改动，所以这些不能靠「以后做无障碍时再说」。
        """
        code = _code(_COMPONENT)
        for needed, why in (
            ('aria-activedescendant', '读屏软件不知道当前高亮的是哪一项（↑↓ 完全无反馈）'),
            ('role="combobox"', '输入框不是 combobox，读屏软件不会按「建议列表」播报'),
            ('role="listbox"', '结果列表没有列表语义'),
            ('role="option"', '每一项不是选项，读屏软件读不出「共几项、当前第几项」'),
            ('aria-selected', '当前项没有选中态'),
            ('ArrowDown', '↓ 不能移动高亮'),
            ('ArrowUp', '↑ 不能移动高亮'),
            ("event.key === 'Enter'", '回车不能打开当前项'),
            ('isComposing', '没有拦输入法合成态：用拼音打 zhanghao 按回车会直接跳走，'
                            '跳到哪条取决于当时高亮的是谁——用户完全无法预期'),
            ('scrollIntoView', '键盘移动高亮时不会把它滚进视野（移到了屏幕外，看起来像卡住）'),
        ):
            self.assertIn(needed, code, f'CommandPalette 缺少 `{needed}`：{why}。')

    def test_navigation_does_not_double_the_deploy_prefix(self) -> None:
        """跳转用 `router.push`，且**不**套 `withBasePath`。

        `next/navigation` 的路由器由 Next 自己加部署前缀；再套一层会变成
        `/wb/wb/tasks`。这与 `components/ui/floating-dock.tsx` 相反——那边是原生
        `<a href>`，Next 管不到，所以**必须**自己补。两处看着矛盾，判据只有一条：
        **这段路径经不经 Next 的路由器**。写反了本地（无前缀）完全看不出来。
        """
        code = _code(_COMPONENT)
        self.assertIn(
            'router.push(', code,
            'CommandPalette 没有用 router.push —— 换成 window.location 会整页重载，'
            '而且要把 basePath 的处理方式整个重想一遍。',
        )
        self.assertNotIn(
            'withBasePath', code,
            'CommandPalette 用了 withBasePath。`router.push` 由 Next 自己加部署前缀，'
            '再套一层会变成 `/wb/wb/tasks`（本地无前缀，测不出来）。'
            'floating-dock 需要它是因为那边是原生 <a>。',
        )

    def test_renders_the_markers_the_acceptance_script_uses(self) -> None:
        """验收脚本按 `data-slot` 定位；少一个就有一条断言恒真（空转）。

        「找不到元素 → 数量为 0 → 断言 0 == 0 通过」是这一系列批次反复踩的坑
        （`[role=tab]` 那次）。所以这里逐个钉住标记名。
        """
        code = _code(_COMPONENT)
        missing = [slot for slot in _REQUIRED_SLOTS if f'data-slot="{slot}"' not in code]
        self.assertEqual(
            missing, [],
            'CommandPalette 缺少这些定位标记（验收脚本会因此恒真）：' + '、'.join(missing),
        )
        self.assertIn(
            'data-slot="command-palette"', code,
            '面板容器没有 data-slot="command-palette" —— 验收脚本定位不到它。',
        )

    def test_copy_keys_exist_in_every_locale(self) -> None:
        """组件用到的每个键都必须在**每个**语言包里存在。

        `test_web_i18n` 的 `test_all_literal_keys_exist` 只回查**源语言**字典；
        某个目标语言缺了这一条，界面上会直接显示裸键名（`palette.placeholder`）。
        这里逐个语言包查，且键集**从组件源码里扫**，不另抄一份。
        """
        keys = sorted(set(_COPY_KEY_RE.findall(_COMPONENT.read_text(encoding='utf-8'))))
        self.assertGreaterEqual(
            len(keys), 8,
            f'从组件里只扫到 {len(keys)} 个文案键（{keys}）—— `t(\'palette.…\')` 的写法'
            '变了？解析失效会让文案断言空转。',
        )
        missing: list[str] = []
        for name in ('zh-CN', 'zh-TW', 'en', 'ja', 'ko'):
            table = _locale(name).get('palette')
            self.assertIsInstance(table, dict, f'{name}.json 没有 palette 段')
            for dotted in keys:
                value = table.get(dotted.partition('.')[2])
                if not isinstance(value, str) or not value.strip():
                    missing.append(f'{name}: {dotted}')
        self.assertEqual(
            missing, [],
            '以下语言包缺少命令面板文案，界面上会显示裸键名：\n  ' + '\n  '.join(missing),
        )

    def test_acceptance_script_targets_the_palette(self) -> None:
        """浏览器验收脚本必须真的走一遍命令面板。

        没有这一段的话，「⌘K 能打开、能搜到、能跳」全是**没被验证过**的宣称：
        单元测试只证明检索函数算得对，证明不了它接在了界面上、也证明不了
        回车真的换了路由。
        """
        self.assertTrue(_VERIFY.is_file(), f'找不到验收脚本: {_VERIFY}')
        code = _VERIFY.read_text(encoding='utf-8')
        for needed, why in (
            ('data-slot=command-palette-trigger', '没有从按钮打开过面板'),
            ('data-slot=command-palette-input', '没有真的在输入框里打字'),
            ('data-slot=command-palette-item', '没有核对过搜索结果'),
        ):
            self.assertIn(
                needed, code,
                f'验收脚本里找不到 `{needed}`（{why}）——'
                '「⌘K 能用」就成了没被验证过的宣称。',
            )


if __name__ == '__main__':
    unittest.main()

"""页面内二级导航（批次 4 ②）—— 行为测试借 Node 执行，结构不变式在源码上断言。

为什么值得有：这一批把底栏**目的地从 11 项收敛到 8 项**（任务记录 / 红包 /
聊天测试台 改为在「账号」/「密钥」/「模型」页里用页内二级导航切换）。做错了
都不会报错，只是界面看起来不对：

  · 清单里加了一项、却没有对应的路由目录 → 点进去 404；
  · 有路由目录、却不在清单里 → 页面上根本没有那个 Tab（页面其余部分完全正常）；
  · 某页忘了渲染 `<PageSectionTabs />` → 那一页少了导航，其余一切正常；
  · 底栏没把被吸收的三项去掉 → 收敛到 8 项这件事等于没做（而「分组」还在，
    看起来一切正常）；
  · 路径匹配放宽成**后缀** → `/settings/models` 以 `/models` 结尾，设置页被认成
    「模型」节（默认部署形态下就会发生）；
  · 调用方忘了把部署前缀交给解析函数 → 子路径部署下全部失效，本地永远复现不出来。

前两条由 `web/lib/section-nav.test.mjs` 的行为测试盖住（跑的是真实实现，
不是复制一份逻辑）；其余几条只能在**源码形状**上断言。

**本模块不复述清单**：节名、项、href 都从 `web/lib/section-nav.ts` 里解析出来
（`_section_nav()`）。复述一份副本的测试只会证明「副本和自己一致」。

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
_SCRIPT = _WEB / 'lib' / 'section-nav.test.mjs'
_PURE = _WEB / 'lib' / 'section-nav.ts'
_TABS = _WEB / 'components' / 'common' / 'layout' / 'PageSectionTabs.tsx'
_BAR = _WEB / 'components' / 'common' / 'layout' / 'ManagementBar.tsx'
_DOCK_FIXTURE = _WEB / 'lib' / 'dock-groups.test.mjs'
_LOCALES = _WEB / 'lib' / 'i18n' / 'locales'

_NODE = shutil.which('node')

# 只跑 .mjs，不碰 .ts —— Node 的 type stripping 从 22.6 起才有
_MIN_MAJOR = 22

# 底栏里**不**属于任何一节的 5 个目的地。它们不该因为这一批而消失，
# 所以在这里写死：写死才是断言（从别处推出来的「应该还在」证明不了它们还在）。
_OTHER_DESTINATIONS = ['/dashboard', '/stats', '/logs', '/traces', '/security', '/settings']

_ITEM_RE = re.compile(
    r"\{key:\s*'([^']+)',\s*href:\s*'([^']+)',\s*labelKey:\s*'([^']+)',"
    r"\s*iconKey:\s*'([^']+)'\}"
)


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
    src = re.sub(r'\{/\*.*?\*/\}', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


def _section_nav() -> dict[str, list[tuple[str, str, str, str]]]:
    """从 `section-nav.ts` 解析出 `{节: [(key, href, labelKey, iconKey), ...]}`。

    解析而不是复述：清单只有一份，测试来读它。清单一旦改写法（比如换成
    常量拼接），`_ITEM_RE` 会一条都匹配不到 —— 所以 `setUp` 里断言了
    「三节、每节两项」，让解析失效变成**失败**而不是空转。
    """
    src = _PURE.read_text(encoding='utf-8')
    start = src.index('export const SECTION_NAV')
    end = src.index('\n};', start)
    block = src[start:end]

    out: dict[str, list[tuple[str, str, str, str]]] = {}
    parts = re.split(r'\n  (\w+): \[', block)
    # parts = [前置, 节名, 该节内容, 节名, 该节内容, ...]
    for i in range(1, len(parts) - 1, 2):
        out[parts[i]] = _ITEM_RE.findall(parts[i + 1])
    return out


def _dock_hrefs() -> list[str]:
    """`const dockItems = [ ... ]` 里出现的 `href: '...'`（动作项没有 href）。"""
    code = _code(_BAR)
    start = code.index('const dockItems = [')
    end = code.index('\n  ];', start)
    return re.findall(r"href:\s*'([^']+)'", code[start:end])


class SectionNavBehaviourTest(unittest.TestCase):
    """跑 Node 侧的行为测试：清单与路径解析的真实实现。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_section_nav(self) -> None:
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
        # （与 `test_dock_groups.py` / `test_settings_tabs.py` 同一处理。）
        if proc.returncode not in (0, 1):
            proc = run_once()
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'二级导航逻辑不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class SectionNavInvariantTest(unittest.TestCase):

    def setUp(self) -> None:
        self.nav = _section_nav()
        # 解析失效 → 下面的断言会「一条都没查到」而静默通过。先钉住形状。
        self.assertEqual(
            sorted(self.nav), ['accounts', 'keys', 'models'],
            f'从 section-nav.ts 里解析出的节不对：{sorted(self.nav)} —— '
            '`SECTION_NAV` 的写法变了？解析失效会让这个类里的断言全部空转。',
        )
        for section, items in self.nav.items():
            self.assertEqual(
                len(items), 2,
                f'「{section}」这一节解析出 {len(items)} 项，应当是 2 项。'
                '只有一项就不该叫「二级导航」；解析失效也会走到这里。',
            )

    def test_module_stays_runnable_without_node_modules(self) -> None:
        """`section-nav.ts` 不得出现**运行时** import，也不得依赖 `BASE_PATH`。

        前一半是上面那条 Node 测试能跑起来的前提：`.mjs` 直接
        `import './section-nav.ts'`，而 Node 的 ESM 解析**要求带扩展名**，仓库的
        app 代码却一律不写扩展名。一旦 import 了别的模块的**值**，解析就会失败
        ——症状是「Cannot find module」，看起来像路径写错。

        后一半是这一批的坑：部署前缀要**由调用方传进来**（`basePath` 参数），
        本模块不能自己去 import `@/lib/base-path`。自己 import 既破坏了零依赖，
        又把「前端逻辑测试」和 Next 的运行时绑在一起。

        注意这条**只**约束 import：形参叫 `basePath`（小写），不触发下面的断言。
        """
        offenders = [
            line.strip() for line in _code(_PURE).splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(
            offenders, [],
            'section-nav.ts 出现了运行时 import：\n  ' + '\n  '.join(offenders)
            + '\n本模块被 web/lib/section-nav.test.mjs 直接 import，引入运行时 import '
              '会让那条测试以「找不到模块」失败。',
        )
        self.assertNotIn(
            'BASE_PATH', _code(_PURE),
            'section-nav.ts 用到了 BASE_PATH。部署前缀必须由调用方**传参数**进来'
            '（见 `sectionNavFromPath`）——自己 import 就不是零依赖了，'
            '而且这条 Node 测试会因为解析不到 `@/lib/base-path` 而失败。',
        )

    def test_parser_requires_the_deploy_prefix(self) -> None:
        """`sectionNavFromPath` 的 `basePath` 必须是**必填**参数。

        写成可选（`basePath?: string` 或给默认值 `= ''`）看起来更省事，代价是
        「调用方忘了传」变成静默错误：根路径部署下一切正常，子路径部署下二级
        导航**整排消失**，而本地永远复现不出来。必填参数至少让 TypeScript 在
        编译期拦住——那是这个仓库唯一会在 CI 前挡住它的东西。
        """
        code = _code(_PURE)
        self.assertRegex(
            code, r'sectionNavFromPath\(\s*pathname:[^,]+,\s*basePath\s*:\s*string\s*,',
            'sectionNavFromPath 的第二个参数不是必填的 `basePath: string`。'
            '可选参数会让「忘了传前缀」在子路径部署下静默失效。',
        )
        self.assertIsNone(
            re.search(r'basePath\s*\?', code),
            'basePath 被写成了可选参数（`basePath?`）——它必须必填。',
        )
        self.assertIsNone(
            re.search(r'basePath\s*:\s*string\s*=', code),
            'basePath 带了默认值——它必须必填，默认值会让漏传变成静默错误。',
        )

    def test_every_item_has_a_real_route_directory(self) -> None:
        """清单里的每个 `href` 都要有真实存在的路由目录。

        漏了就是「导航上看得见、点进去 404」——而 404 页面不会说「你少建了个
        目录」，用户只会以为面板坏了。
        """
        missing: list[str] = []
        for section, items in self.nav.items():
            for key, href, _, _ in items:
                page = _MAIN / href.strip('/') / 'page.tsx'
                if not page.is_file():
                    missing.append(f'{section}/{key}: {href} → 缺 {page.relative_to(_ROOT)}')
        self.assertEqual(
            missing, [],
            '清单里的这些项没有对应的路由目录（点进去会 404）：\n  '
            + '\n  '.join(missing),
        )

    def test_each_section_starts_at_its_own_landing_page(self) -> None:
        """每节的**第一项**必须是该节的落点，且 href 与节名同名。

        底栏点进「账号」落到哪一页，取决于这一条：把顺序写反了，用户点「账号」
        会直接进「任务记录」，而且看起来完全正常。
        """
        wrong = [f'{s}: 第一项是 {items[0][1]}' for s, items in self.nav.items()
                 if items[0][1] != f'/{s}']
        self.assertEqual(
            wrong, [],
            '每节的第一项必须是该节的落点（href 与节名一致）：\n  ' + '\n  '.join(wrong),
        )

    def test_dock_keeps_the_section_heads_and_drops_the_absorbed(self) -> None:
        """底栏保留三节的落点，且**不再**出现被吸收的三页。

        这是这一批真正要交付的东西（11 → 8）。两种错法都不会报错：
        被吸收的页还挂在底栏上（收敛没做），或者连落点也一起删了（少了一个入口，
        那一节再也进不去）。
        """
        hrefs = _dock_hrefs()
        heads = [items[0][1] for items in self.nav.values()]
        absorbed = [item[1] for items in self.nav.values() for item in items[1:]]

        self.assertEqual(
            [h for h in heads if h not in hrefs], [],
            '底栏里找不到这些节的落点——那一节就没有入口了：\n  '
            + '\n  '.join(h for h in heads if h not in hrefs),
        )
        self.assertEqual(
            [h for h in absorbed if h in hrefs], [],
            '底栏里仍然挂着这些已被吸收的页——收敛到 8 项这件事等于没做'
            '（它们应当只出现在页面内的二级导航里）：\n  '
            + '\n  '.join(h for h in absorbed if h in hrefs),
        )

    def test_dock_has_exactly_nine_destinations(self) -> None:
        """底栏**目的地**正好 9 项（动作项没有 href，不计入）。

        写死数量是**有意**的。历史：批次 4 ② 把 11 项收敛到 8 项；随后「轨迹」
        （调用抓包）作为**与日志同级**的入口加回来，成为第 9 项 —— 它是「逐轮看
        这一跑发了什么」的独立动作，与「扫一眼谁用了多少」不是同一件事，因此
        没有并进日志节的二级导航。

        这条断言仍然值得留：少一项说明有人顺手删了个入口；多一项说明有人又加了
        一级入口 —— 两者都该被问一句「它是不是该并进哪一节」。
        """
        hrefs = _dock_hrefs()
        self.assertEqual(
            len(hrefs), 9,
            f'底栏有 {len(hrefs)} 个目的地，应当是 9 个：{hrefs}',
        )
        # 用集合合并：每节的落点也可能同时出现在 _OTHER_DESTINATIONS 里
        # （`/logs` 既是底栏目的地、又是「日志」节的落点），列表相加会把它算两次。
        expected = sorted(set([items[0][1] for items in self.nav.values()]
                              + _OTHER_DESTINATIONS))
        self.assertEqual(
            sorted(hrefs), expected,
            '底栏的目的地与预期不符。预期的另外 ' + str(len(_OTHER_DESTINATIONS))
            + ' 项（' + '、'.join(_OTHER_DESTINATIONS) + '）各有独立的心智模型，不该被合并。',
        )

    def test_every_page_in_a_section_renders_the_tabs(self) -> None:
        """该有二级导航的页面**都要**渲染 `<PageSectionTabs />`。

        漏了不会报错：那一页少一排 Tab，其余部分完全正常——只有把两页并排看
        才发现。所以逐个文件检查，而不是「至少有一处渲染了」。
        """
        missing: list[str] = []
        for section, items in self.nav.items():
            for key, href, _, _ in items:
                page = _MAIN / href.strip('/') / 'page.tsx'
                if not page.is_file():
                    continue  # 缺文件由 test_every_item_has_a_real_route_directory 报
                if '<PageSectionTabs' not in _code(page):
                    missing.append(f'{section}/{key}: {href}')
        self.assertEqual(
            missing, [],
            '这些页面没有渲染 <PageSectionTabs />（页面上会少一排二级导航）：\n  '
            + '\n  '.join(missing),
        )

    def test_tabs_derive_the_active_item_from_the_path(self) -> None:
        """`PageSectionTabs` 不接参数，「我在哪一节」只能由路径推出来。

        接一个 `section` / `active` 参数看起来更灵活，但那多出两种错法：传错一节
        （页面顶部出现两条指向别处的 Tab）或传错 active（点了没反应）。两种都
        不会报错。所以这里钉住「无参数 + 读 usePathname」。
        """
        code = _code(_TABS)
        self.assertRegex(
            code, r'export function PageSectionTabs\(\)',
            'PageSectionTabs 接受了参数。判据应当只来自路径——传参多出「传错一节」'
            '与「传错 active」两种静默错法。',
        )
        self.assertIn(
            'usePathname()', code,
            'PageSectionTabs 没有读 usePathname —— 那它靠什么知道当前在哪一项？',
        )
        self.assertIn(
            'sectionNavFromPath', code,
            'PageSectionTabs 没有用 sectionNavFromPath 解析路径，等于自己又写了一套。',
        )

    def test_tabs_pass_the_deploy_prefix_to_the_parser(self) -> None:
        """`PageSectionTabs` 必须把 `BASE_PATH` 交给解析函数。

        解析函数改成「先剥部署前缀、再精确匹配」之后，前缀就成了**调用方的责任**
        （它自己不能 import，见 `test_module_stays_runnable_without_node_modules`）。
        漏传的表现是子路径部署下二级导航整排消失——本地（`BASE_PATH === ''`）
        完全看不出来，所以只能在这里钉住。

        子路径部署时 `usePathname()` 是**带着**前缀的
        （`/workbuddy-manager/tasks`），所以这个参数不是「加上去」而是「剥掉」。
        """
        code = _code(_TABS)
        self.assertIn(
            'BASE_PATH', code,
            'PageSectionTabs 没有 import BASE_PATH —— 那它没法把部署前缀交给'
            'sectionNavFromPath，子路径部署下二级导航会整排消失。',
        )
        self.assertRegex(
            code, r'sectionNavFromPath\(\s*usePathname\(\)\s*,\s*BASE_PATH\s*\)',
            'PageSectionTabs 调 sectionNavFromPath 时没有传 BASE_PATH，'
            '或者传的不是 usePathname()。子路径部署下前缀剥不掉 → 认不出路径 → '
            '二级导航整排消失（本地 BASE_PATH 为空串，测不出来）。',
        )

    def test_dock_groups_fixture_matches_the_real_dock(self) -> None:
        """`dock-groups.test.mjs` 里那份「照着 ManagementBar 的口径」的副本要跟着改。

        那份副本是**手写**的（`dock-groups.ts` 是纯函数，测试得给它一份输入），
        而它上面写着「照着 ManagementBar 的口径」。这一批改底栏时先漏了它，
        表现是那条 Node 测试报「运营组应当 6 项、实际 3 项」——如果没人跑它，
        这份副本就会一直躺在那儿，声称底栏还是 11 项。

        判据取「被吸收的那几项不在副本里」：副本**必须**跟着收敛。
        """
        fixture = _DOCK_FIXTURE.read_text(encoding='utf-8')
        absorbed = [item[0] for items in self.nav.values() for item in items[1:]]
        stale = [k for k in absorbed if f"item('{k}'," in fixture]
        self.assertEqual(
            stale, [],
            'web/lib/dock-groups.test.mjs 的「真实底栏」副本里仍然有这些已被吸收的项：'
            + '、'.join(stale) + ' —— 那份副本声称自己「照着 ManagementBar 的口径」，'
            '不同步就是在说谎。',
        )

    def test_label_keys_exist_in_all_locales(self) -> None:
        """导航标签用的 i18n 键必须在**每个**语言包里都存在。

        缺了不会报错：页面上直接显示裸键名（`nav.redPackets`），而且
        `test_web_i18n` 只查「各语言键集是否一致」，**不查引用是否存在**。
        """
        needed = sorted({item[2] for items in self.nav.values() for item in items})
        self.assertTrue(needed, '没解析出任何 labelKey —— 解析失效了（会假通过）')
        self.assertTrue(_LOCALES.is_dir(), f'找不到语言包目录: {_LOCALES}')
        missing: list[str] = []
        for path in sorted(_LOCALES.glob('*.json')):
            data = json.loads(path.read_text(encoding='utf-8'))
            for dotted in needed:
                head, _, tail = dotted.partition('.')
                node = data.get(head)
                if not isinstance(node, dict) or not isinstance(node.get(tail), str) \
                        or not node.get(tail).strip():
                    missing.append(f'{path.name}: {dotted}')
        self.assertEqual(
            missing, [],
            '以下语言包缺少二级导航用到的键，页面上会直接显示裸键名：\n  '
            + '\n  '.join(missing),
        )


if __name__ == '__main__':
    unittest.main()

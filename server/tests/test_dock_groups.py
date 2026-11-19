"""底栏（浮动 Dock）的分组语义（前端逻辑，借 Node 执行）+ 渲染层的结构不变式。

为什么值得有：这一批改的是**信息架构**，做错了不会报错，只是底栏看起来不对——

  · 分组切错（同名分组跨距离被合并 / 同组散落被切开）→ 组名与组内内容对不上；
  · 分隔线不带组名 → 用户只能猜那条竖线分开的是什么（这正是这一批要修的）；
  · 手机端不画分组 → 11 项平铺，分组语义在手机上完全消失（原来就是靠一句
    `if (item.title === 'divider') return null` 硬跳过的）；
  · 引导气泡没有关闭入口、也不响应「点别处」→ 它会一直挂在底栏正上方，而底栏
    可以被拖到页面中部，于是正好压住正文（用户反馈「反复出现在页面中部」）；
  · 切到「固定底部」后拖动没真的关掉 → 先跟手走一段、松手才归位，看起来像拖动坏了；
  · 引导里留一条当前模式下**做不到**的话（固定模式下还说「长按可拖动」）→
    用户会反复长按然后怀疑是自己手法不对。

第一条由 `web/lib/dock-groups.test.mjs` 的行为测试盖住（跑的是真实实现，不是复制
一份逻辑）；其余几条只能在**源码形状**上断言 —— 行为测试证明不了「渲染层真的用了
那个分组结果」，而这几条恰恰都是「算了却没用在分支上」型的错。

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
_SCRIPT = _ROOT / 'web' / 'lib' / 'dock-groups.test.mjs'
_PURE = _ROOT / 'web' / 'lib' / 'dock-groups.ts'
_DOCK = _ROOT / 'web' / 'components' / 'ui' / 'floating-dock.tsx'
_BAR = _ROOT / 'web' / 'components' / 'common' / 'layout' / 'ManagementBar.tsx'
_LOCALES = _ROOT / 'web' / 'lib' / 'i18n' / 'locales'
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
    src = re.sub(r'\{/\*.*?\*/\}', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


def _dock_items_block(code: str) -> str:
    """切出 `const dockItems = [ ... ]` 这一段，供「每一项都带分组键」这类断言用。

    不能直接搜全文数 `groupKey:`：那个词在注释与别处也会出现，数出来的数对不上
    条目数时，报错信息会指向一个根本不存在的问题。
    """
    start = code.index('const dockItems = [')
    end = code.index('\n  ];', start)
    return code[start:end]


class DockGroupsBehaviourTest(unittest.TestCase):
    """跑 Node 侧的行为测试：分组切分（`splitByGroup`）的真实实现。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_split_by_group(self) -> None:
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
        # （与 `test_account_list.py` / `test_account_status.py` 同一处理。）
        if proc.returncode not in (0, 1):
            proc = run_once()
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'底栏分组逻辑不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class DockGroupsInvariantTest(unittest.TestCase):
    """行为测试盖不到的前提与结构不变式。"""

    def test_module_stays_runnable_without_node_modules(self) -> None:
        """`dock-groups.ts` 不得出现**运行时** import。

        这是上面那条 Node 测试能跑起来的前提：`.mjs` 直接 `import './dock-groups.ts'`，
        而 Node 的 ESM 解析**要求带扩展名**，仓库的 app 代码却一律不写扩展名。
        一旦本模块 import 了别的模块的**值**，解析就会失败——症状是「Cannot find
        module」，看起来像路径写错。
        """
        offenders = [
            line.strip() for line in _code(_PURE).splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(
            offenders, [],
            'dock-groups.ts 出现了运行时 import：\n  ' + '\n  '.join(offenders)
            + '\n本模块被 web/lib/dock-groups.test.mjs 直接 import，引入运行时 import '
              '会让那条测试以「找不到模块」失败。',
        )

    def test_divider_sentinel_is_gone(self) -> None:
        """哨兵条目 `{title: 'divider'}` 必须彻底消失，两个文件都不能再有。

        这是这一批的**根因**：分组曾经靠一个混在 `items` 里的假条目表达，
        于是每个渲染分支都得记得跳过它——移动端就是靠
        `if (item.title === 'divider') return null` 硬跳过的，代价是手机上
        **完全没有分组语义**。只要这个哨兵回来（或那句跳过回来），移动端就会
        重新退化成「11 项平铺」。
        """
        bar, dock = _code(_BAR), _code(_DOCK)
        self.assertNotIn(
            "'divider'", bar,
            'ManagementBar.tsx 里又出现了 divider 哨兵条目——分组语义应当挂在条目的 '
            'groupKey 上，不要再往 items 里塞假条目。',
        )
        self.assertNotIn(
            "'divider'", dock,
            'floating-dock.tsx 里又出现了按 title 跳过哨兵的写法——那正是移动端分组'
            '消失的原因。',
        )

    def test_every_dock_item_carries_a_group_key(self) -> None:
        """底栏**每一项**都要带 `groupKey`。

        漏掉一项不会报错：`splitByGroup` 会把 `groupKey` 缺省当成空串 '',
        于是那一项自己成为一组——底栏上凭空多出一条分隔线，或者它被划进
        相邻的组里，组名与内容对不上。
        """
        block = _dock_items_block(_code(_BAR))
        titles = len(re.findall(r"title: t\('nav\.", block))
        keys = len(re.findall(r'\bgroupKey:', block))
        self.assertGreater(titles, 0, '找不到底栏条目：`const dockItems = [` 的写法变了？')
        self.assertEqual(
            keys, titles,
            f'底栏有 {titles} 项，却只有 {keys} 项写了 groupKey。漏写的那一项会'
            '自成一組（缺省当空串处理），底栏上会多出一条分隔线，或它的组名与内容对不上。',
        )

    def test_group_labels_are_declared_on_group_leads(self) -> None:
        """三组语义各自有组名，且「动作组」刻意没有组名。

        `splitByGroup` 只取组内**首项**的 label，所以组名写在该组第一项上即可；
        但三个组名一个都不能少——少了就是「有分隔线、说不出分的是什么」，
        正是这一批要修的那句「分隔线无标签」。
        """
        block = _dock_items_block(_code(_BAR))
        for key in ("t('nav.groupOverview')", "t('nav.groupOps')", "t('nav.groupGovernance')"):
            self.assertIn(key, block, f'底栏缺少组名 {key}')
        # 动作组（快速添加 / 个人信息）不是「目的地」而是动作，刻意不带组名：
        # 只画一条分隔线与前面的页面分开，不要给它编一个组名。
        self.assertNotIn(
            "groupLabel: t('nav.groupActions')", block,
            '动作组不该有组名——快速添加与个人信息是动作不是页面，硬起一个组名'
            '反而让人以为那是一类页面。',
        )

    def test_both_layouts_render_groups(self) -> None:
        """桌面端与手机端**都**要按分组渲染，两处都要有。

        断言「两处都有」而不是「出现过」：只改桌面端的话，手机上依然是 11 项平铺
        （这一批要修的就是这个），而且界面不报错，看不出漏了。
        """
        dock = _code(_DOCK)
        self.assertEqual(
            dock.count('splitByGroup(items)'), 2,
            'splitByGroup 没有同时用在桌面端与手机端两个分支上——只改一处的话，'
            '另一种布局下分组语义仍然是缺失的（手机上尤其明显：11 项平铺）。',
        )
        self.assertIn(
            'data-slot="dock-group-divider"', dock,
            '桌面端的分隔线没有标记，验收脚本与测试都定位不到它',
        )
        self.assertIn(
            'data-slot="dock-group-separator"', dock,
            '手机端没有分组分隔线——手机上会退化成 11 项平铺，看不出哪几项是一类',
        )
        # 组名要挂在分隔线上（桌面端靠近时显示、手机端直接显示），不能只在数据里躺着
        self.assertIn('<GroupDivider label={group.label}', dock,
                      '桌面端的分隔线没把组名传下去，那条竖线又变成无标签的了')
        self.assertRegex(
            dock, r'group\.label \?',
            '手机端的分隔线没把组名渲染出来',
        )
        # 第一组之前不画线：`index > 0` 两处都要有（否则底栏最左边凭空多一条竖线）
        self.assertEqual(
            dock.count('> 0 &&'), 2,
            '两个渲染分支都要判断「不是第一组才画线」——少了就会在最前面多画一条'
            '分隔线，看起来像「前面还有一组」。',
        )

    def test_divider_label_is_driven_by_live_rects(self) -> None:
        """组名的显隐必须由「当场量出来的矩形」决定，**不能**靠分隔线自己的 hover 事件。

        这是这一批唯一一条「浏览器里真的坏了、而源码看着完全正常」的问题，实测出来的
        过程值得记下来：底栏是会放大的——鼠标一动附近的图标就从 40px 长到 70px，整个
        底栏随之变宽（实测 **725 → 787px**）。底栏是 `translateX(-50%)` 居中的，于是
        **分隔线会从光标底下挪走**（实测挪 8~30px，而线本身只有 1px 宽 + 8px 内边距）。
        用 `onMouseEnter`/`onMouseLeave` 的结果是「组名亮一下又灭」——采样出来是
        `11111 000000000000000`，而图标自己的 tooltip 一直稳定在屏上（它的放大围绕
        光标，所以不会把自己挤走）。

        判据分两半，缺一不可：
          · 分隔线组件里**不许**再有自己的 hover 事件（那是坏掉的那一版）；
          · 桌面底栏的 mousemove 里**必须**当场量矩形（`getBoundingClientRect`），
            否则「挪走」这件事没被考虑进去。

        只在源码上钉住是**不够**的（`active` 完全可能被传成常量而永远为假），所以
        浏览器层还有一条：`dev/verify_state_honesty.mjs` 里 hover 完要能读到组名。
        """
        dock = _code(_DOCK)
        start = dock.index('const GroupDivider')
        end = dock.index('GroupDivider.displayName', start)
        block = dock[start:end]
        self.assertIn(
            'data-slot="dock-group-divider"', block,
            '切不出 GroupDivider 的实现（锚点变了？）——这条断言会变成空转',
        )
        self.assertNotIn(
            'onMouseEnter', block,
            '分隔线又用回自己的 onMouseEnter 了：图标放大会把分隔线从光标底下推走'
            '（实测 8~30px，线本身只有 9px 宽），组名会「亮一下又灭」。'
            '判据要改成由底栏的 mousemove 当场量矩形。',
        )
        self.assertNotIn(
            'onMouseLeave', block,
            '分隔线又用回自己的 onMouseLeave 了：理由同上——它会被图标放大推走。',
        )

        # 桌面底栏的 mousemove 里必须真的量矩形，并把结果传到分隔线上
        dstart = dock.index('const FloatingDockDesktop')
        dend = dock.index('FloatingDockDesktop.displayName', dstart)
        desktop = dock[dstart:dend]
        self.assertIn(
            'getBoundingClientRect', desktop,
            '桌面底栏的 mousemove 没有当场量分隔线的矩形：图标放大让底栏变宽、分隔线'
            '被推开之后，判据就不再指向光标下的那一条了。',
        )
        self.assertIn(
            'DIVIDER_HIT_SLOP', desktop,
            '没有给分隔线留命中区：线只有 1px 宽（含内边距 9px），必须留出容差',
        )
        self.assertRegex(
            desktop, r'active=\{activeDivider ===',
            '量出来的序号没有传到 GroupDivider 上（算了却没用，等于没修）',
        )

    def test_tip_can_always_be_dismissed(self) -> None:
        """引导气泡必须**随时关得掉**：点别处能关、也有明确的关闭按钮。

        原实现只能靠「把几条都点完」关掉，于是它会一直挂在底栏正上方——而底栏可以
        被拖到页面中部，于是它正好压住正文（用户反馈「反复出现在页面中部」）。
        """
        bar = _code(_BAR)
        self.assertIn(
            "closest('[data-slot=dock-tip]')", bar,
            '引导气泡没有「点别处就关掉」的处理：它会一直挂着，而底栏可以被拖到'
            '页面中部，正好压住正文。',
        )
        self.assertRegex(
            bar, r"addEventListener\('pointerdown',\s*handlePointerDown,\s*true\)",
            '「点别处关掉」要用**捕获**阶段监听：气泡挂在底栏内部，底栏自己的'
            'pointerdown 冒泡到 document 之前可能已被处理，气泡会关不掉。',
        )
        self.assertIn(
            'data-slot="dock-tip"', bar,
            '引导气泡没有标记，验收脚本定位不到它',
        )
        self.assertRegex(
            bar, r"onClick=\{handleDismissDockTip\}",
            '引导气泡没有明确的关闭按钮——用户不想看时只能一条条点完',
        )

    def test_tip_index_is_clamped(self) -> None:
        """引导步骤的索引要夹回范围内。

        切换「固定底部 / 悬浮」会让步骤数变化（固定模式下「长按可拖动」那一条被
        去掉）。停在最后一步时切过去，索引就越界了，直接取会渲染出 `undefined`
        ——气泡里出现空白，而且不报错。
        """
        bar = _code(_BAR)
        self.assertRegex(
            bar, r'dockTipSteps\[Math\.min\(dockTipStep,\s*dockTipSteps\.length - 1\)\]',
            '引导气泡直接用了 dockTipStep 取步骤：切换停靠模式会让步骤数变少，'
            '索引越界时渲染出 undefined（气泡里一片空白，且不报错）。',
        )

    def test_pinned_mode_really_disables_dragging(self) -> None:
        """「固定底部」模式下拖动要**彻底关掉**，而不是拖完再弹回来。

        后者会先跟手走一段、松手才归位，看起来像拖动坏了。同时引导里那条
        「长按空白区域可拖动」也必须跟着消失——留一句当前模式下做不到的话，
        用户会反复长按然后怀疑是自己手法不对。
        """
        bar = _code(_BAR)
        self.assertRegex(
            bar, r"if \(dockMode !== 'floating'\) return;",
            '「固定底部」模式下没有把拖动关掉：底栏会先跟手走一段、松手才弹回原位，'
            '看起来像拖动坏了。',
        )
        for step in ('dock.mobile2', 'dock.desktop1'):
            self.assertIn(
                f"dockMode === 'floating' ? [t('{step}')]", bar,
                f'引导里 {step} 那条（「长按可拖动」）没有跟着停靠模式去掉：'
                '固定底部时拖动已被禁用，留一句做不到的话比不写更糟。',
            )

    def test_dock_mode_is_remembered(self) -> None:
        """停靠模式要**写进也读回** localStorage。

        只写不读（或只读不写）都会让这个开关看起来是坏的：刷新一次就回到默认，
        用户每次打开面板都要重设一遍，而界面上没有任何提示说明原因。
        两个方向都要断言——只查「这个键出现过」的话，删掉写或读任一侧仍然是绿的。
        """
        bar = _code(_BAR)
        self.assertRegex(
            bar, r'localStorage\.setItem\(DOCK_MODE_STORAGE_KEY',
            '停靠模式没有写进 localStorage：刷新一次就回到默认',
        )
        self.assertRegex(
            bar, r'localStorage\.getItem\(DOCK_MODE_STORAGE_KEY',
            '停靠模式没有从 localStorage 读回：存了也不生效，刷新后仍是默认',
        )

    def test_group_keys_exist_in_all_locales(self) -> None:
        """底栏新增的文案键必须在**每个**语言包里都存在。

        缺了不会报错：页面上直接显示裸键名（`nav.groupOps`），而且
        `test_web_i18n` 只查「各语言键集是否一致」，**不查引用是否存在**——
        只有这条能发现「代码引用了没人翻译的键」。
        """
        needed = ['nav.groupOverview', 'nav.groupOps', 'nav.groupGovernance',
                  'dock.tipDismiss', 'dock.tipNext',
                  'profile.dockLabel', 'profile.dockFloating', 'profile.dockPinned']
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
            '以下语言包缺少底栏这一批用到的键，页面上会直接显示裸键名：\n  '
            + '\n  '.join(missing),
        )


if __name__ == '__main__':
    unittest.main()

class DockDialogVisibilityTest(unittest.TestCase):
    """issue #406：弹窗打开时底栏跟随动画仍在跑。"""

    def test_dock_hidden_while_dialogs_open(self) -> None:
        src = (_ROOT / 'web' / 'components' / 'common' / 'layout' / 'ManagementBar.tsx'
               ).read_text(encoding='utf-8')
        self.assertIn('profileOpen || addOpen', src,
                      '个人信息/快速添加弹窗打开时没有隐藏底栏')
        self.assertIn("{visibility: 'hidden'}", src)

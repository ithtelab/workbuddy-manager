"""危险操作必须二次确认、异常要有兜底、假反馈不能留（P0-3 / P0-4 / P1-5 / P1-6）。

与 test_async_state.py 同一思路：这几条是**结构性**约定，类型系统表达不了，
但可以用源码断言钉住。不钉住的话，一次「顺手把弹窗拆了」的改动就能把 README
明确承诺过的「危险操作一律二次确认」悄悄变回不成立，而所有自动化测试仍是绿的
——用户看到的是「点一下东西就没了，没有任何回旋余地」。

这里刻意不评判文案措辞（那没法自动判定），只锁机械可查的几件事：
1. 删 IP 规则**确实**在确认弹窗里；
2. 全站不再出现原生阻断式弹窗（`window.prompt` / `confirm` / `alert`）；
3. 测试台的假点赞 / 点踩按钮已移除；
4. `error.tsx` 存在，且给出「重试」这条出路；
5. `errText` 在 403 上**先信后端文案**，兜底排在后面。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_WEB = _ROOT / 'web'

_SECURITY = _WEB / 'app' / '(main)' / 'security' / 'page.tsx'
# 设置页的外壳在 `layout.tsx`：批次 4 起 7 个 Tab 变成了 `/settings/<tab>`
# 子路由，而子路由的 page 每次跳转都会重挂载 —— 取数、表单状态与所有危险操作
# 的确认弹窗都必须留在不重挂载的 layout 上（见那个文件顶部的说明）。
# `page.tsx` 现在只剩一句重定向，读它会得到「找不到 settingsApi.removeUser」
# 这种看不出所以然的报错。
_SETTINGS = _WEB / 'app' / '(main)' / 'settings' / 'layout.tsx'
_PLAYGROUND = _WEB / 'app' / '(main)' / 'playground' / 'page.tsx'
_API = _WEB / 'lib' / 'api.ts'
_MAIN_ERROR = _WEB / 'app' / '(main)' / 'error.tsx'
_ROOT_ERROR = _WEB / 'app' / 'error.tsx'


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def _strip_comments(src: str) -> str:
    """去掉注释再断言。

    注释里会**提到**被禁的写法（例如「原来这里是 window.prompt」这种说明），
    不剥掉的话，恰恰是那段解释为什么改掉的注释会让守卫报错。
    """
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


def _inside_confirm_dialog(src: str, pos: int) -> bool:
    """`pos` 处是否位于某个 `<ConfirmDialog …>` 的 JSX 子块里。

    做法：从 pos 往前找**最近**的一次 `<ConfirmDialog` 与 `</ConfirmDialog>`，
    谁更靠后就说明当前在哪一层。不做完整 JSX 解析——这里只需要一个方向性判断，
    而真正要防的回归（拆掉弹窗、恢复成裸 onClick）会让两者都找不到、或让闭合
    标签变成更近的那一个，两种情况都会判 False。
    """
    opened = src.rfind('<ConfirmDialog', 0, pos)
    closed = src.rfind('</ConfirmDialog>', 0, pos)
    return opened > closed


def _enclosing_function(src: str, pos: int) -> str:
    """`pos` 所在函数的函数名（找不到返回空串）。只认 `function name(` 这种写法。"""
    last = None
    for m in re.finditer(r'(?:async\s+)?function\s+(\w+)\s*\(', src[:pos]):
        last = m
    return last.group(1) if last else ''


def _wired_into_confirm(src: str, pos: int) -> bool:
    """调用点在具名函数里、而该函数被某个 `ConfirmDialog` 的 `onConfirm` 引用。

    等价于「在确认弹窗内」，只是形态不同：`#89` 的「删除上游」把调用写成
    `async function remove(item)`，再由 `<ConfirmDialog onConfirm={() => remove(item)}>`
    触发。合并 `#87` 的扫描器时这条被判成「没有二次确认」——是**误报**：弹窗确实在，
    只是调用点不在 JSX 子块里。

    判据刻意收窄：必须**同时**满足「有具名函数包着它」与「该函数名出现在某处
    onConfirm 里」，所以裸 `onClick={() => remove(item)}` 不会因此过关。
    """
    fn = _enclosing_function(src, pos)
    if not fn:
        return False
    # 两种写法都算「接进了确认弹窗」：`onConfirm={() => fn(item)}` 与
    # `onConfirm={fn}`（直接交引用）。判据仍收窄在 onConfirm 上——
    # 裸 `onClick={fn}` 不会因此过关。
    return (
        re.search(r'onConfirm=\{[^}]*\b' + re.escape(fn) + r'\s*\(', src) is not None
        or re.search(r'onConfirm=\{\s*' + re.escape(fn) + r'\s*\}', src) is not None
    )


# 破坏性调用：删掉东西、清空记录、吊销凭据。README 承诺「危险操作一律二次确认」，
# 这份形态清单就是那句话的可执行版本。
_DESTRUCTIVE = re.compile(
    r'Api\.(?:remove|clear|revoke|logsClear|clearCheckinLogs|clearTaskLogs)\w*\(')

# 已知且**有意**不做二次确认的破坏性调用，写成 `相对路径::调用形态`。
# 目前为空——留着是为了让「以后要破例」变成一次显式、可评审的改动，
# 而不是悄悄加个裸 onClick 就绕过去了。
_ALLOWED_WITHOUT_CONFIRM: set[str] = set()


def _destructive_sites() -> list[tuple[str, int, str, bool]]:
    """全站扫一遍破坏性调用：返回 (文件, 行号, 调用形态, 是否在确认弹窗内)。"""
    sites: list[tuple[str, int, str, bool]] = []
    for path in sorted(_WEB.rglob('*.tsx')):
        # node_modules 与 out（构建产物）都不是源码
        if 'node_modules' in path.parts or 'out' in path.parts:
            continue
        src = path.read_text(encoding='utf-8')
        rel = path.relative_to(_ROOT).as_posix()
        for m in _DESTRUCTIVE.finditer(src):
            confirmed = (_inside_confirm_dialog(src, m.start())
                         or _wired_into_confirm(src, m.start()))
            sites.append((rel, src[:m.start()].count('\n') + 1, m.group(0), confirmed))
    return sites


class DangerousActionTest(unittest.TestCase):
    """删除类操作必须经过确认弹窗。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sites = _destructive_sites()

    def test_scan_is_not_vacuous(self) -> None:
        """先确认扫描真的抓到了东西，免得正则失配后测试空转照样绿。"""
        self.assertGreaterEqual(len(self.sites), 10,
                                f'只扫到 {len(self.sites)} 处破坏性调用 —— 正则可能失配')
        self.assertIn('web/app/(main)/security/page.tsx', {s[0] for s in self.sites})

    def test_helper_discriminates(self) -> None:
        """再证明这个判据本身有效，免得它恒真（那样下面的测试就是空转）。"""
        src = _read(_SECURITY)
        pos = src.index('securityApi.removeRule')
        self.assertTrue(_inside_confirm_dialog(src, pos), '删规则应判定为「在弹窗内」')
        # 反向对照：同一文件里随便挑一处与弹窗无关的代码
        other = src.index('securityApi.addRule')
        self.assertFalse(_inside_confirm_dialog(src, other), '新增规则不该被判定为在弹窗内')

    def test_wired_into_confirm_discriminates(self) -> None:
        """新判据同样要有正/反对照（理由同 `test_helper_discriminates`）。

        正例：`UpstreamEndpoints.tsx` 的 `remove` —— 具名函数、被 `onConfirm` 引用；
        反例：同文件的 `probe` —— 由普通按钮触发，不该被判成「在弹窗内」。

        这条判据是合并 #87 与 #89 时补的：扫描器原本只认「调用点写在 JSX 子块里」，
        于是把上游删除那个**真有弹窗**的写法报成了漏网（回归守卫自己先红了）。

        反例原先用的是同文件的 `create`；账号分组把表单抽成公共的
        `UpstreamFormDialog`（账号页与设置页共用），`create` 随之搬去那边 ——
        这里改用同样由普通按钮触发、且仍留在本文件的 `probe`，判据意图不变。
        """
        src = _read(_ROOT / 'web' / 'components' / 'settings' / 'UpstreamEndpoints.tsx')
        pos = src.index('upstreamsApi.remove(')
        self.assertTrue(_wired_into_confirm(src, pos), '删除上游应判定为「在确认弹窗内」')
        other = src.index('upstreamsApi.probe(')
        self.assertFalse(_wired_into_confirm(src, other), '探测上游不该被判定为在弹窗内')

    def test_security_rule_delete_is_confirmed(self) -> None:
        """删 IP 规则必须二次确认。

        改的是**访问控制**：删掉一条 deny 规则等于当场放行该网段，删掉一条
        allow 规则等于当场把该网段关在门外——两者都不该由一次误点决定。
        （同一文件里「清空访问日志」早就有弹窗了，只有这里漏了。）
        """
        src = _read(_SECURITY)
        self.assertIn('securityApi.removeRule', src, '删规则的调用不见了 —— 守卫失去目标')
        for pos in [m.start() for m in re.finditer(r'securityApi\.removeRule', src)]:
            self.assertTrue(
                _inside_confirm_dialog(src, pos),
                f'第 {src[:pos].count(chr(10)) + 1} 行的 securityApi.removeRule 不在 '
                f'ConfirmDialog 里 —— 点一下就直接删了，违反 README 的「一律二次确认」')
        # 光有弹窗还不够：如果按钮上**同时**留着一个裸 onClick，点一下照样立刻删，
        # 弹窗只是白弹。所以再钉一条——调用处附近不许出现 onClick。
        for m in re.finditer(r'securityApi\.removeRule', src):
            near = src[max(0, m.start() - 300): m.start() + 300]
            self.assertNotIn(
                'onClick', near,
                '删规则的调用紧挨着 onClick —— 按钮上还留着直接删除的处理器，'
                '弹窗拦不住它')

    def test_settings_remove_user_is_confirmed(self) -> None:
        """删用户同样是危险操作，作为判据的阳性对照一起钉住。"""
        src = _read(_SETTINGS)
        pos = src.index('settingsApi.removeUser')
        self.assertTrue(_inside_confirm_dialog(src, pos), '删用户应仍在 ConfirmDialog 里')

    def test_all_destructive_calls_are_confirmed(self) -> None:
        """**所有**删除 / 清空 / 吊销类调用都要在确认弹窗里。

        README 承诺的是「危险操作**一律**二次确认」，所以判据不能只盯住本次修的那
        一处——那只能保证「这一处没退回去」，保证不了「下一处新写的也守规矩」。
        这里按调用形态扫全站，逐处要求它在弹窗内。

        实测做这次巡检时全站共 12 处，`security` 删 IP 规则是唯一漏网的——也就是
        说这条守卫如果早就在，P0-4 根本不会出现。
        """
        offenders = [
            f'{rel}:{line} → {call}'
            for rel, line, call, confirmed in self.sites
            if not confirmed and f'{rel}::{call}' not in _ALLOWED_WITHOUT_CONFIRM
        ]
        self.assertEqual(
            offenders, [],
            f'有 {len(offenders)} 处破坏性调用没有二次确认：{offenders[:5]}')


class ConfirmWiringFormTest(unittest.TestCase):
    """`onConfirm` 的两种写法都要被认出来，半途而废的写法（裸 onClick）仍要报。

    直接交引用（`onConfirm={fn}`）与包一层（`onConfirm={() => fn(item)}`）都是
    「接进了确认弹窗」，判据不该只认后者——否则合法的写法会被判成没确认，
    逼着作者去改代码迁就测试。
    """

    def test_bare_reference_is_wired(self) -> None:
        src = ("async function clearResult() {\n"
               "  await Api.clearUpdateStatus();\n"
               "}\n"
               "const A = () => <ConfirmDialog onConfirm={clearResult} />;\n")
        self.assertTrue(_wired_into_confirm(src, src.index('Api.clearUpdateStatus')))

    def test_call_form_is_wired(self) -> None:
        src = ("async function removeOne(item) {\n"
               "  await Api.remove(item);\n"
               "}\n"
               "const B = () => <ConfirmDialog onConfirm={() => removeOne(x)} />;\n")
        self.assertTrue(_wired_into_confirm(src, src.index('Api.remove(')))

    def test_bare_onclick_is_not_enough(self) -> None:
        src = ("async function clearResult() {\n"
               "  await Api.clearUpdateStatus();\n"
               "}\n"
               "const C = () => <Button onClick={clearResult} />;\n")
        self.assertFalse(_wired_into_confirm(src, src.index('Api.clearUpdateStatus')),
                         '裸 onClick 被当成有二次确认了')


class NativeDialogTest(unittest.TestCase):
    """原生阻断式弹窗一律不再使用。"""

    def test_no_native_blocking_dialogs(self) -> None:
        """全站不得再出现 `window.prompt` / `confirm` / `alert`。

        它们不参与多语言、没法校验输入、样式与全站 Dialog 体系割裂，键盘
        无障碍能力也取决于浏览器实现。原先只有设置页的重置密码一处用
        `window.prompt`，已改为 `ResetPasswordDialog`；钉成守卫是因为「图省事
        用一下 prompt」的诱惑一直存在。
        """
        pattern = re.compile(r'window\.(prompt|confirm|alert)\s*\(')
        offenders: list[str] = []
        for path in sorted(_WEB.rglob('*')):
            if path.suffix not in ('.ts', '.tsx'):
                continue
            # node_modules 与 out（构建产物）都不是源码
            if 'node_modules' in path.parts or 'out' in path.parts:
                continue
            src = _strip_comments(path.read_text(encoding='utf-8'))
            for m in pattern.finditer(src):
                line = src[:m.start()].count('\n') + 1
                offenders.append(f'{path.relative_to(_ROOT).as_posix()}:{line} → {m.group(0)}')
        self.assertEqual(offenders, [],
                         f'还有 {len(offenders)} 处原生弹窗：{offenders[:5]}')


class FakeFeedbackTest(unittest.TestCase):
    """假反馈（看起来能用其实没用）不能留在界面上。"""

    def test_playground_has_no_feedback_buttons(self) -> None:
        """测试台不得再有点赞 / 点踩按钮。

        它们原先只弹一句「已记录反馈」，不写任何数据——比没有更糟：用户会以为
        自己真的反馈过了，于是不再另想办法提意见。产品上已决定不做这个功能，
        所以是**移除**而不是补实现。
        """
        src = _read(_PLAYGROUND)
        self.assertIn('CopyButton', src, '测试台源码结构变了 —— 守卫失去目标')
        for icon in ('ThumbsUp', 'ThumbsDown'):
            self.assertNotIn(icon, src, f'{icon} 又回来了 —— 这个按钮不产生任何数据')
        for key in ('playground.goodAnswer', 'playground.badAnswer',
                    'playground.feedbackRecorded', 'playground.feedbackDetail'):
            self.assertNotIn(key, src, f'{key} 被重新引用 —— 假反馈又回来了')


class ErrorBoundaryTest(unittest.TestCase):
    """渲染期异常要有兜底，而不是白屏。"""

    def test_boundaries_exist_and_offer_a_way_out(self) -> None:
        """`(main)/error.tsx` 与 `app/error.tsx` 都要在，且都给「重试」。

        两层都要，是因为 React 的错误边界只能接住**子树**的异常：
        `(main)/error.tsx` 接不住 `(main)/layout.tsx` 自己抛的错，而那里挂着
        底栏与 RealmProvider，是整页白屏最难受的一处；`(auth)/login` 与公开的
        `/claim` 也不在 `(main)` 之下。

        必须给出路：兜底页只有一句「出错了」等于把用户困住——`reset` 至少让
        偶发失败（例如一次取数竞态）能自己恢复。
        """
        for path in (_MAIN_ERROR, _ROOT_ERROR):
            rel = path.relative_to(_ROOT).as_posix()
            self.assertTrue(path.is_file(), f'{rel} 不存在 —— 渲染期异常仍会白屏')
            src = path.read_text(encoding='utf-8')
            self.assertIn("'use client'", src, f'{rel} 必须是客户端组件（错误边界靠 React）')
            self.assertIn('reset', src, f'{rel} 没接 reset —— 用户没有出路')
            self.assertIn('onRetry={reset}', src, f'{rel} 没把 reset 接到「重试」上')


class ForbiddenMessageTest(unittest.TestCase):
    """403 的文案优先级：后端原文 > 状态码兜底。"""

    def test_backend_detail_wins_over_status_fallback(self) -> None:
        """`errText` 必须先信后端文案，403 兜底只能排在后面。

        顺序反了就是真 bug：本项目 403 有四种含义，其中「本机导入开关没开」
        「调用方不是面板所在机器」两条原文就是**可照做的操作说明**，先按状态码
        改写成「权限不足」会把用户唯一能照着做的那句话抹掉。
        """
        src = _read(_API)
        # 剥注释后定位：注释里同样会提到这些写法
        code = _strip_comments(src)
        body = code[code.index('export function errText'):]
        body = body[:body.index('\n}')]
        detail_first = body.index('if (raw) return tp(raw)')
        fallback = body.index('status === 403')
        self.assertLess(detail_first, fallback,
                        '403 兜底排到了后端文案前面 —— 本机导入那两条操作说明会被抹掉')
        self.assertIn("t('error.forbidden')", body, '403 兜底没有用可翻译的键')


if __name__ == '__main__':
    unittest.main()

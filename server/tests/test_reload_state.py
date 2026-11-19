"""上游重载状态反馈（批次 5）—— 行为测试借 Node 执行，结构不变式在源码上断言。

为什么值得有：这一批把 `GET /api/upstream/reload-state` 接上界面（此前封装写好了
却零调用），让「正在应用配置」从一句**没有兑现的承诺**变成可核对的状态。做错了都
不会报错，只是界面继续说着不准确的话：

  · 判定拿上一次的结论当这一次的 → 刚修好配置就报「重载失败」（或反过来先说
    「已生效」）——由 `web/lib/reload-state.test.mjs` 盖住；
  · 组件自己又写一套判定 → 两份逻辑迟早不一致，而两处都不报错；
  · 提示做成 toast → 几秒后自己消失，「需要你去宿主机处理」的那条就没了；
  · 只在一个 return 分支里渲染 → 「配置读不到」时看不到重载失败，而配置读不到
    往往**正是**重载失败的原因；
  · 失败文案说成「保存失败」→ 配置其实已经写进去了，用户会往错的方向查。

**本模块不复述清单**：相位名从 `reload-state.ts` 解析，文案键从组件源码里扫出来，
不另抄一份副本。
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
_PURE = _WEB / 'lib' / 'reload-state.ts'
_SCRIPT = _WEB / 'lib' / 'reload-state.test.mjs'
_NOTICE = _WEB / 'components' / 'common' / 'settings' / 'UpstreamReloadNotice.tsx'
_SHELL = _WEB / 'app' / '(main)' / 'settings' / 'layout.tsx'
_LOCALES = _WEB / 'lib' / 'i18n' / 'locales'
_VERIFY = _ROOT / 'dev' / 'verify_state_honesty.mjs'

_NODE = shutil.which('node')
_MIN_MAJOR = 22

# 组件里扫出的文案键（`t('settings.apply*')`）
_KEY_RE = re.compile(r"t\('(settings\.apply[A-Za-z]+)'\)")

# 必须真的渲染出来的相位：验收脚本按 `data-phase` 断言，少一个就有一档在界面上
# 根本不存在（判定算对了也没用）
_VISIBLE_PHASES = ('applying', 'ok', 'failed')


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


def _locale(name: str) -> dict:
    return json.loads((_LOCALES / f'{name}.json').read_text(encoding='utf-8'))


class ReloadStateBehaviourTest(unittest.TestCase):
    """跑 Node 侧的行为测试：判定的真实实现。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_reload_state(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')

        def run_once() -> subprocess.CompletedProcess:
            return subprocess.run(
                [_NODE, '--experimental-strip-types', str(_SCRIPT)],
                capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
            )

        proc = run_once()
        # Node 偶发进程级崩溃（整套测试并跑时遇到过 0xC0000409）：只对异常退出码
        # 重试一次；rc=1（脚本自己判失败）不重试，免得守卫变成「跑两遍总能绿」。
        if proc.returncode not in (0, 1):
            proc = run_once()
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'重载状态判定不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class ReloadNoticeInvariantTest(unittest.TestCase):

    def setUp(self) -> None:
        self.notice = _code(_NOTICE)
        self.shell = _code(_SHELL)
        # 解析失效 → 下面的断言会「一条都没查到」而静默通过。先钉住形状。
        self.keys = sorted(set(_KEY_RE.findall(_NOTICE.read_text(encoding='utf-8'))))
        self.assertGreaterEqual(
            len(self.keys), 4,
            f'从组件里只扫到 {len(self.keys)} 个文案键（{self.keys}）—— '
            '`t(\'settings.apply…\')` 的写法变了？解析失效会让文案断言空转。',
        )

    def test_module_stays_runnable_without_node_modules(self) -> None:
        """`reload-state.ts` 不得有运行时 import。

        它是上面那条 Node 测试能跑起来的前提：`.mjs` 直接
        `import './reload-state.ts'`，而 Node 的 ESM 解析**要求带扩展名**，
        仓库的 app 代码却一律不写扩展名。一旦 import 了别的模块的**值**，
        解析就会失败——症状是「Cannot find module」，看起来像路径写错。
        """
        offenders = [
            line.strip() for line in _PURE.read_text(encoding='utf-8').splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(
            offenders, [],
            'reload-state.ts 出现了运行时 import：\n  ' + '\n  '.join(offenders)
            + '\n本模块被 web/lib/reload-state.test.mjs 直接 import。',
        )

    def test_phase_names_are_not_restated_in_the_component(self) -> None:
        """判定只能来自 `reload-state.ts`，组件不得自己再写一套。

        组件里出现 `running` / `pending` / `last_ok` 这类**判定用**的字段，就说明
        它在自己判断相位——于是同一件事有两个说法，改一处漏一处都不报错。

        `last_at` 是例外：组件要拿它当基线传给 `reloadPhase`（那是它的入参，
        不是判定）。所以这里只禁判定字段，并要求真的把基线传了进去。
        """
        for field in ('running', 'pending', 'last_ok'):
            self.assertNotIn(
                field, self.notice,
                f'UpstreamReloadNotice 里出现了 `{field}` —— 它在自己判定相位。'
                '判定必须只来自 @/lib/reload-state（那里有行为测试）。',
            )
        self.assertIn(
            'reloadPhase', self.notice,
            'UpstreamReloadNotice 没有用 reloadPhase，那它靠什么决定显示哪一档？',
        )
        self.assertIn(
            'reloadPollDelay', self.notice,
            'UpstreamReloadNotice 没有用 reloadPollDelay —— 轮询节奏会与判定脱钩'
            '（「应用中」跟不紧，或者闲时也在猛发请求）。',
        )
        self.assertRegex(
            self.notice, r'reloadPhase\([^)]*,[^)]*\)',
            'reloadPhase 只传了一个参数 —— 缺了基线，「这一次有没有落地」就判不出来'
            '（会把上一次的结论安到这一次头上）。',
        )

    def test_reload_state_endpoint_is_actually_called(self) -> None:
        """这一批的**全部意义**就在这里：端点必须真的被调用。

        此前 `settingsApi.reloadState` 写好了却零调用，界面只说「正在应用配置」
        然后没有下文——重载失败也照样一片祥和。
        """
        self.assertIn(
            'settingsApi.reloadState()', self.notice,
            'UpstreamReloadNotice 没有调用 settingsApi.reloadState()。'
            '那「正在应用配置」就还是一句无法核对的承诺。',
        )

    def test_notice_is_not_a_toast(self) -> None:
        """必须常驻，不能用 toast。

        与 `LoadError` 同一个理由：toast 几秒后自己消失。「上游重载失败」是
        **需要人去宿主机处理**的事，用户切回来时必须还在。
        """
        self.assertNotIn(
            'notify', self.notice,
            'UpstreamReloadNotice 用了 notify（toast）—— 它会自己消失，'
            '而「重载失败」恰恰是必须留下来的那条。',
        )

    def test_every_visible_phase_is_rendered(self) -> None:
        """三个可见相位都要有对应的渲染分支（验收脚本按 `data-phase` 断言）。

        少一档的表现是「判定算对了，但那一档在界面上不存在」——比如算出了
        `failed` 却没有分支渲染它，于是失败时什么都不显示。
        """
        missing = [p for p in _VISIBLE_PHASES if f'data-phase="{p}"' not in self.notice]
        self.assertEqual(
            missing, [],
            'UpstreamReloadNotice 没有渲染这些相位：' + '、'.join(missing)
            + '（验收脚本按 data-phase 断言，缺一档就有一档在界面上不存在）',
        )

    def test_notice_is_mounted_exactly_once_and_outside_the_early_return(self) -> None:
        """在设置页外壳里**只挂一次**，且挂在早返回之前。

        这是这一批最容易犯的错（与批次 3 的 `{children}`、批次 4 的
        `PageSectionTabs` 同类）：两个 return 分支里各写一遍，漏一处就是
        「配置读不到时看不到重载失败」——而配置读不到往往**正是**重载失败的原因。
        放进 `header` 常量由构造保证，所以这里钉住「只出现一次」+「在早返回之前」。
        """
        # 两个索引都取自**同一份**剥过注释的文本：混用原始源码与剥过注释的源码
        # 去比下标，会因为注释被删掉而错位——断言看着通过，其实比的是两个位置。
        code = _code(_SHELL)
        uses = code.count('<UpstreamReloadNotice')
        self.assertEqual(
            uses, 1,
            f'设置页外壳里出现了 {uses} 次 <UpstreamReloadNotice />。'
            '它必须只写一处（放进 `header` 常量），否则两个 return 分支会漂移。',
        )
        early = code.find('if (!cfg)')
        self.assertGreater(early, 0, '找不到 `if (!cfg)` 早返回 —— 锚点过期了')
        self.assertLess(
            code.find('<UpstreamReloadNotice'), early,
            'UpstreamReloadNotice 挂在了 `if (!cfg)` 早返回之后 —— '
            '配置读不到时它就消失了，而那正是最需要看到它的时刻。',
        )

    def test_polling_pauses_in_a_hidden_tab(self) -> None:
        """标签页在后台时不再发请求（与 use-heartbeat 同一取舍）。

        少了它，一个被切走的设置页会一直按 1.5 秒的节奏问下去——重载是分钟级的
        事，回到前台再对齐完全够。
        """
        self.assertIn(
            'document.hidden', self.notice,
            'UpstreamReloadNotice 没有判断 document.hidden —— 后台标签页会一直轮询。',
        )

    def test_failure_copy_does_not_say_the_save_failed(self) -> None:
        """失败文案不能说成「保存失败」。

        配置**已经写进去了**，失败的是让它生效的那一步：说成「保存失败」会让人
        回头重填表单，而正确的下一步是在宿主机重启容器。同时必须给出那条可照做的
        命令——只说「失败了」等于把问题丢回给用户。
        """
        zh = _locale('zh-CN')['settings']
        failed = zh['applyFailed']
        hint = zh['applyFailedHint']
        for bad in ('保存失败', '未保存', '没有保存'):
            self.assertNotIn(
                bad, failed,
                f'失败文案里出现了「{bad}」——配置其实已经写入，'
                '说成保存失败会把人引向错的方向。',
            )
        self.assertIn(
            'docker compose restart', hint,
            '失败提示没有给出可照做的下一步（宿主机重启命令）——'
            '只说「失败了」等于把问题丢回给用户。',
        )

    def test_ok_copy_does_not_claim_it_is_still_working(self) -> None:
        """「已生效」不能同时又暗示还在进行中（两句话会互相打架）。"""
        zh = _locale('zh-CN')['settings']
        for bad in ('正在', '请稍候', '等待'):
            self.assertNotIn(
                bad, zh['applyOk'],
                f'「已生效」文案里出现了「{bad}」—— 既说完成又说进行中，'
                '用户不知道到底该等还是该走。',
            )
        self.assertNotIn(
            '正在', zh['applyFailed'],
            '「重载失败」文案里出现了「正在」—— 失败是已发生的事，不是进行中。',
        )

    def test_acceptance_script_covers_the_notice(self) -> None:
        """浏览器验收脚本必须真的走一遍四档相位。

        没有这一段的话，「重载失败时界面会说」全是**没被验证过**的宣称：单元测试
        只证明判定函数算得对，证明不了它接在了设置页上、也证明不了失败时那一句
        真的显示出来了。四档里只有一档该有提示，所以脚本必须**成对**断言
        （闲时没有 + 该出现的那档出现了），并且核对失败文案里那两条硬信息
        （可照做的命令、上游原样输出）。
        """
        self.assertTrue(_VERIFY.is_file(), f'找不到验收脚本: {_VERIFY}')
        code = _VERIFY.read_text(encoding='utf-8')
        for needed, why in (
            ('data-slot=reload-notice', '没有找过这条提示'),
            ('data-phase=applying', '没有断言「重载中」那一档'),
            ('data-phase=ok', '没有断言「已生效」那一档'),
            ('data-phase=failed', '没有断言「重载失败」那一档'),
            ('docker compose restart', '没有核对失败提示里那条可照做的命令'),
            ('保存失败', '没有核对失败文案**不**说成「保存失败」'),
        ):
            self.assertIn(
                needed, code,
                f'验收脚本里找不到 `{needed}`（{why}）——'
                '「重载失败时界面会如实说」就成了没被验证过的宣称。',
            )

    def test_all_copy_keys_exist_in_every_locale(self) -> None:
        """组件用到的每个键都必须在**每个**语言包里存在。

        `test_web_i18n` 的 `test_all_literal_keys_exist` 只回查**源语言**字典；
        某个目标语言缺了这一条，页面上会直接显示裸键名（`settings.applyOk`）。
        这里逐个语言包查，且键集**从组件源码里扫**，不另抄一份。
        """
        missing: list[str] = []
        for name in ('zh-CN', 'zh-TW', 'en', 'ja', 'ko'):
            table = _locale(name).get('settings')
            self.assertIsInstance(table, dict, f'{name}.json 没有 settings 段')
            for key in self.keys:
                value = table.get(key.partition('.')[2])
                if not isinstance(value, str) or not value.strip():
                    missing.append(f'{name}: {key}')
        self.assertEqual(
            missing, [],
            '以下语言包缺少重载状态文案，页面上会显示裸键名：\n  ' + '\n  '.join(missing),
        )


if __name__ == '__main__':
    unittest.main()

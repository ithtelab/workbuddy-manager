"""前端轮询必须处理「标签页切回」——否则登录成功了界面也不更新（issue #34）。

现场（issue #34 的追加反馈）：用户点弹出的授权链接、在浏览器里登录成功，切回
管理端页面时**弹窗仍停在「等待…」**，看起来像没生效。

根因是浏览器对**后台标签页的定时器节流**：切走之后 `setInterval` 被压到约
1 次/分钟。而添加账号弹窗的轮询当时用的是裸 `setInterval` —— 用户切回来时，
下一次 tick 可能还要等最久一整分钟，那一分钟里界面毫无变化，表现就是「卡住」。
再叠加 `STATE_TTL`（15 分钟）的计时，等待感尤其糟。

这个项目里 `lib/use-heartbeat.ts` 早就处理了这件事（隐藏时跳过、`visibilitychange`
时立即补一次），弹窗那处当初漏了。本文件把「轮询类代码必须处理可见性变化」
钉成可静态检查的规矩 —— 这类**可以静态判定**的遗漏，不值得靠人去记。

判据是机械的，不评判实现细节：
  1. 含有 `setInterval` 轮询的组件，必须同时出现 `visibilitychange`；
  2. 必须判断 `document.hidden`（否则切走那一下也会白发请求）。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_WEB = _ROOT / 'web'


def _tsx_files() -> list[Path]:
    out = []
    for p in _WEB.rglob('*.tsx'):
        parts = set(p.parts)
        if 'node_modules' in parts or '.next' in parts:
            continue
        out.append(p)
    for p in _WEB.rglob('*.ts'):
        parts = set(p.parts)
        if 'node_modules' in parts or '.next' in parts:
            continue
        out.append(p)
    return sorted(out)


class PollingVisibilityTest(unittest.TestCase):
    def test_scan_found_files(self) -> None:
        """先确认扫到了东西，否则下面几条会在空集上「通过」。"""
        files = _tsx_files()
        self.assertGreater(len(files), 20, f'只扫到 {len(files)} 个前端文件，扫描逻辑可能失效')

    def test_dialog_poll_handles_visibility(self) -> None:
        """添加账号弹窗的轮询必须处理可见性变化。

        这条直接对应 issue #34：点链接登录后切回页面，界面要**立即**反映结果。
        """
        p = _WEB / 'components' / 'common' / 'accounts' / 'AddAccountDialog.tsx'
        self.assertTrue(p.is_file(), f'找不到 {p}')
        src = p.read_text(encoding='utf-8')
        self.assertIn('setInterval', src, '前提变了：弹窗不再用 setInterval 轮询')
        self.assertIn('visibilitychange', src,
                      '弹窗轮询没有监听 visibilitychange —— 用户切回标签页后界面不会立即更新'
                      '（点链接登录后会表现为「卡在等待」）')
        self.assertIn('document.hidden', src,
                      '没有判断 document.hidden —— 切走那一下也会白发一次请求')

    def test_poll_listener_is_removed_on_stop(self) -> None:
        """监听器必须随轮询一起摘掉。

        否则弹窗关闭后，每次切标签页都会调用一个已作废的 tick：轻则白发请求，
        重则对着已卸载的组件 setState。

        加与摘必须**出自同一份列表**（`wakeEvents()`）：早先是各写一遍事件名，
        新增唤醒事件时极易只加不摘 —— 那就成了泄漏。现在两侧都遍历同一个列表，
        这条测试钉住这个结构。
        """
        p = _WEB / 'components' / 'common' / 'accounts' / 'AddAccountDialog.tsx'
        src = p.read_text(encoding='utf-8')
        self.assertRegex(
            src, r'wakeEvents\(\)\.forEach\([\s\S]{0,200}?addEventListener',
            '添加监听没有走 wakeEvents() —— 摘除那一侧就不会覆盖它',
        )
        self.assertRegex(
            src, r'wakeEvents\(\)\.forEach\([\s\S]{0,200}?removeEventListener',
            '没有移除轮询监听器（弹窗关掉后仍会触发已作废的 tick）',
        )

    def test_occluded_window_wakes_on_interaction(self) -> None:
        """被遮挡的窗口不触发 visibilitychange，必须靠交互事件唤醒。

        现场：Chrome 对**被遮挡**（不是「隐藏」）的窗口把定时器压到约 1 次/分钟，
        而 `document.hidden` 仍是 false、`visibilitychange` 一次都不触发 ——
        只挂可见性的兜底完全失效。用户切回来点一下、敲一下或让窗口获得焦点时
        必须立即补一次轮询，否则界面要干等一分钟（「加账号仍要等 20-60 秒」）。
        """
        p = _WEB / 'components' / 'common' / 'accounts' / 'AddAccountDialog.tsx'
        src = p.read_text(encoding='utf-8')
        m = re.search(r'function wakeEvents\(\)[\s\S]{0,900}?\n}', src)
        self.assertIsNotNone(m, '找不到 wakeEvents() —— 唤醒事件的唯一列表')
        body = m.group(0)
        events = set(re.findall(r"\[\s*(?:document|window)\s*,\s*'([a-z]+)'\s*\]", body))
        self.assertIn('visibilitychange', events, '丢了可见性兜底')
        for ev in ('focus', 'pointerdown', 'keydown'):
            self.assertIn(
                ev, events,
                f'唤醒列表里没有 {ev}：被遮挡窗口的定时器被节流且不触发 visibilitychange 时，'
                f'用户回来操作页面不会立即刷新界面',
            )

    def test_every_interval_poller_handles_visibility(self) -> None:
        """任何前端**轮询**（setInterval + 网络请求）都要处理可见性。

        只认「同一个文件里既有 setInterval 又有 await/请求」的形态 —— 纯动画、
        纯倒计时之类不需要（它们本来也不是"拉数据"，切回来时值会自己算出来）。
        """
        offenders: list[str] = []
        for p in _tsx_files():
            src = p.read_text(encoding='utf-8')
            if 'setInterval' not in src:
                continue
            # 只挑真正在拉数据的：间隔回调里出现请求调用
            looks_like_polling = bool(re.search(
                r'setInterval\([\s\S]{0,600}?(await\s+\w*[Aa]pi\.|\bawait\s+fetch\(|\bget<|\bpost<)',
                src))
            if not looks_like_polling:
                continue
            if 'visibilitychange' not in src:
                offenders.append(str(p.relative_to(_ROOT)))
        self.assertEqual(
            offenders, [],
            '这些文件在轮询数据但没有处理 visibilitychange —— '
            '用户切回标签页后要等一个完整间隔才更新（后台还会被节流到约 1 次/分钟）：'
            + '\n  ' + '\n  '.join(offenders),
        )

    def test_no_listener_escapes_the_wake_list(self) -> None:
        """这个文件里**只有** wakeEvents() 那两处监听器（一加一摘）。

        绕过列表的监听器不会被 stopPoll 摘掉，弹窗关掉后仍会触发；这条测试的
        失败信息就是给后来者看的说明：把事件加进 wakeEvents()。
        """
        p = _WEB / 'components' / 'common' / 'accounts' / 'AddAccountDialog.tsx'
        src = p.read_text(encoding='utf-8')
        self.assertEqual(
            (src.count('addEventListener('), src.count('removeEventListener(')),
            (1, 1),
            '弹窗里的监听器数量不是「一加一摘」—— 多出来的那些不会被 wakeEvents() '
            '统一摘除（弹窗关掉后仍会触发已作废的 tick）；请把事件加进 wakeEvents() 列表',
        )

    def test_heartbeat_helper_still_documents_the_pattern(self) -> None:
        """既有的 useHeartbeat 是这套模式的正本，别把它改坏。

        它同时是「为什么需要判断 document.hidden」的**可执行说明**：
        后来者写轮询时照抄它就行。
        """
        p = _WEB / 'lib' / 'use-heartbeat.ts'
        src = p.read_text(encoding='utf-8')
        self.assertIn('document.hidden', src)
        self.assertIn('visibilitychange', src)
        self.assertIn('setInterval', src)


if __name__ == '__main__':
    unittest.main()

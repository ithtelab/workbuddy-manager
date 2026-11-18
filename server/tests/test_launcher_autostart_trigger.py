"""启动器「开机自启时同时启动项目」的触发点守卫（issue #449）。

## 背景

静默启动走 `SetVisibleCore` 拦下首次显示。**实测（最小 WinForms 程序）**：拦下
之后 `Load` 与 `Shown` **都不会触发** —— 两者都由 `SetVisibleCore(true)` 内部驱动。

因此任何「窗口就绪后启动项目」的代码都不能挂在这两个事件上，否则表现为
「托盘图标在、项目没起，必须手动打开一次控制窗口」——这正是 issue #449。

正确触发点是 `HandleCreated`（`CreateHandle()` 之后，静默路径同样走到）。

## 这个测试防什么

第一版修复就是把 `Shown` 改成了 `Load`：编译通过、语义看起来完全合理，但**实测
仍然不触发**。所以这里不看代码「像不像对的」，只钉住「必须是 HandleCreated」。

C# 用正则做静态检查（项目没有 .NET 单测框架，启动器靠 `csc` 编译 + 这个守卫）。
"""
from __future__ import annotations

import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MAIN_FORM = _ROOT / 'windows' / 'launcher' / 'src' / 'MainForm.cs'


class AutostartProjectTriggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.src = _MAIN_FORM.read_text(encoding='utf-8-sig')

    def test_source_exists(self) -> None:
        self.assertTrue(self.src, '找不到 MainForm.cs')
        self.assertIn('_autoStartOnBoot', self.src,
                      '自动启动的守卫字段不见了 —— 本测试需要跟着改')

    def test_autostart_is_not_on_shown_or_load(self) -> None:
        """自动启动不能挂在 Shown / Load：静默模式下两者都不触发（实测）。"""
        # 抓 `Shown +=` / `Load +=` 的事件体，确认里面没有 StartProject
        for event in ('Shown', 'Load'):
            for m in re.finditer(rf'{event}\s*\+=', self.src):
                # 取该事件处理器到下一个 `};` 的片段
                end = self.src.find('};', m.end())
                body = self.src[m.end(): end if end > 0 else m.end() + 400]
                self.assertNotIn(
                    'StartProject', body,
                    f'{event} 事件里不能启动项目 —— 静默启动下它不会触发（issue #449）')

    def test_autostart_is_on_handle_created(self) -> None:
        """必须是 HandleCreated —— 静默路径下唯一可靠的「窗口已就绪」信号。"""
        self.assertRegex(
            self.src,
            r'HandleCreated\s*\+=',
            '没有 HandleCreated 注册 —— 开机自启时项目不会被启动（issue #449）')
        # 且该**注册处**（`HandleCreated +=`，不是注释里提到的那次）里确实调用了
        # StartProject(true)（quiet）
        m = re.search(r'HandleCreated\s*\+=', self.src)
        body = self.src[m.end(): m.end() + 600]
        self.assertIn('StartProject(true)', body,
                      'HandleCreated 里应当调用 StartProject(true)（quiet=true）')

    def test_guard_is_only_set_in_hidden_path(self) -> None:
        """`_autoStartOnBoot` 只在静默分支赋值：手动启动不该自动拉项目。"""
        assigns = [m.start() for m in re.finditer(r'_autoStartOnBoot\s*=\s*string', self.src)]
        self.assertEqual(len(assigns), 1, '自动启动的赋值点应当只有一处')
        # 赋值点之前最近的 `if (_startHidden)` 应当存在
        before = self.src[: assigns[0]]
        self.assertIn('if (_startHidden)', before,
                      '自动启动只应在 _startHidden 分支里启用')

    def test_comment_records_the_measured_fact(self) -> None:
        """把「Load/Shown 都不触发」这个实测结论留在代码旁 —— 否则下一个人会再试一次。"""
        self.assertIn('Load', self.src)
        self.assertIn('Shown', self.src)
        self.assertRegex(self.src, r'都不会触发|实测',
                         '注释里应写明这是实测结论，而不是推断')


if __name__ == '__main__':
    unittest.main()

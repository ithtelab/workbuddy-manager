"""前端可访问性护栏：Select / Switch 必须有可读名称。

这两个控件都是 Radix 的 div/button，**没有原生 label 关联**：只放一个可见的
`<Label>` 而不给控件 `aria-label`，读屏软件读到的就是「按钮」，用户不知道
自己在调什么。审计（2026-10-18）时全站扫出 26 个没有名称的下拉与 14 个开关，
逐个补齐后加这条用例，防止新页面再把它们加回来。

只看项目自己的页面与业务组件：`components/ui`、`components/animate-ui` 是
第三方组件库的实现（Switch/Select 的根节点就在那里），不参与检查。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2] / 'web'
_SKIP_DIRS = ('ui', 'animate-ui')


def _files():
    for path in sorted(_ROOT.rglob('*.tsx')):
        parts = set(path.relative_to(_ROOT).parts)
        if parts & set(_SKIP_DIRS):
            continue
        yield path


def _tags(text: str, name: str):
    """返回每个 `<name ...>` 开标签的文本（支持跨行，忽略字符串/花括号里的 >）。"""
    start = 0
    needle = '<' + name
    while True:
        i = text.find(needle, start)
        if i < 0:
            return
        j = i + len(needle)
        if j < len(text) and text[j] not in ' \t\r\n/>':
            start = j
            continue
        depth = 0
        quote = ''
        while j < len(text):
            ch = text[j]
            if quote:
                if ch == quote and text[j - 1] != '\\':
                    quote = ''
            elif ch in ('"', "'"):
                quote = ch
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            elif ch == '>' and depth == 0:
                yield text[i:j + 1]
                start = j + 1
                break
            j += 1
        else:
            return


class ControlLabelTest(unittest.TestCase):
    def test_select_triggers_have_accessible_names(self) -> None:
        missing = []
        for path in _files():
            text = path.read_text(encoding='utf-8')
            for tag in _tags(text, 'SelectTrigger'):
                if 'aria-label' not in tag and 'aria-labelledby' not in tag:
                    missing.append(f'{path.relative_to(_ROOT)}: {" ".join(tag.split())[:120]}')
        self.assertFalse(missing, '以下下拉没有可读名称：\n' + '\n'.join(missing))

    def test_switches_have_accessible_names(self) -> None:
        missing = []
        for path in _files():
            text = path.read_text(encoding='utf-8')
            for tag in _tags(text, 'Switch'):
                if 'aria-label' in tag or 'aria-labelledby' in tag:
                    continue
                # id= 只有在页面里存在对应的 htmlFor= 时才算有名称。
                m = re.search(r'\bid="([^"]+)"', tag)
                if m and (f'htmlFor="{m.group(1)}"' in text
                          or f"htmlFor='{m.group(1)}'" in text):
                    continue
                missing.append(f'{path.relative_to(_ROOT)}: {" ".join(tag.split())[:120]}')
        self.assertFalse(missing, '以下开关没有可读名称：\n' + '\n'.join(missing))


if __name__ == '__main__':
    unittest.main()

"""抓包页（/traces）的结构守卫与前端逻辑测试。

页面上的展示逻辑（格式化、上一轮、差异判定）由 `web/lib/tap.test.mjs` 跑真实
实现来盖；这里只锁**接线**：加一个页面要同时改四处（底栏 / 二级导航 / 命令
面板 / 多语言字典），漏一处的表现都是「静默缺一块」——Tab 不出现、面板搜不到、
英文界面显示中文键名。这些都值得在构建前失败。

顺带钉住一条本功能专属的约束：抓包页的数据一律走 `lib/api` 的 `tapApi`，
不在页面里裸 `fetch`。裸 fetch 会绕开鉴权失效跳转、错误文案与 `BASE_PATH`，
子路径部署下还会打到域名根。
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
_PAGE = _MAIN / 'traces' / 'page.tsx'
_SCRIPT = _WEB / 'lib' / 'tap.test.mjs'
_PURE = _WEB / 'lib' / 'tap.ts'
_SECTION = _WEB / 'lib' / 'section-nav.ts'
_PALETTE = _WEB / 'lib' / 'command-palette.ts'
_BAR = _WEB / 'components' / 'common' / 'layout' / 'ManagementBar.tsx'
_LOCALES = _WEB / 'lib' / 'i18n' / 'locales'

_NODE = shutil.which('node')
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
    """读源码并剥掉注释（注释里正解释着「不要这么写」，搜全文会误判）。"""
    src = path.read_text(encoding='utf-8')
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    src = re.sub(r'\{/\*.*?\*/\}', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


class TapPageLogicTest(unittest.TestCase):
    """跑 Node 侧的纯函数测试。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_tap_helpers(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')
        proc = subprocess.run(
            [_NODE, '--experimental-strip-types', str(_SCRIPT)],
            capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
        )
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'抓包页展示逻辑不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')

    def test_pure_module_imports_no_runtime_values(self) -> None:
        """`tap.ts` 只能 import type：它要被 Node 直接 import（同 section-nav）。"""
        offenders = [
            line.strip() for line in _code(_PURE).splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(offenders, [], 'tap.ts 出现了运行时 import：\n  '
                         + '\n  '.join(offenders))


class TapPageWiringTest(unittest.TestCase):
    """页面接线：四处清单 + 字典。"""

    def test_page_exists_and_uses_the_api_client(self) -> None:
        self.assertTrue(_PAGE.is_file(), '缺少 /traces 页面')
        code = _code(_PAGE)
        self.assertIn('tapApi', code)
        self.assertNotIn('fetch(', code,
                         '抓包页出现了裸 fetch：会绕开鉴权失效跳转与 basePath')

    def test_traces_is_a_top_level_destination(self) -> None:
        """「轨迹」与日志、统计同级：底栏自己的入口，不是日志页的二级导航。

        这条守的是**层级**：并进二级导航虽然也能点进去，但「扫一眼谁用了多少」
        与「逐轮看这一跑发了什么」是两个动作，藏在别人页签下等于让第二个动作
        永远少一次曝光。所以这里要求它出现在 dockItems 里，且**不**出现在
        任何一节的二级导航清单里（两处都有会让同一个页面有两个入口）。
        """
        code = _code(_BAR)
        start = code.index('const dockItems = [')
        end = code.index('\n  ];', start)
        hrefs = re.findall(r"href:\s*'([^']+)'", code[start:end])
        self.assertIn('/traces', hrefs, f'底栏没有「轨迹」入口：{hrefs}')
        section = _SECTION.read_text(encoding='utf-8')
        self.assertNotIn("href: '/traces'", section,
                         '/traces 同时又挂在二级导航里 —— 同一个页面两个入口')

    def test_command_palette_lists_traces(self) -> None:
        self.assertIn("href: '/traces'", _PALETTE.read_text(encoding='utf-8'))

    def test_every_locale_has_the_trace_block(self) -> None:
        base: set[str] | None = None
        for locale in ('zh-CN', 'zh-TW', 'en', 'ja', 'ko'):
            data = json.loads((_LOCALES / f'{locale}.json').read_text(encoding='utf-8'))
            self.assertIn('trace', data, f'{locale} 缺少 trace 段')
            self.assertIn('traces', data['nav'], f'{locale} 缺少 nav.traces')
            # 拍平后逐层比：嵌套的 tab.* 也要一致
            keys = set()

            def walk(obj: dict, prefix: str) -> None:
                for key, value in obj.items():
                    path = f'{prefix}.{key}' if prefix else key
                    if isinstance(value, dict):
                        walk(value, path)
                    else:
                        keys.add(path)

            walk(data['trace'], '')
            if base is None:
                base = keys
            else:
                self.assertEqual(sorted(keys ^ base), [],
                                 f'{locale} 的 trace 键与 zh-CN 不一致')


if __name__ == '__main__':
    unittest.main()

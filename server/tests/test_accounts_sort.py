"""账号页排序偏好的行为测试（前端逻辑，借 Node 执行）。

跑的是 `web/lib/accounts-sort.test.mjs`，它直接 import `.ts` 源码（Node ≥ 22.6
的 type stripping），因此测的是**真实实现**，不是复制一份逻辑。没有 Node
（或版本太旧）时**跳过**而不是失败——与 test_display_prefs.py 同一约定。

另有一条**源码级断言**：display-prefs.ts 的穷举哨兵是 SortKey 的手工复制
（display-prefs.ts 被 node 测试直接 import，只能 import type），两边会漂移——
排序下拉新增一档而哨兵没跟上时，那一档永远记不住（刷新回默认）。这里把两边的
字面量都抠出来做**双向集合等值**：SortKey 多一档、哨兵多一档，都会失败。

抠字面量前先**剥掉注释**（先例见 test_display_prefs.py）：SortKey 的每个成员
都带 JSDoc 注释，注释里的引号小写词会被当成档位（误报）、注释里的 ASCII 分号
会把非贪婪匹配截断在半途，让之后的档位失去检查（漏报）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'accounts-sort.test.mjs'
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


class AccountsSortPrefsTest(unittest.TestCase):
    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_accounts_sort_prefs(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')
        proc = subprocess.run(
            [_NODE, '--experimental-strip-types', str(_SCRIPT)],
            capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
        )
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'排序偏好的行为不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')

    def test_sort_keys_match_sentinel(self) -> None:
        """SortKey 的档位集合必须与 display-prefs.ts 的穷举哨兵双向等值。

        哨兵 Record<SortKey, true> 是 SortKey 的手工复制（display-prefs.ts 只能
        import type）：SortKey 新增档位而哨兵漏写 → 那一档的偏好永远回「默认」；
        哨兵多写 → 拼错的档位被当成合法值。两个方向都不该静默通过。
        """
        lib = _ROOT / 'web' / 'lib'
        account_list = (lib / 'account-list.ts').read_text(encoding='utf-8')
        display_prefs = (lib / 'display-prefs.ts').read_text(encoding='utf-8')

        m = re.search(r'export type SortKey =(.*?);', account_list, flags=re.S)
        self.assertIsNotNone(m, 'account-list.ts 里找不到 SortKey 定义')
        keys = set(re.findall(r"'([a-z]+)'", m.group(1)))
        self.assertTrue(keys, 'SortKey 里没有解析出任何字面量')

        s = re.search(r'ACCOUNTS_SORT_SENTINEL: Record<SortKey, true> = \{(.*?)\}',
                      display_prefs, flags=re.S)
        self.assertIsNotNone(s, 'display-prefs.ts 里找不到穷举哨兵'
                                'ACCOUNTS_SORT_SENTINEL（实现改了形状，这条断言要跟着改）')
        sentinel = set(re.findall(r'(\w+):', s.group(1)))
        self.assertTrue(sentinel, '哨兵对象里没有解析出任何键')

        self.assertEqual(keys, sentinel,
                         'SortKey 与 ACCOUNTS_SORT_SENTINEL 漂移：'
                         f'SortKey={sorted(keys)} 哨兵={sorted(sentinel)}——'
                         '新增档位时两处要一起改，否则那一档的偏好记不住')

    def test_sort_direction_stays_binary(self) -> None:
        """SortDirection 只允许两档。

        将来若加第三档，display-prefs.ts 的读规则「非 desc 一律 asc」会把新档位
        永远读成 asc——这条断言逼着改的人把新档位的读法想清楚。
        """
        src = (_ROOT / 'web' / 'lib' / 'account-list.ts').read_text(encoding='utf-8')
        code = re.sub(r'/\*.*?\*/', '', src, flags=re.S)   # 块注释
        code = re.sub(r'//[^\n]*', '', code)               # 行注释
        m = re.search(r'export type SortDirection =(.*?);', code, flags=re.S)
        self.assertIsNotNone(m, 'account-list.ts 里找不到 SortDirection 定义')
        dirs = set(re.findall(r"'([a-z]+)'", m.group(1)))
        self.assertEqual(dirs, {'asc', 'desc'},
                         f'SortDirection 变成了 {sorted(dirs)}：'
                         'display-prefs.ts 的读规则与相关断言要跟着想清楚')

    def test_drag_switch_does_not_write_pref(self) -> None:
        """拖拽自动切到「自定义顺序」档不得写排序偏好（feature 级不变式）。

        拖一下就改掉用户自己选的默认视图是反面教材（见 display-prefs.ts 头注）：
        系统替用户做的切换只能改本次会话的 state，不能动 localStorage。做法是数
        page.tsx 里 setAccountsSort( 的调用次数——只允许 chooseSort 与
        clearListFilters 各一次；拖拽路径一旦加了写偏好，这里就会失败。
        """
        src = (_ROOT / 'web' / 'app' / '(main)' / 'accounts' / 'page.tsx').read_text(
            encoding='utf-8')
        self.assertIn("setSort('manual')", src, '拖拽前自动切档的调用不见了')
        calls = src.count('setAccountsSort(')
        self.assertEqual(
            calls, 2,
            f'page.tsx 里 setAccountsSort( 出现了 {calls} 次（应只在 chooseSort 与 '
            'clearListFilters 各一次）。若你在拖拽自动切档路径加了写偏好：那是刻意'
            '禁止的——拖一下不该改掉用户自己选的默认视图（见 display-prefs.ts 头注）')


if __name__ == '__main__':
    unittest.main()

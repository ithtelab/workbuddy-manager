"""`dev/apply_upstream_patches.py` 的「已应用」判定（补丁流水线的护栏）。

为什么单独立一条：这个脚本是**重打包与发布**的必经步骤（CI 取资产、维护者重打资产都
走它）。判定一旦对上下文敏感，就会出现「内容明明是对的、脚本却报 patch does not match
this snapshot」——2026-10-17 加第 6 个补丁时就踩到了：新补丁改动了前一个补丁的上下文
邻域，`git apply --reverse --check` 于是失败，整条流水线卡死。现在改成**按新增行内容
判定**（反向 apply 仍作为兜底）。
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        'applyp', str(_ROOT / 'dev' / 'apply_upstream_patches.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hunk(add_line: str, before: str = '') -> str:
    """构造一份最小但**合法**的补丁：在 line1 之后插入 add_line（before 则插在其前）。"""
    return ('--- a/pkg/a.go\n'
            '+++ b/pkg/a.go\n'
            '@@ -1,5 +1,6 @@\n'
            ' line1\n'
            + ((' ' + before + '\n') if before else '')
            + '+' + add_line + '\n'
            ' line2\n line3\n line4\n line5\n')


class AlreadyAppliedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = self.root / 'src'
        (self.src / 'pkg').mkdir(parents=True)
        (self.src / 'go.mod').write_bytes(b'module x\n')
        (self.src / 'docker-compose.yml').write_bytes(b'services: {}\n')
        self.target = self.src / 'pkg' / 'a.go'
        self.target.write_bytes(b'line1\nline2\nline3\nline4\nline5\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _patch(self, body: str, name: str) -> Path:
        p = self.root / name
        p.write_bytes(body.encode())
        return p

    def test_detects_applied_by_added_lines(self) -> None:
        self._patch(_hunk('inserted'), '0001-x.patch')
        self.assertEqual(self.mod.apply_patches(self.src, self.root), ['applied: 0001-x.patch'])
        # 再跑一次：按内容判定为「已应用」（不依赖反向 apply 的上下文匹配）
        self.assertEqual(self.mod.apply_patches(self.src, self.root),
                         ['already applied: 0001-x.patch'])

    def test_removes_stray_patch_copy_at_source_root(self) -> None:
        """历史事故：0040 外层 diff 把补丁自身当成新增文件，源码根多了 0040.patch。

        它会随载体一起发布，纯属垃圾；流水线重入时要自动清掉。
        """
        stray = self.src / '0040.patch'
        stray.write_bytes(b'leftover' + bytes([10]))
        self._patch(_hunk('inserted'), '0001-x.patch')
        self.mod.apply_patches(self.src, self.root)
        self.assertFalse(stray.exists(), '源码根的补丁副本没有被清理')

    def test_context_drift_does_not_break_detection(self) -> None:
        """后一个补丁改动前一个补丁的**上下文邻域**——按内容判定仍要认出「已应用」。

        这正是 2026-10-17 的真实场景：`git apply --reverse --check` 会失败，
        于是流水线在重入时报错（而内容是对的）。
        """
        self._patch(_hunk('first'), '0001-first.patch')
        self._patch(_hunk('second', before='first'), '0002-second.patch')
        self.mod.apply_patches(self.src, self.root)
        # 再制造一次上下文漂移（相当于第三个补丁又动了这段的邻域）
        text = self.target.read_text(encoding='utf-8')
        self.target.write_text(text.replace('line1\n', 'line1\nline1b\n', 1), encoding='utf-8')
        results = self.mod.apply_patches(self.src, self.root)
        self.assertEqual(results, ['already applied: 0001-first.patch',
                                   'already applied: 0002-second.patch'],
                         '上下文漂移后仍应按内容认出「已应用」')

    def test_missing_added_line_is_not_applied(self) -> None:
        patch = self._patch(_hunk('never-inserted'), '0001-x.patch')
        self.assertFalse(self.mod._already_applied(patch, self.src))

    def test_common_lines_elsewhere_do_not_mask_a_missing_hunk(self) -> None:
        """`}` 之类的常见行不能算「已应用」的证据。

        旧实现用子串匹配：补丁新增 `}` 与 `inserted`，文件里本来就有 `}`
        和 `prefix-inserted-suffix`（子串包含 inserted），于是被误判成已应用，
        `git apply` 被跳过 —— 产物里少了整段逻辑，而且没有任何报错。
        """
        self.target.write_bytes(
            b'line1\n}\nprefix-inserted-suffix\nline2\nline3\nline4\nline5\n')
        body = ('--- a/pkg/a.go\n'
                '+++ b/pkg/a.go\n'
                '@@ -1,5 +1,7 @@\n'
                ' line1\n'
                '+}\n'
                '+inserted\n'
                ' line2\n line3\n line4\n line5\n')
        patch = self._patch(body, '0001-x.patch')
        self.assertFalse(self.mod._already_applied(patch, self.src),
                         '常见行/子串被当成了「补丁已应用」的证据')

    def test_real_patch_dir_parses_added_lines(self) -> None:
        """真补丁也要能被解析出「文件 → 新增行」（判定按这份数据走）。"""
        files = self.mod._added_lines(_ROOT / 'dev' / 'upstream-patches'
                                      / '0005-conversation-id-header-family.patch')
        self.assertIn('internal/upstream/client.go', files)
        self.assertTrue(any('CodeOf' in line for line in files['internal/upstream/client.go']),
                        '未解析出该补丁的新增函数')


class ManifestIdempotencyTest(unittest.TestCase):
    """后一个补丁改写前一个补丁的新增行时，重入仍须判定为「已应用」。

    现场（2026-10-18）：0009 引入的思考兜底块被 0010 重排（帧序修正），0009 的
    新增行在树里已不存在 —— 按内容判定会把 0009 误判成「没打」，第二次运行直接
    报 patch does not match。清单按「补丁字节哈希」记录已应用事实，避免这种误判。
    """

    def setUp(self) -> None:
        self.mod = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = self.root / 'src'
        (self.src / 'pkg').mkdir(parents=True)
        (self.src / 'go.mod').write_bytes(b'module x\n')
        (self.src / 'docker-compose.yml').write_bytes(b'services: {}\n')
        self.target = self.src / 'pkg' / 'a.go'
        self.target.write_bytes(b'line1\nline2\nline3\nline4\nline5\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_later_patch_may_rewrite_earlier_added_lines(self) -> None:
        insert = ('--- a/pkg/a.go\n'
                  '+++ b/pkg/a.go\n'
                  '@@ -1,5 +1,6 @@\n'
                  ' line1\n'
                  '+alpha\n'
                  ' line2\n line3\n line4\n line5\n')
        rewrite = ('--- a/pkg/a.go\n'
                   '+++ b/pkg/a.go\n'
                   '@@ -1,6 +1,6 @@\n'
                   ' line1\n'
                   '-alpha\n'
                   '+beta\n'
                   ' line2\n line3\n line4\n line5\n')
        (self.root / '0001-add.patch').write_bytes(insert.encode())
        (self.root / '0002-rewrite.patch').write_bytes(rewrite.encode())

        self.assertEqual(self.mod.apply_patches(self.src, self.root),
                         ['applied: 0001-add.patch', 'applied: 0002-rewrite.patch'])
        # 清单已落盘，且记录的是补丁名 → 字节哈希
        manifest = self.mod._load_manifest(self.src)
        self.assertEqual(sorted(manifest), ['0001-add.patch', '0002-rewrite.patch'])

        # 重入：0001 的新增行 alpha 已被 0002 改写成 beta，内容判定会失败；
        # 清单命中必须让它仍然算「已应用」，否则整条流水线卡死。
        self.assertEqual(self.mod.apply_patches(self.src, self.root),
                         ['already applied: 0001-add.patch',
                          'already applied: 0002-rewrite.patch'])


if __name__ == '__main__':
    unittest.main()

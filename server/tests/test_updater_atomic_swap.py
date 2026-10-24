"""目录级原子替换：替换失败必须把旧目录换回来（2.2.1）。

现场：`deploy/update.py` 以前是 `rmtree(server)` + `copytree(new)`。复制中途失败
（磁盘满、权限、更新进程被杀）会让 `server/` 只剩半份，服务再也起不来 —— 备份目录
在，但没有人会去自动恢复。现在改为「先完整复制到 `server.new`，再交换」。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]


def _load_update():
    spec = importlib.util.spec_from_file_location(
        'upd_swap', str(_ROOT / 'deploy' / 'update.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SwapDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_update()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.incoming = self.root / 'new-server'
        self.incoming.mkdir()
        (self.incoming / 'main.py').write_text('new', encoding='utf-8')
        (self.incoming / 'pkg').mkdir()
        (self.incoming / 'pkg' / 'x.py').write_text('x', encoding='utf-8')
        self.target = self.root / 'server'
        self.target.mkdir()
        (self.target / 'main.py').write_text('old', encoding='utf-8')
        (self.target / 'stale.py').write_text('stale', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_replaces_whole_tree_and_cleans_staging(self) -> None:
        self.mod.swap_dir(self.incoming, self.target)
        self.assertEqual((self.target / 'main.py').read_text(encoding='utf-8'), 'new')
        self.assertTrue((self.target / 'pkg' / 'x.py').is_file())
        # 旧文件不该残留（整目录替换的语义）
        self.assertFalse((self.target / 'stale.py').exists())
        # 中间目录不残留
        self.assertFalse((self.root / 'server.old').exists())
        self.assertFalse((self.root / 'server.new').exists())
        # incoming 是源，不该被搬走或删掉
        self.assertTrue((self.incoming / 'main.py').is_file())

    def test_exdev_falls_back_to_copy_and_delete(self) -> None:
        """overlayfs lowerdir 上的目录 rename 会报 EXDEV（issue #225）。"""
        real_replace = os.replace
        calls = {'n': 0}

        def exdev_first(src, dst):  # noqa: ANN001, ANN202
            calls['n'] += 1
            if calls['n'] == 1:
                raise OSError(18, 'Invalid cross-device link')
            return real_replace(src, dst)

        with mock.patch('os.replace', side_effect=exdev_first):
            self.mod.swap_dir(self.incoming, self.target)
        self.assertEqual((self.target / 'main.py').read_text(encoding='utf-8'), 'new')
        self.assertFalse((self.target / 'stale.py').exists())
        self.assertFalse((self.root / 'server.old').exists())

    def test_rollback_when_swap_fails(self) -> None:
        real_replace = os.replace
        calls = {'n': 0}

        def flaky(src, dst):  # noqa: ANN001, ANN202
            calls['n'] += 1
            if calls['n'] == 2:      # 第二次 = staging → target，模拟交换失败
                raise OSError('boom')
            return real_replace(src, dst)

        with mock.patch('os.replace', side_effect=flaky):
            with self.assertRaises(OSError):
                self.mod.swap_dir(self.incoming, self.target)

        # 旧目录被换回来了：服务仍有完整代码可跑
        self.assertEqual((self.target / 'main.py').read_text(encoding='utf-8'), 'old')
        self.assertTrue((self.target / 'stale.py').is_file())
        self.assertFalse((self.root / 'server.old').exists())
        self.assertFalse((self.root / 'server.new').exists())


if __name__ == '__main__':
    unittest.main()

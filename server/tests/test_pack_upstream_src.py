"""`dev/pack_upstream_src.py` 的打包口径（issue #202）。

资产是随面板 Release 分发的上游源码，用户那边的更新器把它同步进部署树、再构建网关
镜像。它一旦带 CRLF，容器里脚本的首行就成了 `#!/usr/bin/env bash\r` —— 解释器路径
对不上，一键登录 / 签到 / 积分这些依赖脚本的功能整体失效。

历史事实：**包内本身就带 CRLF**（实测 293/298 个文件），因为打包是在 Windows 上做的、
而脚本是字节拷贝。修在打包这一步（治源头），同步侧另有兜底（deploy/update.py）。
"""
from __future__ import annotations

import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load_packer():
    spec = importlib.util.spec_from_file_location(
        'packer', str(_ROOT / 'dev' / 'pack_upstream_src.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LfTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_packer()

    def test_crlf_text_is_normalized(self) -> None:
        self.assertEqual(self.mod._lf_text(b'a\r\nb\r\n'), b'a\nb\n')

    def test_lf_stays_untouched(self) -> None:
        self.assertEqual(self.mod._lf_text(b'a\nb'), b'a\nb')

    def test_binary_with_nul_is_untouched(self) -> None:
        blob = b'\x89PNG\r\n\x1a\n\x00\r\n'
        self.assertEqual(self.mod._lf_text(blob), blob, '含 NUL 的当二进制，一个字节都不动')

    def test_non_utf8_is_untouched(self) -> None:
        blob = b'\xff\xfe\r\n\xff'          # 不是合法 UTF-8
        self.assertEqual(self.mod._lf_text(blob), blob)


class SecretExclusionTest(unittest.TestCase):
    """部署树里的凭据/运行时数据绝不进公开资产（2026-10-18 审计）。"""

    def setUp(self) -> None:
        self.mod = _load_packer()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, data: bytes = b'x') -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def test_runtime_files_are_excluded(self) -> None:
        keep = ['server/main.py', 'scripts/task_runner.py', 'config.example.json',
                'README.md']
        drop = ['.env', '.env.local', 'users.json', 'manager.db', 'manager.db-wal',
                'update.log', 'logs/today.log', 'data/state.json',
                'auths/workbuddy-u.json', 'config.json', '.venv/lib/x.py',
                'server/__pycache__/main.pyc']
        for rel in keep + drop:
            self._write(rel)

        got = {p.relative_to(self.root).as_posix() for p in self.mod._iter_files(self.root)}
        for rel in keep:
            self.assertIn(rel, got, f'{rel} 是源码，不该被排除')
        for rel in drop:
            self.assertNotIn(rel, got, f'{rel} 是运行时数据/凭据，不能进公开资产')


class UpstreamIdTest(unittest.TestCase):
    """`.upstream-id` 是面板判断「这次发布带没带上游」的依据。"""

    def test_id_ignores_generated_notice_but_tracks_sources(self) -> None:
        mod = _load_packer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'scripts').mkdir()
            (root / 'scripts' / 'task_runner.py').write_bytes(b'print(1)\n')
            (root / 'UPSTREAM-SRC.txt').write_bytes(b'note\n')
            first = mod.tree_id(root)
            (root / 'UPSTREAM-SRC.txt').write_bytes(b'note changed\n')
            self.assertEqual(mod.tree_id(root), first, '说明文件的时间戳/文案不该改变 id')
            (root / 'scripts' / 'task_runner.py').write_bytes(b'print(2)\n')
            self.assertNotEqual(mod.tree_id(root), first, '源码变化必须改变 id')


class PackRoundTripTest(unittest.TestCase):
    """真打一次包：脚本落地必须是 LF，二进制原样，可执行位保留。"""

    def test_packed_scripts_have_lf(self) -> None:
        mod = _load_packer()
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'src'
            (src / 'scripts').mkdir(parents=True)
            (src / 'docker-compose.yml').write_bytes(b'services: {}\r\n')
            (src / 'login.sh').write_bytes(b'#!/usr/bin/env bash\r\necho hi\r\n')
            (src / 'logo.png').write_bytes(b'\x89PNG\r\n\x1a\n\x00\r\n')
            out = Path(tmp) / 'out'
            argv = ['pack_upstream_src.py', str(src), '-o', str(out)]
            import sys
            old = sys.argv
            sys.argv = argv
            try:
                self.assertEqual(mod.main(), 0)
            finally:
                sys.argv = old

            with tarfile.open(out / 'workbuddy2api-src.tar.gz') as tf:
                names = tf.getnames()
                self.assertIn('workbuddy2api/login.sh', names)
                sh = tf.extractfile('workbuddy2api/login.sh').read()
                self.assertEqual(sh, b'#!/usr/bin/env bash\necho hi\n',
                                 '包内脚本必须无 CR —— 否则容器里执行不了')
                png = tf.extractfile('workbuddy2api/logo.png').read()
                self.assertEqual(png, b'\x89PNG\r\n\x1a\n\x00\r\n', '二进制不该被改')


if __name__ == '__main__':
    unittest.main()

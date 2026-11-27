"""Run the browser's upstream-schedule draft logic on synthetic config values.

`web/lib/upstream-schedule.ts` 是零运行时依赖的纯模块，Node 的 type stripping 可以
直接 import 它；本文件只是把那条 JS 测试挂进 Python 测试套件（无 node 时 skip）。
"""
import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path('web/lib/upstream-schedule.test.mjs')


class UpstreamScheduleDraftTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_multi_hours_sparse_patch_and_out_of_range_values(self):
        version = subprocess.run(['node', '--version'], capture_output=True, text=True, timeout=10)
        parts = version.stdout.strip().lstrip('v').split('.')
        if version.returncode or len(parts) < 2 or tuple(map(int, parts[:2])) < (22, 6):
            self.skipTest('node >= 22.6 is required for TypeScript stripping')
        root = Path(__file__).resolve().parents[2]
        self.assertTrue((root / _SCRIPT).is_file(), f'missing test script: {_SCRIPT}')
        result = subprocess.run(
            ['node', '--experimental-strip-types', str(_SCRIPT)],
            cwd=root, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('upstream schedule draft passed', result.stdout)

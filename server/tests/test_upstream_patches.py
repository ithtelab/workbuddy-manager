import shutil
from pathlib import Path
import tempfile
import subprocess
import unittest

from dev.apply_upstream_patches import apply_patches


@unittest.skipUnless(shutil.which('git'), 'patch application requires git')
class UpstreamPatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / 'source'
        self.src.mkdir()
        (self.src / 'go.mod').write_text('module test\n')
        (self.src / 'docker-compose.yml').write_text('services: {}\n')
        (self.src / 'example.txt').write_text('before\n')
        self.patches = self.root / 'patches'
        self.patches.mkdir()
        (self.patches / '0001.patch').write_text(
            'diff --git a/example.txt b/example.txt\n'
            '--- a/example.txt\n+++ b/example.txt\n'
            '@@ -1 +1 @@\n-before\n+after\n')

    def test_apply_and_already_applied(self):
        self.assertEqual(apply_patches(self.src, self.patches), ['applied: 0001.patch'])
        self.assertEqual((self.src / 'example.txt').read_text(), 'after\n')
        self.assertEqual(apply_patches(self.src, self.patches), ['already applied: 0001.patch'])

    def test_snapshot_drift_fails_without_changing_source(self):
        (self.src / 'example.txt').write_text('different\n')
        with self.assertRaisesRegex(RuntimeError, 'does not match this snapshot'):
            apply_patches(self.src, self.patches)
        self.assertEqual((self.src / 'example.txt').read_text(), 'different\n')

    def test_source_nested_in_manager_checkout_is_actually_patched(self):
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True)
        self.assertEqual(apply_patches(self.src, self.patches), ['applied: 0001.patch'])
        self.assertEqual((self.src / 'example.txt').read_text(), 'after\n')
        self.assertEqual(apply_patches(self.src, self.patches), ['already applied: 0001.patch'])

    def test_missing_patch_directory_is_an_error(self):
        with self.assertRaisesRegex(ValueError, 'No upstream patches'):
            apply_patches(self.src, self.root / 'missing')

    def test_wrong_source_is_an_error(self):
        with self.assertRaisesRegex(ValueError, 'Not a workbuddy2api'):
            apply_patches(self.root, self.patches)

    def test_partial_application_is_not_treated_as_already_applied(self):
        (self.src / 'another.txt').write_text('before\n')
        patch = self.patches / '0001.patch'
        patch.write_text(patch.read_text() +
            'diff --git a/another.txt b/another.txt\n'
            '--- a/another.txt\n+++ b/another.txt\n'
            '@@ -1 +1 @@\n-before\n+after\n')
        (self.src / 'example.txt').write_text('after\n')
        with self.assertRaisesRegex(RuntimeError, 'does not match this snapshot'):
            apply_patches(self.src, self.patches)
        self.assertEqual((self.src / 'another.txt').read_text(), 'before\n')

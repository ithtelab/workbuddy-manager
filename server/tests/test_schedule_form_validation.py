"""Run the browser's pure schedule validator on synthetic form values."""
import shutil
import subprocess
import unittest
from pathlib import Path

class ScheduleFormValidationTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_invalid_schedule_is_rejected_before_submission(self):
        version = subprocess.run(['node', '--version'], capture_output=True, text=True, timeout=10)
        parts = version.stdout.strip().lstrip('v').split('.')
        if version.returncode or len(parts) < 2 or tuple(map(int, parts[:2])) < (22, 6):
            self.skipTest('node >= 22.6 is required for TypeScript stripping')
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(['node', '--experimental-strip-types', 'web/lib/schedule-validation.test.mjs'], cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

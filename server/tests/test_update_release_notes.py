"""更新前可见的 Release 正文与包内上游标识（issue #217）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.services import updater


class ReleaseNotesTest(unittest.TestCase):
    def test_release_body_and_upstream_marker_are_exposed(self) -> None:
        uid = 'a' * 64
        body = '### 新增\n\n- 需要配套上游\n\n<!-- upstream-id: %s -->' % uid
        rel = {'tag_name': 'v9.9.9', 'html_url': 'https://example/rel',
               'published_at': '2026-10-18T00:00:00Z', 'assets': [],
               'body': body}
        with mock.patch.object(updater, '_gh_get', return_value=rel):
            out = updater._fetch_remote_versions()
        mgr = out['manager']
        self.assertIn('需要配套上游', mgr['release_notes'])
        self.assertNotIn('upstream-id', mgr['release_notes'], '隐藏标记不该显示给用户')
        self.assertEqual(mgr['upstream_id'], uid)
        self.assertEqual(mgr['published_at'], '2026-10-18T00:00:00Z')

    def test_missing_marker_is_empty_not_guessed(self) -> None:
        rel = {'tag_name': 'v9.9.9', 'assets': [], 'body': '普通说明'}
        with mock.patch.object(updater, '_gh_get', return_value=rel):
            out = updater._fetch_remote_versions()
        self.assertEqual(out['manager']['upstream_id'], '')

    def test_local_upstream_id_reads_pack_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.upstream-id').write_text('b' * 64 + '\n', encoding='utf-8')
            with mock.patch.object(updater, '_upstream_dir', return_value=Path(tmp)):
                self.assertEqual(updater._local_upstream_id(), 'b' * 64)
        with mock.patch.object(updater, '_upstream_dir', return_value=Path(tmp) / 'missing'):
            self.assertEqual(updater._local_upstream_id(), '')

    def test_missing_local_upstream_is_marked_changed(self) -> None:
        """老部署没有 .upstream-id：Release 带上游时必须提示需要同步。"""
        cache = {'checked_at': int(updater.time.time()),
                 'manager': {'latest': 'v1.0.0', 'upstream_id': 'a' * 64}}
        with mock.patch.object(updater, '_read_cache', return_value=dict(cache)), \
             mock.patch.object(updater, 'current_version', return_value='v1.0.0'), \
             mock.patch.object(updater, '_local_upstream_id', return_value=''):
            out = updater.check_updates()
        self.assertTrue(out['manager']['upstream_changed'])
        self.assertFalse(out['has_any'])

    def test_stale_release_metadata_does_not_claim_upstream_mismatch(self) -> None:
        """本次检查失败、用的是旧缓存时，不能据此断言本机上游不一致。

        旧缓存里的 upstream-id 可能还是上一个 Release 的；拿它和刚同步好的
        本机比较，会一直显示「需要同步」，用户点多少次都消不掉（issue #240）。
        """
        cache = {'checked_at': int(updater.time.time()),
                 'manager': {'latest': 'v1.0.0', 'upstream_id': 'a' * 64,
                             'stale': True, 'error': 'HTTP 403'}}
        with mock.patch.object(updater, '_read_cache', return_value=dict(cache)), \
             mock.patch.object(updater, 'current_version', return_value='v1.0.0'), \
             mock.patch.object(updater, '_local_upstream_id', return_value='b' * 64):
            out = updater.check_updates()
        self.assertFalse(out['manager']['upstream_changed'],
                         '旧缓存不能作为「上游不一致」的依据')

    def test_packaged_updater_is_preferred(self) -> None:
        """server/deploy/update.py 优先于根目录 deploy/update.py。

        旧安装的根目录更新器可能不支持随包同步；它不会替换自己，
        只靠根目录那份会让提示永远消不掉（issue #240）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'server' / 'deploy').mkdir(parents=True)
            (root / 'deploy').mkdir()
            root_deploy = root / 'deploy' / 'update.py'
            packaged = root / 'server' / 'deploy' / 'update.py'
            root_deploy.write_text('old', encoding='utf-8')
            with mock.patch.object(updater.config, 'ROOT', root):
                self.assertEqual(updater._updater_script(), root_deploy)
            packaged.write_text('new', encoding='utf-8')
            with mock.patch.object(updater.config, 'ROOT', root):
                self.assertEqual(updater._updater_script(), packaged)


if __name__ == '__main__':
    unittest.main()

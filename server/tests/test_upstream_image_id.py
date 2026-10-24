"""镜像部署下「随包上游是否同源」的判定。

上游镜像自带 `dev.workbuddy.upstream-id` label（发布流水线写入，与 Release
正文隐藏标记、包内 `.upstream-id` 同值）。镜像部署的机器磁盘上没有源码目录，
只认 `.upstream-id` 会把它永远判成「未知 = 需要同步」——面板一直提示
「随包上游与本机不一致」，点了「全部更新」却只同步一份用不到的源码。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config
from server.services import updater

UID = 'e' * 64
PACK_ID = 'b' * 64


class _Proc:
    def __init__(self, stdout: str = '', returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


class ImageUpstreamIdTest(unittest.TestCase):
    def setUp(self) -> None:
        self._cache = updater._image_id_cache
        self._mode = config.WB2API_MODE
        updater._image_id_cache = None
        config.WB2API_MODE = 'docker'

    def tearDown(self) -> None:
        updater._image_id_cache = self._cache
        config.WB2API_MODE = self._mode

    def test_label_is_read_from_running_container_image(self) -> None:
        run = mock.Mock(return_value=_Proc(UID + '\n'))
        with mock.patch.object(updater, 'can_control_docker', return_value=True), \
             mock.patch.object(updater.subprocess, 'run', run):
            self.assertEqual(updater._image_upstream_id(), UID)
        cmd = run.call_args[0][0]
        self.assertIn(updater.UPSTREAM_IMAGE_LABEL, cmd[3])
        self.assertEqual(cmd[-1], config.WB2API_CONTAINER)

    def test_label_lookup_is_cached(self) -> None:
        run = mock.Mock(return_value=_Proc(UID + '\n'))
        with mock.patch.object(updater, 'can_control_docker', return_value=True), \
             mock.patch.object(updater.subprocess, 'run', run):
            self.assertEqual(updater._image_upstream_id(), UID)
            self.assertEqual(updater._image_upstream_id(), UID)
        self.assertEqual(run.call_count, 1)

    def test_image_without_label_falls_back_to_pack_marker(self) -> None:
        """旧镜像（≤2.8.2）没有 label：模板渲染成 <no value>，退回文件判定。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.upstream-id').write_text(PACK_ID, encoding='utf-8')
            with mock.patch.object(updater, '_upstream_dir', return_value=Path(tmp)), \
                 mock.patch.object(updater, 'can_control_docker', return_value=True), \
                 mock.patch.object(updater.subprocess, 'run',
                                   mock.Mock(return_value=_Proc('<no value>\n'))):
                self.assertEqual(updater._running_upstream_id(), PACK_ID)

    def test_docker_unavailable_falls_back_to_pack_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.upstream-id').write_text(PACK_ID, encoding='utf-8')
            with mock.patch.object(updater, '_upstream_dir', return_value=Path(tmp)), \
                 mock.patch.object(updater, 'can_control_docker', return_value=False):
                self.assertEqual(updater._running_upstream_id(), PACK_ID)

    def test_native_mode_never_calls_docker(self) -> None:
        config.WB2API_MODE = 'native'
        with mock.patch.object(updater, 'can_control_docker') as probe:
            self.assertEqual(updater._image_upstream_id(), '')
        probe.assert_not_called()

    def test_docker_inspect_failure_does_not_raise(self) -> None:
        with mock.patch.object(updater, 'can_control_docker', return_value=True), \
             mock.patch.object(updater.subprocess, 'run',
                               side_effect=OSError('docker: not found')), \
             self.assertLogs(updater.logger, level='WARNING'):
            self.assertEqual(updater._image_upstream_id(), '')


class UpstreamChangedTest(unittest.TestCase):
    def test_same_id_is_not_changed(self) -> None:
        self.assertFalse(updater.upstream_changed(UID, UID))

    def test_different_id_is_changed(self) -> None:
        self.assertTrue(updater.upstream_changed(UID, 'f' * 64))

    def test_unknown_running_id_is_changed(self) -> None:
        self.assertTrue(updater.upstream_changed(UID, ''))

    def test_stale_release_does_not_claim_mismatch(self) -> None:
        self.assertFalse(updater.upstream_changed(UID, '', manager_stale=True))

    def test_missing_release_id_never_flags(self) -> None:
        self.assertFalse(updater.upstream_changed('', ''))

    def test_image_deployment_at_same_source_reports_in_sync(self) -> None:
        cache = {'checked_at': int(updater.time.time()),
                 'manager': {'latest': 'v1.0.0', 'upstream_id': UID}}
        with mock.patch.object(updater, '_read_cache', return_value=dict(cache)), \
             mock.patch.object(updater, 'current_version', return_value='v1.0.0'), \
             mock.patch.object(updater, '_running_upstream_id', return_value=UID):
            out = updater.check_updates()
        self.assertFalse(out['manager']['upstream_changed'])
        self.assertEqual(out['upstream']['current'], UID[:8])

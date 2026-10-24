"""更新日志里的凭据脱敏与只读账号的可见范围（2.2.1 安全收口）。

现场：下载失败时 `_download_hint` 会把完整 `WB_UPDATE_PROXY`（`http://user:pass@host`）
拼进提示，这条提示写进 `update-status.json`；而 `/api/system/update-status` 只要求
登录（viewer 也能调）。于是一个只读账号轮询状态就能读到部署者的代理密码。

两道防线：
  1. **写入口**脱敏（`deploy/update.py::redact_proxy`）；
  2. **出口**兜底（`updater.redact_credentials`）—— 旧版本已经写进文件的日志同样要洗，
     并且非管理员拿不到日志字段。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import system as system_router  # noqa: E402
from server.services import updater  # noqa: E402


class RedactCredentialsTest(unittest.TestCase):
    def test_strips_userinfo(self) -> None:
        cases = [
            ('http://user:pass@127.0.0.1:7890', 'http://***@127.0.0.1:7890'),
            ('代理 https://u:p@proxy.example:8080，仍连不上',
             '代理 https://***@proxy.example:8080，仍连不上'),
            ('socks5h://token@host:1080', 'socks5h://***@host:1080'),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(updater.redact_credentials(raw), want)

    def test_keeps_credential_free_text(self) -> None:
        for text in ('http://proxy:8080', 'plain text', ''):
            self.assertEqual(updater.redact_credentials(text), text)


class UpdateStatusVisibilityTest(unittest.TestCase):
    """viewer 不看日志；管理员照常拿到（含脱敏后的行）。"""

    def _call(self, role: str) -> dict:
        with mock.patch.object(updater, 'read_status', return_value={
            'running': False, 'version': '2.2.1',
            'logs': ['开始更新', '代理 http://u:p@1.2.3.4:7890 连不上'],
        }), mock.patch.object(updater, 'tail_log', return_value='line with http://u:p@h:1'):
            return system_router.update_status(user={'role': role})

    def test_viewer_gets_no_logs(self) -> None:
        status = self._call('viewer')
        self.assertEqual(status['logs'], [])
        self.assertEqual(status['log_tail'], '')
        # 非日志字段仍可见：只读账号也需要知道「是否在更新 / 当前版本」
        self.assertEqual(status['version'], '2.2.1')

    def test_admin_keeps_logs(self) -> None:
        status = self._call('admin')
        self.assertEqual(len(status['logs']), 2)
        self.assertIn('line with', status['log_tail'])


class OutcomeRedactionTest(unittest.TestCase):
    """`read_status` 对**存量**日志也要洗一遍（旧版本已经写进去的凭据）。"""

    def test_existing_status_file_is_scrubbed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'update-status.json'
            f.write_text('{"logs":["old http://user:pw@host:1 failed"],'
                         '"running":false}', encoding='utf-8')
            with mock.patch.object(updater, 'STATUS_FILE', f), \
                 mock.patch.object(updater, '_lock_active', return_value=False):
                status = updater.read_status()
        self.assertEqual(status['logs'], ['old http://***@host:1 failed'])


    def test_dict_entries_keep_their_shape(self) -> None:
        """issue #224：dict 日志不能被 str()，否则前端 l.text 全是 undefined。"""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'update-status.json'
            f.write_text(
                '{"logs":[{"ts":1,"level":"info",'
                '"text":"proxy http://u:p@h:1 failed"}],"running":false}',
                encoding='utf-8')
            with mock.patch.object(updater, 'STATUS_FILE', f), mock.patch.object(
                    updater, '_lock_active', return_value=False):
                status = updater.read_status()
        entry = status['logs'][0]
        self.assertIsInstance(entry, dict)
        self.assertEqual(entry['level'], 'info')
        self.assertEqual(entry['text'], 'proxy http://***@h:1 failed')


if __name__ == '__main__':
    unittest.main()

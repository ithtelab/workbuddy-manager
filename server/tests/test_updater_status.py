"""更新状态判定的回归测试。

背景：管理端更新最后一步是 `systemctl restart`，而 systemd 默认
KillMode=control-group 会把更新进程（连同一个 cgroup 里的所有进程）一起终止。
旧逻辑一看到「标记运行中但进程没了」就判为「异常中断」，
于是更新明明成功、界面却显示「更新未完成」。

这里覆盖两条判定证据：
  1. 状态里记了目标版本，且已部署版本等于它
  2. 日志里出现「管理端已更新到 X，重启服务以生效」且 X 等于已部署版本

运行：python -m unittest discover -s server/tests -t . -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import updater  # noqa: E402


class UpdateLanded(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = updater.current_version

    def tearDown(self) -> None:
        updater.current_version = self._orig

    def _at(self, version: str) -> None:
        updater.current_version = lambda: version  # type: ignore[assignment]

    def test_target_version_matches(self) -> None:
        self._at('v1.0.5')
        self.assertTrue(updater._update_landed({'target_version': '1.0.5', 'logs': []}))

    def test_target_version_mismatch(self) -> None:
        self._at('v1.0.4')
        self.assertFalse(updater._update_landed({'target_version': '1.0.5', 'logs': []}))

    def test_legacy_worker_without_target_version_uses_log(self) -> None:
        """旧版脚本不写 target_version，靠日志行兜底（过渡兼容）。"""
        self._at('v1.0.5')
        status = {
            'target_version': '',
            'logs': [
                {'text': '== 更新管理端 =='},
                {'text': '管理端已更新到 v1.0.5，重启服务以生效'},
                {'text': '$ systemctl restart workbuddy-web'},
            ],
        }
        self.assertTrue(updater._update_landed(status))

    def test_log_version_does_not_match_current(self) -> None:
        """日志说更新到了 X，但磁盘上还是旧版本 → 仍算失败。"""
        self._at('v1.0.4')
        status = {
            'logs': [{'text': '管理端已更新到 v1.0.5，重启服务以生效'}],
        }
        self.assertFalse(updater._update_landed(status))

    def test_no_evidence_is_failure(self) -> None:
        self._at('v1.0.4')
        self.assertFalse(updater._update_landed({'logs': []}))


class VersionCompare(unittest.TestCase):
    """版本比较必须是「严格更新」，否则回滚场景会冒出降级提示。

    真实事故：更新到 v1.0.5 后，6 小时缓存里还存着 v1.0.4，
    旧逻辑用「不相等」判断，于是界面显示「管理端：v1.0.5 → v1.0.4」。
    """

    def test_newer_detected(self) -> None:
        for remote, current in [
            ('v1.0.5', 'v1.0.4'),
            ('1.0.10', '1.0.9'),   # 数字段按数值比，不能按字符串
            ('v1.1.0', 'v1.0.99'),
            ('v2.0.0', 'v1.99.99'),
            ('v1.0.1', 'v1.0'),
        ]:
            self.assertTrue(updater._version_newer(remote, current), f'{remote} > {current}')

    def test_equal_is_not_an_update(self) -> None:
        for a, b in [('v1.0.5', '1.0.5'), ('1.0.5', 'v1.0.5'), ('v1.0', 'v1.0.0')]:
            self.assertFalse(updater._version_newer(a, b), f'{a} == {b}')

    def test_older_is_not_an_update(self) -> None:
        """降级（当前版本领先于远端）不能提示更新——这就是本次事故。"""
        for remote, current in [
            ('v1.0.4', 'v1.0.5'),
            ('v1.0.9', 'v1.0.10'),
            ('v1.0', 'v1.1'),
        ]:
            self.assertFalse(updater._version_newer(remote, current), f'{remote} < {current}')

    def test_unparseable_never_claims_update(self) -> None:
        self.assertFalse(updater._version_newer('latest', 'v1.0.5'))
        self.assertFalse(updater._version_newer('v1.0.5', 'unknown'))
        self.assertFalse(updater._version_newer('', 'v1.0.5'))

    def test_prerelease_suffix_ignored(self) -> None:
        self.assertTrue(updater._version_newer('v1.0.6-rc1', 'v1.0.5'))
        self.assertFalse(updater._version_newer('v1.0.5-rc1', 'v1.0.5'))


class RemoteCheckOnlyManager(unittest.TestCase):
    """上游随包分发后，版本检测不能再请求外部上游仓库。"""

    def test_only_manager_release_is_queried(self) -> None:
        calls: list[str] = []

        def fake_gh(url: str):
            calls.append(url)
            return {'tag_name': 'v9.9.9', 'html_url': '',
                    'assets': [{'name': 'x.tar.gz'}], 'body': ''}

        with mock.patch.object(updater, '_gh_get', side_effect=fake_gh):
            out = updater._fetch_remote_versions()

        self.assertEqual(out['manager']['latest'], 'v9.9.9')
        self.assertNotIn('upstream', out, '不应再有独立的上游版本结果')
        self.assertTrue(calls)
        self.assertTrue(all('workbuddy-manager' in url for url in calls),
                        f'版本检测请求了非管理端仓库：{calls}')


if __name__ == '__main__':
    unittest.main()

"""检测更新：失败不能说成「已是最新」；GitHub Token 可配（issue #204 / #205）。

## #204 现场

出口 IP 的未认证配额被打满 → GitHub 返回 403 → 代码回退到**上一次**成功结果，
用那个旧的 latest 与当前版本比较，得出「无更新」；界面据此弹「已是最新版本 /
管理端与上游均无更新」。**把一次失败说成了结论**，用户会以为确实没有新版本。

## #205 现场

代码一直支持认证（`WB_GITHUB_TOKEN`，配额 5000/小时），但用户没地方知道、也没地方填。

两条都在 `services/updater.py`，这里钉住：失败要标 stale、界面能据此拒绝下结论；
Token 的优先级（环境变量 > 面板）与掩码（绝不回明文）。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import updater  # noqa: E402


class GithubTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        self._store: dict = {}
        self._patches = [
            mock.patch.object(updater.db, 'get_setting',
                              side_effect=lambda k, d=None: self._store.get(k, d)),
            mock.patch.object(updater.db, 'set_setting',
                              side_effect=lambda k, v: self._store.__setitem__(k, v)),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        for k in ('WB_GITHUB_TOKEN', 'GITHUB_TOKEN'):
            os.environ.pop(k, None)

    def test_panel_value_is_used_when_env_missing(self) -> None:
        self._store['github_token'] = 'github_pat_panel'
        self.assertEqual(updater.github_token(), 'github_pat_panel')

    def test_env_wins_over_panel(self) -> None:
        self._store['github_token'] = 'github_pat_panel'
        os.environ['WB_GITHUB_TOKEN'] = 'github_pat_env'
        self.assertEqual(updater.github_token(), 'github_pat_env')
        os.environ.pop('WB_GITHUB_TOKEN')
        os.environ['GITHUB_TOKEN'] = 'ghp_alt'
        self.assertEqual(updater.github_token(), 'ghp_alt')

    def test_no_token_is_empty_not_error(self) -> None:
        self.assertEqual(updater.github_token(), '')

    def test_mask_never_leaks_plaintext(self) -> None:
        masked = updater.mask_token('github_pat_abcdefgh1234')
        self.assertTrue(masked.endswith('1234'))
        self.assertNotIn('abcdefgh', masked)
        self.assertEqual(updater.mask_token(''), '')


class StaleCheckTest(unittest.TestCase):
    """失败回退到旧缓存时，结果必须标 stale（界面据此拒绝下「已是最新」的结论）。"""

    def setUp(self) -> None:
        self._cache: dict = {}
        self._patches = [
            mock.patch.object(updater, '_read_cache', side_effect=lambda: dict(self._cache)),
            mock.patch.object(updater, '_write_cache',
                              side_effect=lambda v: self._cache.update(v)),
            mock.patch.object(updater, 'current_version', return_value='v1.0.0'),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _fetch(self, manager_latest: str, error: str = '') -> dict:
        return {
            'manager': {'latest': manager_latest, 'url': '', 'assets_ready': None, 'error': error},
            'upstream': {'latest': '', 'date': '', 'subject': '', 'error': error},
        }

    def test_failed_fetch_marks_stale(self) -> None:
        # 上一次成功：远端是 v1.0.0（等于当前版本）
        self._cache.update({'checked_at': 1000, 'manager': {'latest': 'v1.0.0'}, 'upstream': {}})
        with mock.patch.object(updater, '_fetch_remote_versions',
                               return_value=self._fetch('', error='HTTP Error 403: rate limit exceeded')):
            out = updater.check_updates(force=True)
        m = out['manager']
        self.assertEqual(m['latest'], 'v1.0.0', '应回退到上一次成功的结果')
        self.assertTrue(m['stale'], '失败回退必须标 stale —— 否则界面会报「已是最新」')
        self.assertEqual(m['stale_at'], 1000, '旧值的检测时间要带出来，界面才能说清')
        self.assertIn('403', m['error'])
        self.assertFalse(out['has_any'])

    def test_successful_fetch_is_not_stale(self) -> None:
        self._cache.update({'checked_at': 1000, 'manager': {'latest': 'v1.0.0'}, 'upstream': {}})
        with mock.patch.object(updater, '_fetch_remote_versions',
                               return_value=self._fetch('v1.0.0')):
            out = updater.check_updates(force=True)
        self.assertFalse(out['manager']['stale'])
        self.assertEqual(out['manager']['error'], '')

    def test_failed_fetch_without_cache_has_no_latest(self) -> None:
        # 从没成功过：latest 为空、error 有值 → 界面必须显示「检测失败」而不是「已是最新」
        with mock.patch.object(updater, '_fetch_remote_versions',
                               return_value=self._fetch('', error='HTTP Error 403')):
            out = updater.check_updates(force=True)
        self.assertEqual(out['manager']['latest'], '')
        self.assertTrue(out['manager']['error'])
        self.assertFalse(out['has_any'])


class GithubTokenEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from server.main import app
        from server import db, security
        self.client = TestClient(app)
        self._store: dict = {'users': [], 'api_tokens': []}
        # 鉴权用依赖覆盖（与 test_account_proxy / test_key_export 同一口径）：
        # patch 函数对象不会影响 FastAPI 在定义路由时捕获的依赖。
        app.dependency_overrides[security.current_user] = lambda: {'username': 'admin', 'role': 'admin'}
        app.dependency_overrides[security.require_admin] = lambda: {'username': 'admin', 'role': 'admin'}
        self.addCleanup(app.dependency_overrides.clear)
        self._patches = [
            mock.patch.object(db, 'get_setting',
                              side_effect=lambda k, d=None: self._store.get(k, d)),
            mock.patch.object(db, 'set_setting',
                              side_effect=lambda k, v: self._store.__setitem__(k, v)),
            mock.patch.object(security, 'audit', return_value=None),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_put_then_get_returns_mask_only(self) -> None:
        r = self.client.put('/api/settings/github-token', json={'token': 'github_pat_secret1234'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['configured'])
        self.assertNotIn('secret', r.text, '响应里不能出现明文')
        g = self.client.get('/api/settings/github-token')
        self.assertEqual(g.status_code, 200)
        self.assertTrue(g.json()['configured'])
        self.assertEqual(g.json()['source'], 'panel')
        self.assertNotIn('secret', g.text)

    def test_clear(self) -> None:
        self.client.put('/api/settings/github-token', json={'token': 'x'})
        r = self.client.put('/api/settings/github-token', json={'token': ''})
        self.assertFalse(r.json()['configured'])
        self.assertEqual(self._store.get('github_token'), '')

    def test_too_long_rejected(self) -> None:
        r = self.client.put('/api/settings/github-token', json={'token': 'x' * 600})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()

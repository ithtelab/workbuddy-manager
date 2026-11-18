"""自定义密钥形态（issue #447）的行为测试。

三件事必须钉住：

  1. **默认关闭**：没有任何人手动打开时，部署仍只有 `wbk_` 随机串 —— 这是既有
     安全预期。开关是显式选择加入，不是「新版本默认放开」。
  2. **弱口令拦截**：自定义形态的成本是把「生成随机串」换成「接受输入」，安全性
     不能跟着降级。但判据也不能太紧 —— `sk-valid-token-2026-prod-x` 这种正常
     形态必须能过（按**整段**比对弱词，不是子串）。
  3. **唯一性**：两把密钥指向同一 token 会让鉴权命中任意一行，额度与日志归属
     都变得不可预测，必须拒绝。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402


class TokenValidationTest(unittest.TestCase):
    """`_norm_custom_token` 的判据（纯函数层）。"""

    def test_accepts_gateway_style_tokens(self) -> None:
        """从别的网关迁过来的形态必须能用 —— 这正是这条 issue 的目的。"""
        for good in ('sk-prod-teamA-2026-xyz9', 'sk-valid-token-2026-prod-x',
                     'homelab-2026-autumn', 'team_a.prod_key-01'):
            self.assertEqual(keysvc._norm_custom_token(good), good)

    def test_strips_outer_whitespace_only(self) -> None:
        """首尾空白（复制带进来的）要削掉，中间字符**一个不动**。"""
        self.assertEqual(keysvc._norm_custom_token('  sk-prod-a-2026-x  '), 'sk-prod-a-2026-x')

    def test_too_short_rejected(self) -> None:
        for bad in ('sk-test', 'abcdefghijklmno'):   # 15 字符
            with self.assertRaises(ValueError, msg=bad):
                keysvc._norm_custom_token(bad)

    def test_too_long_rejected(self) -> None:
        with self.assertRaises(ValueError):
            keysvc._norm_custom_token('a' * 97 + 'B1')

    def test_bad_characters_rejected(self) -> None:
        """空格/冒号/换行会破坏 Authorization 头解析，或拼出额外的头。"""
        for bad in ('has space in it 12345', 'sk:a-very-long-token-2026',
                    'sk-token\nX-Evil: 1', 'sk-token-2026\ttab'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                keysvc._norm_custom_token(bad)

    def test_weak_tokens_rejected(self) -> None:
        for bad in ('sk-test-aaaaaaaaaaaa', '1234567890123456',
                    'aaaaaaaaaaaaaaaa', 'changeme-changeme'):
            with self.assertRaises(ValueError, msg=bad):
                keysvc._norm_custom_token(bad)

    def test_weak_word_inside_a_long_token_is_fine(self) -> None:
        """按整段比对，不做子串匹配 —— 否则 `sk-...token...` 这类正常形态会被误杀。"""
        self.assertEqual(keysvc._norm_custom_token('sk-token-2026-a1b2c3d4'),
                         'sk-token-2026-a1b2c3d4')
        self.assertEqual(keysvc._norm_custom_token('team-key-prod-9f8e7d6c'),
                         'team-key-prod-9f8e7d6c')


class CustomTokenEndpointTest(unittest.TestCase):
    """`POST /api/keys` 的三道关：开关 / 校验 / 唯一性。"""

    def setUp(self) -> None:
        db.connect()
        self.client = TestClient(self._app())
        self._login()
        self._orig_flag = config.KEY_CUSTOM_ENABLED

    def tearDown(self) -> None:
        config.KEY_CUSTOM_ENABLED = self._orig_flag

    @staticmethod
    def _app():
        from server.main import app
        return app

    def _login(self) -> None:
        cfg = security.load_users()
        admin = next((u for u in cfg.get('users') or [] if u.get('role') == 'admin'), None)
        self.assertIsNotNone(admin)
        self.client.cookies.set('wb_session', security.issue_token(admin['username'], ''))

    def test_disabled_by_default(self) -> None:
        """默认部署里自定义形态不可用 —— 这是刻意的安全默认值。"""
        config.KEY_CUSTOM_ENABLED = False
        r = self.client.post('/api/keys', json={
            'name': 'x', 'custom_token': 'sk-prod-teamA-2026-xyz9'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('WB_KEY_CUSTOM_ENABLED', r.json()['detail'])

    def test_enabled_creates_usable_key(self) -> None:
        config.KEY_CUSTOM_ENABLED = True
        token = 'sk-prod-teamA-2026-xyz9'
        r = self.client.post('/api/keys', json={'name': 'x', 'custom_token': token})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['key'], token)
        # 且真的能用来鉴权（前缀不再必须是 wbk_）
        u = self.client.get('/v1/usage', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(u.status_code, 200, u.text)

    def test_rejects_weak_token(self) -> None:
        config.KEY_CUSTOM_ENABLED = True
        r = self.client.post('/api/keys', json={'name': 'x', 'custom_token': 'sk-test'})
        self.assertEqual(r.status_code, 400)

    def test_rejects_duplicate(self) -> None:
        config.KEY_CUSTOM_ENABLED = True
        token = 'sk-prod-dup-check-2026'
        self.assertEqual(self.client.post('/api/keys', json={
            'name': 'a', 'custom_token': token}).status_code, 200)
        r = self.client.post('/api/keys', json={'name': 'b', 'custom_token': token})
        self.assertEqual(r.status_code, 409)

    def test_default_form_still_works(self) -> None:
        """不传 custom_token 时行为完全不变（默认路径零影响）。"""
        for flag in (False, True):
            config.KEY_CUSTOM_ENABLED = flag
            r = self.client.post('/api/keys', json={'name': f'default-{flag}'})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()['key'].startswith(keysvc.TOKEN_PREFIX))


class ResolveTest(unittest.TestCase):
    """`resolve()` 不再限定 `wbk_`，但仍要挡住明显无效的输入。"""

    def setUp(self) -> None:
        db.connect()

    def test_plain_wbk_key_resolves(self) -> None:
        created = keysvc.create_key('resolve-a')
        self.assertIsNotNone(keysvc.resolve(created['key']))

    def test_custom_key_resolves(self) -> None:
        created = keysvc.create_key('resolve-b', custom_token='sk-resolve-2026-ab-cd')
        self.assertIsNotNone(keysvc.resolve('sk-resolve-2026-ab-cd'))

    def test_garbage_rejected_without_query(self) -> None:
        for bad in ('', 'short', 'x' * 200, 'has space in it 12345'):
            self.assertIsNone(keysvc.resolve(bad), repr(bad))

    def test_unknown_token_returns_none(self) -> None:
        self.assertIsNone(keysvc.resolve('sk-never-created-2026-abcd'))


if __name__ == '__main__':
    unittest.main()

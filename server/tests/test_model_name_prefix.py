"""模型名不再带 `cn:` 前缀（用户要求「直接用腾讯自带的模型名字」）。

背景：上游 `/v1/models` 给国内模型统一加 `cn:` 前缀——那是**它自己的路由约定**
（`resolveModel` 按 `[realm:]model` 解析），不是模型的名字。管理端此前原样透出，
于是模型中心里显示成 `cn:deepseek-v4.1-flash`，用户照着这个名字去配客户端，
既难看又让人以为必须带着前缀写。

关键约束（**改错会静默路由到错误的账号池**）：只去 `cn:`，**绝不能动 `global:`**。
上游 `resolveModel`（internal/server/resolve_model.go）取第一个 `:` 前段，
恰为 cn/global 才剥离，**其余一律当裸名（= 国内版）**。所以：

  · 裸名 == 国内版  → 去掉 `cn:` 安全（存量客户端一直发裸名）
  · 裸名 != 国际版  → 去掉 `global:` 会让国际版模型被路由到国内池

第一版实现用了 `modelcatalog._strip_realm_prefix()`（它两种前缀都去），实测发现
国际版模型名变成裸名后不再可路由，故改为只去 `cn:`。下面的用例把这条钉住。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402
from server.routers.accounts import _strip_cn_prefix  # noqa: E402

# 上游真实形态：国内带 cn:，国际带 global:
UPSTREAM_MODELS = [
    {'id': 'cn:deepseek-v4.1-flash', 'object': 'model', 'name': 'DeepSeek-V4.1-Flash',
     'max_output_tokens': 64000},
    {'id': 'cn:glm-5.2', 'object': 'model', 'name': 'GLM-5.2', 'max_output_tokens': 64000},
    {'id': 'global:gpt-5.6-sol', 'object': 'model', 'name': 'GPT-5.6',
     'max_output_tokens': 64000},
]


class StripCnPrefixTest(unittest.TestCase):
    """纯函数：只去 cn:，保留 global:。"""

    def test_strips_cn_only(self) -> None:
        self.assertEqual(_strip_cn_prefix({'id': 'cn:glm-5.2'})['id'], 'glm-5.2')
        self.assertEqual(_strip_cn_prefix({'id': 'CN:glm-5.2'})['id'], 'glm-5.2')
        self.assertEqual(_strip_cn_prefix({'id': 'glm-5.2'})['id'], 'glm-5.2')

    def test_keeps_global_prefix(self) -> None:
        """`global:` 是国际版的**路由依据**，去掉就会被当成国内模型。"""
        for mid in ('global:gpt-5.6-sol', 'GLOBAL:gpt-5.6-sol'):
            self.assertEqual(_strip_cn_prefix({'id': mid})['id'], mid,
                             f'{mid} 的 global: 前缀被去掉了 —— 会路由到国内池')

    def test_does_not_mutate_input(self) -> None:
        src = {'id': 'cn:glm-5.2', 'name': 'GLM'}
        out = _strip_cn_prefix(src)
        self.assertEqual(src['id'], 'cn:glm-5.2', '原 dict 被改动了')
        self.assertEqual(out['name'], 'GLM', '其余字段要原样保留')

    def test_other_prefixes_untouched(self) -> None:
        """别的冒号（模型名里可能自带，如 vendor:model）不该被动。"""
        for mid in ('a:b', 'gpt:4o', 'kimi:k2.8'):
            self.assertEqual(_strip_cn_prefix({'id': mid})['id'], mid)

    def test_missing_or_bad_id_safe(self) -> None:
        for bad in ({}, {'id': None}, {'id': 123}, {'id': ''}):
            self.assertIsInstance(_strip_cn_prefix(bad), dict)


class ModelsEndpointTest(unittest.TestCase):
    """接口层：界面拿到的就是腾讯裸名，且两个版本仍能正确区分。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db, self._orig_users = config.DB_PATH, config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'm.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)
        r = self.client.post('/api/login', json={'username': 'admin', 'password': 'p'})
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE = self._orig_db, self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _get(self, realm: str) -> list[str]:
        async def fake() -> tuple[bool, list]:
            return True, UPSTREAM_MODELS
        with mock.patch('server.services.wb2api.get_models', fake):
            r = self.client.get(f'/api/models?realm={realm}')
        self.assertEqual(r.status_code, 200, r.text)
        return [m['id'] for m in r.json()['models']]

    def test_cn_models_have_no_prefix(self) -> None:
        ids = self._get('cn')
        self.assertEqual(ids, ['deepseek-v4.1-flash', 'glm-5.2'])
        for mid in ids:
            self.assertFalse(mid.startswith('cn:'), f'{mid} 仍带 cn: 前缀')

    def test_global_models_keep_prefix(self) -> None:
        """国际版必须保留 `global:`，否则上游会按裸名路由到国内池。"""
        ids = self._get('global')
        self.assertEqual(ids, ['global:gpt-5.6-sol'])

    def test_unscoped_lists_both_realms(self) -> None:
        ids = self._get('')
        self.assertIn('glm-5.2', ids)
        self.assertIn('global:gpt-5.6-sol', ids)
        self.assertNotIn('cn:glm-5.2', ids)

    def test_other_fields_survive(self) -> None:
        """去前缀只动 id，显示名等字段必须原样带出。"""
        async def fake() -> tuple[bool, list]:
            return True, UPSTREAM_MODELS
        with mock.patch('server.services.wb2api.get_models', fake):
            r = self.client.get('/api/models?realm=cn')
        first = r.json()['models'][0]
        self.assertEqual(first['name'], 'DeepSeek-V4.1-Flash')
        self.assertEqual(first['max_output_tokens'], 64000)


class KeyWhitelistCompatTest(unittest.TestCase):
    """模型白名单：去前缀后新旧写法必须互认。

    存量密钥的白名单里可能写着 `cn:glm-5.2`，而界面现在显示/推荐的是裸名
    `glm-5.2`。若按字面比对，改版后会出现「原本能用的密钥突然报模型不在
    白名单里」——而报错完全看不出是前缀差异导致的。
    """

    def _row(self, models: list[str]) -> dict:
        # 字段要与 keysvc._parse 的产物一致：validate 直接按键取值（不是 .get），
        # 缺列会 KeyError。往密钥行加字段时这里要跟着补（issue #27 就踩到过）。
        return {'id': 1, 'enabled': True, 'expires_at': 0, 'quota': 0,
                'used_tokens': 0, 'quota_credit': 0.0, 'used_credit': 0.0,
                'ip_allowlist': [], 'max_ips': 0,
                'realm': '', 'models': models}

    def test_both_spellings_accepted(self) -> None:
        pairs = [
            (['cn:glm-5.2'], 'glm-5.2'),   # 存量白名单（带前缀）← 新请求（裸名）
            (['glm-5.2'], 'cn:glm-5.2'),   # 新白名单（裸名）← 存量客户端（带前缀）
            (['glm-5.2'], 'glm-5.2'),
            (['cn:glm-5.2'], 'cn:glm-5.2'),
        ]
        for models, model in pairs:
            with self.subTest(models=models, model=model):
                self.assertIsNone(keysvc.validate(self._row(models), '1.2.3.4', model))

    def test_global_prefix_still_scoped(self) -> None:
        """显式 `global:` 只放行国际版；裸名视为「任意版本」（issue #357）。

        裸名那一侧曾经也是严格的，但密钥编辑器按版本分组、国际版组给的是裸名，
        勾选后存进白名单的也是裸名，而客户端调用要带 `global:` 前缀 —— 严格比对
        会让「列表里能选、一调用就 400」。裸名没有版本信息，解释成通配才符合
        用户意图；想只放行国际版，写 `global:` 前缀即可。
        """
        self.assertIsNotNone(keysvc.validate(self._row(['global:gpt-5']), '1.2.3.4', 'gpt-5'),
                             '国际版白名单不该放行裸名（会被路由到国内池）')
        self.assertIsNone(keysvc.validate(self._row(['gpt-5']), '1.2.3.4', 'global:gpt-5'),
                          '裸名白名单应放行国际版请求（issue #357）')

    def test_other_model_still_rejected(self) -> None:
        self.assertIsNotNone(keysvc.validate(self._row(['glm-5.2']), '1.2.3.4', 'kimi-k2.7'))


if __name__ == '__main__':
    unittest.main()

"""网关 /v1/models 的 provider 分支测试（C1/C2 修复的咬合点）。

覆盖（架构文档 B04 验收标准）：
  * qoder 上游 + global 密钥：拿到桥清单原样（C2——无前缀 id 恒归 cn 的误裁）；
  * qoder 清单不被腾讯目录裁掉（C1——qoder id 不在腾讯目录里，不跳过会全裁空）；
  * 白名单密钥只看到白名单内的 qoder 模型（R03：白名单维度保留）；
  * codebuddy 路径行为与改前快照一致（realm 裁剪 + 目录裁剪照常）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security, upstreamsvc  # noqa: E402
from server.services import modelcatalog  # noqa: E402

# qoder bridge 的真实响应形状（refs/qoder2api/internal/bridge/claude.go L315-370）
QODER_BRIDGE_MODELS = {
    'object': 'list',
    'data': [
        {'id': 'auto', 'object': 'model', 'owned_by': 'qoder'},
        {'id': 'qmodel_38max', 'object': 'model', 'owned_by': 'qoder'},
        {'id': 'qfmodel', 'object': 'model', 'owned_by': 'qoder'},
        {'id': 'kmodel', 'object': 'model', 'owned_by': 'qoder'},
    ],
}

CN_UPSTREAM_MODELS = {
    'object': 'list',
    'data': [
        {'id': 'cn:glm-5.2'},
        {'id': 'glm-5.1'},
        {'id': 'global:gpt-5.6-sol'},
    ],
}


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


def _client_returning(payload):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return _Resp(payload)
    return _Client()


class GatewayQoderModelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('pw')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.cookies.clear()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _qoder_upstream(self) -> dict:
        return upstreamsvc.create_upstream('Qoder 池', 'http://127.0.0.1:8963',
                                           api_key='bridge-tok', provider='qoder',
                                           realm='global')

    def _models(self, key: dict):
        return self.client.get('/v1/models',
                               headers={'Authorization': f"Bearer {key['key']}"})

    def test_global_key_sees_prefixless_bridge_models(self) -> None:
        """C2：qoder 模型无前缀、恒归 cn——global 密钥若按 realm 裁剪会拿空列表。"""
        up = self._qoder_upstream()
        key = keysvc.create_key('g', realm='global', upstream_id=up['id'])
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: _client_returning(QODER_BRIDGE_MODELS)):
            r = self._models(key)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([m['id'] for m in r.json()['data']],
                         ['auto', 'qmodel_38max', 'qfmodel', 'kmodel'],
                         '桥清单必须原样透传（版本隔离已由「密钥×上游」承担）')

    def test_models_not_pruned_by_tencent_catalog(self) -> None:
        """C1：腾讯目录里没有 qoder 模型，裁剪调用必须被跳过。"""
        up = self._qoder_upstream()
        key = keysvc.create_key('g', upstream_id=up['id'])

        async def _explode(realm):
            raise AssertionError('qoder 上游不得调用腾讯目录裁剪（C1）')

        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: _client_returning(QODER_BRIDGE_MODELS)), \
                mock.patch.object(modelcatalog, 'catalog', side_effect=_explode):
            r = self._models(key)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()['data']), 4)

    def test_whitelist_still_applies_to_qoder_models(self) -> None:
        """R03：清单维度保留白名单——白名单外的 qoder 模型照样裁。"""
        up = self._qoder_upstream()
        key = keysvc.create_key('g', upstream_id=up['id'],
                                models=['qmodel_38max'])
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: _client_returning(QODER_BRIDGE_MODELS)):
            r = self._models(key)
        self.assertEqual([m['id'] for m in r.json()['data']], ['qmodel_38max'])

    def test_codebuddy_path_snapshot_unchanged(self) -> None:
        """存量路径快照：codebuddy 上游的 realm 裁剪行为与改前逐字一致。"""
        up = upstreamsvc.create_upstream('CB 池', 'http://cb.example:7863')
        gl = keysvc.create_key('g', realm='global', upstream_id=up['id'])
        cn = keysvc.create_key('c', realm='cn', upstream_id=up['id'])
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: _client_returning(CN_UPSTREAM_MODELS)), \
                mock.patch.object(modelcatalog, 'catalog',
                                  side_effect=AssertionError('codebuddy 快照不该走到这里'
                                                             '（realm 均为空目录时跳过）')):
            r_gl = self._models(gl)
            r_cn = self._models(cn)
        self.assertEqual([m['id'] for m in r_gl.json()['data']], ['global:gpt-5.6-sol'])
        self.assertEqual([m['id'] for m in r_cn.json()['data']], ['cn:glm-5.2', 'glm-5.1'])


if __name__ == '__main__':
    unittest.main()

"""白名单校验认识 qoder 模型名（C5 修复）的契约测试。

覆盖（架构文档 B06 验收标准）：
  * 单元侧：`unknown_whitelist_entries(extra_known=…)` 的第三出口；
  * 默认参数（extra_known=None）行为逐字不变（存量签名兼容）；
  * 端点侧：bridge 缓存含 `qmodel` → 白名单填它不报 unknown；
  * 缓存未暖（None）→ 退化为原两清单判定；三来源全空 → `checked: false`。
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
from server.services import modelcatalog, qoder as qoder_svc  # noqa: E402


class UnknownWhitelistEntriesUnitTest(unittest.TestCase):
    def test_extra_known_short_circuits_realm_bucket(self) -> None:
        """qoder 模型名无前缀，会被前缀规则归进 cn 桶、拿腾讯清单判成 unknown——
        它必须在进 realm 判定**之前**被 extra_known 接住（C5）。"""
        known = {'cn': {'glm-5.2'}, 'global': {'gpt-5.6-sol'}}
        out = keysvc.unknown_whitelist_entries(
            ['qmodel', 'qfmodel'], known, extra_known={'qmodel', 'qfmodel'})
        self.assertEqual(out, [])
        # extra_known 之外的照旧报 unknown
        out = keysvc.unknown_whitelist_entries(['qmodel', 'typo-name'], known,
                                               extra_known={'qmodel'})
        self.assertEqual(out, ['typo-name'])

    def test_extra_known_accepts_prefixless_entry_with_prefix(self) -> None:
        out = keysvc.unknown_whitelist_entries(['cn:qmodel'], {},
                                               extra_known={'qmodel'})
        self.assertEqual(out, [], '白名单带前缀的写法也要能对上无前缀的桥 id')

    def test_default_extra_none_keeps_old_behavior(self) -> None:
        """默认参数 = 行为逐字不变（存量调用点/测试直调的签名兼容）。"""
        known = {'cn': None, 'global': {'gpt-5.6-sol'}}
        out = keysvc.unknown_whitelist_entries(['global:gpt-5.6-sol', 'qmodel'], known)
        # global 清单可判 → 该条认得；qmodel 落 cn 桶且 cn=None → 不判
        self.assertEqual(out, [])
        out = keysvc.unknown_whitelist_entries(['cn:glm-5.2'],
                                               {'cn': {'glm-5.2'}, 'global': None})
        self.assertEqual(out, [])


class CheckModelsQoderEndpointTest(unittest.TestCase):
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
        qoder_svc._model_cache.clear()
        # modelcatalog 的缓存是**进程级**单例：整段跑的时候其它测试可能已经暖过
        # 腾讯清单，本文件的「缓存未暖 → 退化/不校验」用例会被污染。这里显式清空，
        # 与「三来源全空」的前置条件对齐（test_qoder_service 同款处理）。
        modelcatalog._cache.clear()
        from server.main import app
        self.client = TestClient(app)
        r = self.client.post('/api/login', json={'username': 'admin', 'password': 'pw'})
        assert r.status_code == 200, r.text
        self.client.cookies.update(dict(r.cookies))

    def tearDown(self) -> None:
        self.client.cookies.clear()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        qoder_svc._model_cache.clear()
        modelcatalog._cache.clear()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _check(self, models: list[str]) -> dict:
        r = self.client.post('/api/keys/check-models', json={'models': models})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_warm_bridge_cache_accepts_qoder_names(self) -> None:
        qoder_svc._model_cache['http://127.0.0.1:8963'] = (
            frozenset({'qmodel', 'qfmodel'}), __import__('time').time())
        out = self._check(['qmodel', 'qfmodel'])
        self.assertTrue(out['checked'])
        self.assertEqual(out['unknown'], [])

    def test_cold_cache_degrades_to_realm_sources(self) -> None:
        """bridge 缓存未暖（None）→ 退化为原两清单判定。

        退化是有代价的：qmodel 落 cn 桶，若腾讯清单恰好暖着，它会被报成
        unknown（这正是 C5 要修的假警报——所以「设置 → Qoder」页要顺手暖
        缓存；这里钉的是「退化后行为与改造前一致」，不是退化后没有假警报）。
        """
        from server.services import modelcatalog
        self.assertIsNone(qoder_svc.cached_model_ids())
        with mock.patch.object(modelcatalog, 'cached_ids',
                               side_effect=lambda r: {'glm-5.2'} if r == 'cn' else None):
            out = self._check(['qmodel'])
        self.assertTrue(out['checked'])
        self.assertEqual(out['unknown'], ['qmodel'], '退化 = 按原两清单判定（与改造前一致）')

    def test_all_sources_missing_reports_unchecked(self) -> None:
        """三来源全空 → checked:false（如实显示「暂时无法校验」）。"""
        self.assertIsNone(qoder_svc.cached_model_ids())
        # 模型中心缓存也未暖：modelcatalog.cached_ids 返回 None
        out = self._check(['glm-5.2'])
        self.assertFalse(out['checked'])
        self.assertIn('reason', out)

    def test_tencent_source_still_flags_typos(self) -> None:
        """有腾讯清单时，白名单里真正的错字仍要报出来（修复不放宽存量判定）。"""
        from server.services import modelcatalog
        qoder_svc._model_cache['http://127.0.0.1:8963'] = (
            frozenset({'qmodel'}), __import__('time').time())
        with mock.patch.object(modelcatalog, 'cached_ids',
                               return_value={'glm-5.2'}):
            out = self._check(['glm-52-typo'])
        self.assertTrue(out['checked'])
        self.assertEqual(out['unknown'], ['glm-52-typo'])


if __name__ == '__main__':
    unittest.main()

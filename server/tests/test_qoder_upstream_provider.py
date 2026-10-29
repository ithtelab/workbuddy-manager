"""上游 provider（codebuddy | qoder）的数据层与解析层契约测试。

覆盖（架构文档 B01 验收标准）：
  * 迁移：旧库（无 provider/realm 列）升级后列存在，连跑两遍幂等；
  * 存量快照：codebuddy 行的 `_row_to_dict` / `resolve_for_key` 与升级前一致
    （新增键只增不改，判定不进新分支）；
  * qoder 上游建/改必填桥接 token（不硬编码任何默认值）；
  * 「密钥×上游」realm 匹配：global 密钥绑 cn 上游 → UpstreamUnavailable；
  * probe 对 qoder 上游打 /v1/models（bridge 没有 /healthz）。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, upstreamsvc  # noqa: E402


class _TempDbMixin:
    def _setup_db(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        db._conn = None
        db.connect()

    def _teardown_db(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


class MigrationTest(_TempDbMixin, unittest.TestCase):
    """迁移双路径：旧库 ALTER 补列 + 幂等可重放。"""

    def setUp(self) -> None:
        self._setup_db()

    def tearDown(self) -> None:
        self._teardown_db()

    def _drop_new_columns(self) -> None:
        """把新列去掉，模拟「上一个版本建的库」。

        SQLite 3.35+ 支持 DROP COLUMN；老环境退化成重建表（列少，直接做）。
        """
        cols = [r[1] for r in db._conn.execute('PRAGMA table_info(upstreams)')]
        if 'provider' in cols:
            db._conn.execute('ALTER TABLE upstreams DROP COLUMN provider')
        if 'realm' in cols:
            db._conn.execute('ALTER TABLE upstreams DROP COLUMN realm')
        db._conn.commit()

    def test_migration_adds_columns_and_is_idempotent(self) -> None:
        self._drop_new_columns()
        cols = {r[1] for r in db._conn.execute('PRAGMA table_info(upstreams)')}
        self.assertNotIn('provider', cols)
        db._migrate(db._conn)
        cols = {r[1] for r in db._conn.execute('PRAGMA table_info(upstreams)')}
        self.assertIn('provider', cols)
        self.assertIn('realm', cols)
        # 连跑两遍：第二条路径（已存在的库再启动）不该报错，也不该重复加列
        db._migrate(db._conn)
        cols = {r[1] for r in db._conn.execute('PRAGMA table_info(upstreams)')}
        self.assertEqual(sum(1 for c in cols if c == 'provider'), 1)

    def test_existing_rows_get_defaults(self) -> None:
        self._drop_new_columns()
        db._conn.execute(
            "INSERT INTO upstreams(name, base_url, api_key, created_at, updated_at) "
            "VALUES('旧分组', 'http://a.example', '', 1, 1)")
        db._conn.commit()
        db._migrate(db._conn)
        row = db.query_one('SELECT * FROM upstreams WHERE name = ?', ('旧分组',))
        # 存量行回填默认值：读侧 `or 'codebuddy'` 兜底之后恒为 codebuddy
        self.assertEqual(row['provider'], 'codebuddy')
        self.assertEqual(row['realm'], '')


class ExistingBehaviorSnapshotTest(_TempDbMixin, unittest.TestCase):
    """存量 codebuddy 行为零回归的快照断言。"""

    def setUp(self) -> None:
        self._setup_db()

    def tearDown(self) -> None:
        self._teardown_db()

    def test_row_to_dict_shape_unchanged_plus_new_keys(self) -> None:
        created = upstreamsvc.create_upstream('A组', 'http://a.example')
        self.assertEqual(created['provider'], 'codebuddy')
        self.assertEqual(created['realm'], '')
        # 原有键一个不少
        for k in ('id', 'name', 'base_url', 'api_key', 'note', 'enabled',
                  'auth_dir', 'container', 'created_at', 'updated_at', 'is_default'):
            self.assertIn(k, created)

    def test_default_upstream_is_codebuddy(self) -> None:
        d = upstreamsvc.default_upstream()
        self.assertEqual(d['provider'], 'codebuddy')
        self.assertEqual(d['realm'], '')
        # 未绑定密钥的解析结果不变
        self.assertEqual(upstreamsvc.resolve_for_key({'upstream_id': None})['provider'],
                         'codebuddy')

    def test_codebuddy_realm_never_blocks(self) -> None:
        """codebuddy 上游 + 任意密钥版本：不进「密钥×上游」匹配（存量行为不变）。"""
        up = upstreamsvc.create_upstream('A', 'http://a.example')
        for realm in ('', 'cn', 'global'):
            key = keysvc.create_key('t', realm=realm, upstream_id=up['id'])
            row = upstreamsvc.resolve_for_key({'upstream_id': up['id'],
                                               'realm': key['realm']})
            self.assertEqual(row['id'], up['id'])


class QoderUpstreamValidationTest(_TempDbMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._setup_db()

    def tearDown(self) -> None:
        self._teardown_db()

    def test_create_qoder_requires_bridge_token(self) -> None:
        """R02：qoder 上游必须显式填桥接 token——不硬编码默认值（铁律 4）。"""
        with self.assertRaises(ValueError):
            upstreamsvc.create_upstream('Q', 'http://127.0.0.1:8963',
                                        provider='qoder')
        created = upstreamsvc.create_upstream('Q', 'http://127.0.0.1:8963',
                                              api_key='my-bridge-token',
                                              provider='qoder')
        self.assertEqual(created['provider'], 'qoder')
        self.assertEqual(created['api_key'], 'my-bridge-token')

    def test_create_rejects_bad_provider_and_realm(self) -> None:
        """类型是结构性选择：非法值报错，不静默兜底（猜一个等于埋雷）。"""
        with self.assertRaises(ValueError):
            upstreamsvc.create_upstream('X', 'http://a.example', provider='qoder2')
        # 大小写与首尾空白被归一化（合法输入，不拒）
        self.assertEqual(upstreamsvc.create_upstream(
            'X', 'http://a.example', provider='CodeBuddy ')['provider'], 'codebuddy')
        with self.assertRaises(ValueError):
            upstreamsvc.create_upstream('Q', 'http://a.example', api_key='t',
                                        provider='qoder', realm='国内')

    def test_update_to_qoder_requires_token(self) -> None:
        """PATCH 只改 provider 不带 token 也要拦：静默生效会让转发全 401。"""
        up = upstreamsvc.create_upstream('A', 'http://a.example')
        with self.assertRaises(ValueError):
            upstreamsvc.update_upstream(up['id'], {'provider': 'qoder'})
        # 带 token 即可改成 qoder；版本归属可单独清回「不限」
        updated = upstreamsvc.update_upstream(
            up['id'], {'provider': 'qoder', 'api_key': 'tok',
                       'realm': 'global'})
        self.assertEqual(updated['provider'], 'qoder')
        self.assertEqual(updated['realm'], 'global')
        updated = upstreamsvc.update_upstream(up['id'], {'realm': ''})
        self.assertEqual(updated['realm'], '', "显式传 '' = 清空版本归属，必须生效")


class KeyUpstreamRealmMatchTest(_TempDbMixin, unittest.TestCase):
    """「密钥×上游」版本匹配（仅 qoder 上游消费）。"""

    def setUp(self) -> None:
        self._setup_db()

    def tearDown(self) -> None:
        self._teardown_db()

    def test_global_key_on_cn_upstream_rejected(self) -> None:
        up = upstreamsvc.create_upstream('Q-cn', 'http://127.0.0.1:8963',
                                         api_key='tok', provider='qoder',
                                         realm='cn')
        with self.assertRaises(upstreamsvc.UpstreamUnavailable) as ctx:
            upstreamsvc.resolve_for_key({'upstream_id': up['id'], 'realm': 'global'})
        self.assertIn('不匹配', str(ctx.exception))

    def test_matching_realm_and_unlimited_pass(self) -> None:
        up = upstreamsvc.create_upstream('Q-cn', 'http://127.0.0.1:8963',
                                         api_key='tok', provider='qoder',
                                         realm='cn')
        # 同版本放行
        row = upstreamsvc.resolve_for_key({'upstream_id': up['id'], 'realm': 'cn'})
        self.assertEqual(row['id'], up['id'])
        # 密钥不限版本 → 放行（key.realm == '' 放行一切）
        row = upstreamsvc.resolve_for_key({'upstream_id': up['id'], 'realm': ''})
        self.assertEqual(row['id'], up['id'])
        # 上游不限版本 → 放行（upstream.realm 为空表示该池不区分）
        up2 = upstreamsvc.create_upstream('Q-any', 'http://127.0.0.1:8964',
                                          api_key='tok', provider='qoder')
        row = upstreamsvc.resolve_for_key({'upstream_id': up2['id'],
                                           'realm': 'global'})
        self.assertEqual(row['id'], up2['id'])


class QoderProbeTest(_TempDbMixin, unittest.TestCase):
    """probe 对 qoder 上游打 /v1/models（bridge 没有实现 /healthz）。"""

    def setUp(self) -> None:
        self._setup_db()

    def tearDown(self) -> None:
        self._teardown_db()

    class _Resp:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    def _fake_client(self, sent: list, status_code: int = 200):
        resp = self._Resp(status_code)
        outer = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kw):
                sent.append(('GET', url, kw.get('headers') or {}))
                return resp
        return _Client()

    def test_qoder_probe_hits_models_with_bearer(self) -> None:
        up = upstreamsvc.create_upstream('Q', 'http://127.0.0.1:8963',
                                         api_key='bridge-tok', provider='qoder')
        sent: list = []
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: self._fake_client(sent)):
            ok, msg = self._run(upstreamsvc.probe(up))
        self.assertTrue(ok, msg)
        self.assertEqual(sent[0][1], 'http://127.0.0.1:8963/v1/models')
        self.assertEqual(sent[0][2].get('Authorization'), 'Bearer bridge-tok')

    def test_qoder_probe_401_reports_token(self) -> None:
        up = upstreamsvc.create_upstream('Q', 'http://127.0.0.1:8963',
                                         api_key='bad', provider='qoder')
        sent: list = []
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: self._fake_client(sent, 401)):
            ok, msg = self._run(upstreamsvc.probe(up))
        self.assertFalse(ok)
        self.assertIn('token', msg)

    def test_codebuddy_probe_still_hits_healthz(self) -> None:
        up = upstreamsvc.create_upstream('A', 'http://a.example')
        sent: list = []
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: self._fake_client(sent)):
            ok, msg = self._run(upstreamsvc.probe(up))
        self.assertTrue(ok, msg)
        self.assertEqual(sent[0][1], 'http://a.example/healthz')

    @staticmethod
    def _run(coro):
        import asyncio
        return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == '__main__':
    unittest.main()

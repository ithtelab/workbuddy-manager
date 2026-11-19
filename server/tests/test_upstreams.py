"""多上游（账号池分组）的契约测试。

覆盖三层，每层盯一个真实会出错的点：

  1. **配置语义**（upstreamsvc）：默认上游来自配置而不是数据库；地址归一化；
     被密钥引用的上游拒绝删除。
  2. **密钥绑定**：建密钥时绑了不存在的上游要当场 400 —— 否则症状是「密钥建好了，
     但每条请求都 503」，管理员对着 503 排查建密钥的那一刻，方向完全跑偏。
  3. **转发真的走对应上游**（本功能的执行点）：用假上游记录**实际请求的地址**，
     断言绑定了上游的密钥发到它自己的地址，未绑定的密钥发到默认地址。
     这一条是「静默回落 = 隔离失效」的咬合点——只断言状态码是抓不住的。
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


class _TempDbMixin:
    """临时库 + 用户表的公共脚手架（与既有测试同口径）。"""

    def _setup_db(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()

    def _teardown_db(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


# ── 1) 配置语义 ────────────────────────────────────────────
class UpstreamServiceTest(_TempDbMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._setup_db()

    def tearDown(self) -> None:
        self._teardown_db()

    def test_default_upstream_comes_from_config(self) -> None:
        """默认上游是运行时读配置的结果，不是库里的一行——存量部署升级零改动。"""
        with mock.patch.object(config, 'WB2API_BASE', 'http://up.example:7863'), \
                mock.patch.object(config, 'upstream_api_key', lambda: 'k-default'):
            d = upstreamsvc.default_upstream()
        self.assertIsNone(d['id'], '默认上游不该有数据库 id')
        self.assertTrue(d['is_default'])
        self.assertEqual(d['base_url'], 'http://up.example:7863')
        self.assertEqual(d['api_key'], 'k-default')

    def test_list_puts_default_first(self) -> None:
        upstreamsvc.create_upstream('A组', 'http://a.example:7863')
        items = upstreamsvc.list_upstreams()
        self.assertTrue(items[0]['is_default'], '默认上游必须排第一（前端下拉顺序）')
        self.assertEqual([i['name'] for i in items[1:]], ['A组'])

    def test_base_url_normalization_and_validation(self) -> None:
        """末尾斜杠去掉（转发是字符串拼接，多一个斜杠就是另一个 URL）；非 http(s) 当场拒。"""
        created = upstreamsvc.create_upstream('A', 'http://a.example:7863/')
        self.assertEqual(created['base_url'], 'http://a.example:7863')
        for bad in ('a.example:7863', 'ftp://a.example', '', '   '):
            with self.assertRaises(ValueError, msg=f'{bad!r} 应该被拒绝'):
                upstreamsvc.create_upstream('X', bad)
        with self.assertRaises(ValueError):
            upstreamsvc.create_upstream('   ', 'http://a.example')

    def test_update_partial_and_clear_api_key(self) -> None:
        """PATCH 语义：只改传进来的字段；显式传空串要能清空 api_key。"""
        created = upstreamsvc.create_upstream('A', 'http://a.example', api_key='secret')
        updated = upstreamsvc.update_upstream(created['id'], {'note': '备用池'})
        self.assertEqual(updated['api_key'], 'secret', '没传的字段不该被清掉')
        self.assertEqual(updated['note'], '备用池')
        updated = upstreamsvc.update_upstream(created['id'], {'api_key': ''})
        self.assertEqual(updated['api_key'], '', '显式清空 api_key 必须生效')
        self.assertIsNone(upstreamsvc.update_upstream(9999, {'name': 'x'}), '不存在的 id 返回 None')

    def test_delete_refused_when_key_bound(self) -> None:
        up = upstreamsvc.create_upstream('A', 'http://a.example')
        key = keysvc.create_key('t', upstream_id=up['id'])
        deleted, used = upstreamsvc.delete_upstream(up['id'])
        self.assertFalse(deleted, '有密钥绑定时不该删掉')
        self.assertEqual(used, 1)
        # 解绑之后才能删
        keysvc.update_key(key['id'], {'upstream_id': None})
        deleted, used = upstreamsvc.delete_upstream(up['id'])
        self.assertTrue(deleted)
        self.assertEqual(used, 0)

    def test_key_counts_includes_default(self) -> None:
        up = upstreamsvc.create_upstream('A', 'http://a.example')
        keysvc.create_key('d1')
        keysvc.create_key('d2')
        keysvc.create_key('a1', upstream_id=up['id'])
        counts = upstreamsvc.key_counts()
        self.assertEqual(counts.get('default'), 2, '未绑定上游的密钥算在默认上游头上')
        self.assertEqual(counts.get(up['id']), 1)

    def test_resolve_for_key(self) -> None:
        up = upstreamsvc.create_upstream('A', 'http://a.example')
        with mock.patch.object(config, 'WB2API_BASE', 'http://default.example'), \
                mock.patch.object(config, 'upstream_api_key', lambda: 'k-default'):
            self.assertEqual(upstreamsvc.resolve_for_key(None)['base_url'], 'http://default.example')
            self.assertEqual(upstreamsvc.resolve_for_key({'upstream_id': None})['base_url'],
                             'http://default.example')
            bound = upstreamsvc.resolve_for_key({'upstream_id': up['id']})
            self.assertEqual(bound['base_url'], 'http://a.example')
            self.assertEqual(bound['api_key'], '')

            # 停用 → 明确报错（不静默回落）
            upstreamsvc.update_upstream(up['id'], {'enabled': False})
            with self.assertRaises(upstreamsvc.UpstreamUnavailable) as ctx:
                upstreamsvc.resolve_for_key({'upstream_id': up['id']})
            self.assertIn('已停用', str(ctx.exception))

            # 删除 → 同样明确报错
            upstreamsvc.update_upstream(up['id'], {'enabled': True})
            upstreamsvc.delete_upstream(up['id'])
            with self.assertRaises(upstreamsvc.UpstreamUnavailable) as ctx:
                upstreamsvc.resolve_for_key({'upstream_id': up['id']})
            self.assertIn('不存在', str(ctx.exception))


# ── 2) 密钥绑定（走真实 HTTP 路由）──────────────────────────
class UpstreamApiE2ETest(_TempDbMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._setup_db()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('pw')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)
        r = self.client.post('/api/login', json={'username': 'admin', 'password': 'pw'})
        assert r.status_code == 200, r.text
        self.client.cookies.update(dict(r.cookies))

    def tearDown(self) -> None:
        self.client.cookies.clear()
        self._teardown_db()

    def _create_upstream(self, name: str, base: str) -> dict:
        r = self.client.post('/api/upstreams', json={'name': name, 'base_url': base})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_list_create_update_delete(self) -> None:
        r = self.client.get('/api/upstreams')
        self.assertEqual(r.status_code, 200, r.text)
        items = r.json()['items']
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]['is_default'])
        self.assertIn('bound_keys', items[0])

        created = self._create_upstream('A组', 'http://a.example:7863/')
        self.assertEqual(created['base_url'], 'http://a.example:7863')

        r = self.client.patch(f"/api/upstreams/{created['id']}", json={'enabled': False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()['enabled'])

        r = self.client.delete(f"/api/upstreams/{created['id']}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.client.get('/api/upstreams').json()['items']), 1)

    def test_create_rejects_bad_url(self) -> None:
        r = self.client.post('/api/upstreams', json={'name': 'X', 'base_url': 'a.example'})
        self.assertEqual(r.status_code, 400, r.text)

    def test_delete_conflicts_when_key_bound(self) -> None:
        up = self._create_upstream('A组', 'http://a.example')
        key = keysvc.create_key('t', upstream_id=up['id'])
        r = self.client.delete(f"/api/upstreams/{up['id']}")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn('还有 1 把密钥', r.json()['detail'])
        # 解绑后即可删除
        keysvc.update_key(key['id'], {'upstream_id': None})
        self.assertEqual(self.client.delete(f"/api/upstreams/{up['id']}").status_code, 200)

    def test_key_create_with_unknown_upstream_is_400(self) -> None:
        r = self.client.post('/api/keys', json={'name': 't', 'upstream_id': 9999})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('不存在', r.json()['detail'])

    def test_key_can_bind_and_unbind(self) -> None:
        up = self._create_upstream('A组', 'http://a.example')
        r = self.client.post('/api/keys', json={'name': 't', 'upstream_id': up['id']})
        self.assertEqual(r.status_code, 200, r.text)
        kid = r.json()['id']
        self.assertEqual(r.json()['upstream_id'], up['id'])
        listed = {k['id']: k for k in self.client.get('/api/keys').json()}
        self.assertEqual(listed[kid]['upstream_id'], up['id'])
        r = self.client.patch(f'/api/keys/{kid}', json={'upstream_id': None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()['upstream_id'], '显式解绑要能回到默认上游')


# ── 3) 转发按密钥走对应上游 ─────────────────────────────────
class UpstreamRoutingTest(_TempDbMixin, unittest.TestCase):
    """本功能的执行点：请求真的发到了密钥绑定的那个上游地址。"""

    def setUp(self) -> None:
        self._setup_db()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('pw')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.cookies.clear()
        self._teardown_db()

    class _Resp:
        status_code = 200

        def json(self):
            return {'id': 'x', 'choices': [], 'usage': {}}

    def _fake_client(self, sent: list):
        resp = self._Resp()
        outer = self

        class _Client:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a):
                return False

            async def post(self_, url, **kw):
                sent.append(('POST', url, kw.get('headers') or {}))
                return resp

            async def get(self_, url, **kw):
                sent.append(('GET', url, kw.get('headers') or {}))
                return resp
        return _Client()

    def _chat(self, token: str):
        return self.client.post(
            '/v1/chat/completions',
            headers={'Authorization': f'Bearer {token}'},
            json={'model': 'glm-5.2', 'messages': [{'role': 'user', 'content': 'hi'}],
                  'stream': False},
        )

    def test_routes_to_bound_upstream_and_default_otherwise(self) -> None:
        up = upstreamsvc.create_upstream('A组', 'http://bound.example:7863', api_key='k-bound')
        bound_key = keysvc.create_key('bound', upstream_id=up['id'])
        default_key = keysvc.create_key('default')

        sent: list = []
        with mock.patch.object(config, 'WB2API_BASE', 'http://default.example:7863'), \
                mock.patch.object(config, 'http_client', lambda *a, **k: self._fake_client(sent)):
            r1 = self._chat(bound_key['key'])
            r2 = self._chat(default_key['key'])

        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(sent[0][1], 'http://bound.example:7863/v1/chat/completions',
                         '绑定了上游的密钥必须发到它自己的地址')
        self.assertEqual(sent[0][2].get('Authorization'), 'Bearer k-bound',
                         '必须用该上游的 api_key（拿错 key 等于打到别的账号池）')
        self.assertEqual(sent[1][1], 'http://default.example:7863/v1/chat/completions',
                         '未绑定的密钥走默认上游')

    def test_disabled_upstream_errors_and_never_calls_default(self) -> None:
        """绑定的上游停用 → 503 且**不许**回落到默认上游（静默回落 = 隔离失效）。"""
        up = upstreamsvc.create_upstream('A组', 'http://bound.example:7863')
        upstreamsvc.update_upstream(up['id'], {'enabled': False})
        key = keysvc.create_key('bound', upstream_id=up['id'])

        sent: list = []
        with mock.patch.object(config, 'WB2API_BASE', 'http://default.example:7863'), \
                mock.patch.object(config, 'http_client', lambda *a, **k: self._fake_client(sent)):
            r = self._chat(key['key'])

        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn('已停用', r.json()['error']['message'])
        self.assertEqual(sent, [], '一次都不该发出去——既不该发绑定上游，更不该回落默认上游')

    def test_models_use_bound_upstream(self) -> None:
        up = upstreamsvc.create_upstream('A组', 'http://bound.example:7863')
        key = keysvc.create_key('bound', upstream_id=up['id'])
        sent: list = []
        with mock.patch.object(config, 'WB2API_BASE', 'http://default.example:7863'), \
                mock.patch.object(config, 'http_client', lambda *a, **k: self._fake_client(sent)):
            r = self.client.get('/v1/models', headers={'Authorization': f"Bearer {key['key']}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sent[0][1], 'http://bound.example:7863/v1/models')

    def test_empty_key_same_address_inherits_default_key(self) -> None:
        """分组只填名称（api_key 留空、地址=默认分组）→ 转发沿用默认上游那把钥匙。

        不沿用的话，绑定这类分组的密钥一调用就吃上游 401 —— 而「只填名称」正是
        账号页「添加分组」的默认形态，见 upstreamsvc.forward_api_key。
        """
        up = upstreamsvc.create_upstream('简称组', 'http://default.example:7863')
        key = keysvc.create_key('g', upstream_id=up['id'])

        sent: list = []
        with mock.patch.object(config, 'WB2API_BASE', 'http://default.example:7863'), \
                mock.patch.object(config, 'upstream_api_key', lambda: 'k-default'), \
                mock.patch.object(config, 'http_client', lambda *a, **k: self._fake_client(sent)):
            r = self._chat(key['key'])

        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sent[0][1], 'http://default.example:7863/v1/chat/completions')
        self.assertEqual(sent[0][2].get('Authorization'), 'Bearer k-default',
                         '留空 + 同址 = 沿用默认上游的钥匙，而不是不带鉴权头')

    def test_empty_key_other_address_sends_no_auth_header(self) -> None:
        """留空 + 不同址 → 不带鉴权头：支持不鉴权的自建实例，也别把默认钥匙发过去。"""
        up = upstreamsvc.create_upstream('外部组', 'http://other.example:7863')
        key = keysvc.create_key('g2', upstream_id=up['id'])

        sent: list = []
        with mock.patch.object(config, 'WB2API_BASE', 'http://default.example:7863'), \
                mock.patch.object(config, 'upstream_api_key', lambda: 'k-default'), \
                mock.patch.object(config, 'http_client', lambda *a, **k: self._fake_client(sent)):
            r = self._chat(key['key'])

        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sent[0][1], 'http://other.example:7863/v1/chat/completions')
        self.assertNotIn('Authorization', sent[0][2],
                         '地址不同的分组留空 = 不带鉴权头')


if __name__ == '__main__':
    unittest.main()

class ApiKeyNeverReturnedTest(unittest.TestCase):
    """上游凭据绝不明文回前端（评审补）。

    这一组接口的**列表只要登录**（含只读账号），而 api_key 与上游 config.json 里的
    同等敏感——明文回传等于把它发给每一个登录用户。评审时实测过：没有这层脱敏时
    create 与 list 的响应里都是明文。

    判据用**哨兵值**扫所有响应体，而不是只看某个字段名：字段改名、多一层嵌套、
    将来新增接口（比如详情）都不该让明文漏出去。
    """

    SECRET = 'SENTINEL-UPSTREAM-KEY-8f3a'

    def setUp(self) -> None:
        import json as _json
        import tempfile
        from pathlib import Path as _P
        from fastapi.testclient import TestClient
        from server import config, db, security
        self._tmp = tempfile.TemporaryDirectory()
        d = _P(self._tmp.name)
        self._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'u.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(_json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('pw')},
                      {'username': 'viewer', 'role': 'viewer',
                       'pwd_hash': security.make_hash('pw')}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        self.c = TestClient(app)

    def tearDown(self) -> None:
        from server import config, db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _login(self, who: str) -> None:
        r = self.c.post('/api/login', json={'username': who, 'password': 'pw'})
        self.assertEqual(r.status_code, 200, r.text)
        self.c.cookies.update(dict(r.cookies))

    def test_no_endpoint_returns_the_plaintext_key(self) -> None:
        import json as _json
        self._login('admin')
        created = self.c.post('/api/upstreams', json={
            'name': 'A', 'base_url': 'http://a.example', 'api_key': self.SECRET})
        self.assertEqual(created.status_code, 200, created.text)
        uid = created.json()['id']
        responses = {
            'create': created.json(),
            'list': self.c.get('/api/upstreams').json(),
            'patch': self.c.patch(f'/api/upstreams/{uid}', json={'note': 'x'}).json(),
            'probe': self.c.post(f'/api/upstreams/{uid}/probe').json(),
        }
        for where, body in responses.items():
            with self.subTest(where=where):
                self.assertNotIn(self.SECRET, _json.dumps(body, ensure_ascii=False),
                                 f'{where} 的响应里出现了明文 api_key')
        # 前端仍要能显示「已配置」与脱敏值
        self.assertTrue(responses['create']['has_key'])
        self.assertTrue(responses['create']['api_key_masked'])
        self.assertNotIn(self.SECRET, responses['create']['api_key_masked'])

    def test_viewer_cannot_read_the_key_either(self) -> None:
        """只读账号能看列表（与密钥列表同口径），但同样看不到明文。"""
        import json as _json
        self._login('admin')
        self.c.post('/api/upstreams', json={'name': 'A', 'base_url': 'http://a.example',
                                            'api_key': self.SECRET})
        self.c.cookies.clear()
        self._login('viewer')
        r = self.c.get('/api/upstreams')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(self.SECRET, _json.dumps(r.json(), ensure_ascii=False))


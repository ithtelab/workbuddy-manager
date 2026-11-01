"""密钥明文回看（「再复制一次」，POST /api/keys/{id}/reveal）。

三条必须守住的性质：

  1. **明文只从这一条接口出去**：列表接口只读账号（viewer）也能调，它必须
     永远不带明文；回看要会话管理员，且每次都留审计（不含明文本身）。
  2. **取不回来要说清怎么办**：保存明文之前建的存量密钥只有哈希，回 409 并
     说明「新建一把替换」，而不是回一个空串让界面复制出空文本。
  3. **红包密钥不能反成另一样**：它们的明文本来就在 `red_packet_shares.token`
     里（见 db.py），手工建的能取回、红包发的取不回会让人以为功能坏了。

顺带钉住：明文列不随异地备份（pgsync）搬到远程 PG —— 那份库可能在别人手里。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402
from server.routers import keys as keys_router  # noqa: E402
from server.services import pgsync  # noqa: E402

ADMIN = {'username': 'admin', 'role': 'admin'}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / 'keys.db'
        patch = mock.patch.object(config, 'DB_PATH', self.db_path)
        patch.start()
        self.addCleanup(patch.stop)
        db._conn = None
        db.connect()
        self.addCleanup(self._close_db)

        app = FastAPI()
        app.include_router(keys_router.router)
        app.dependency_overrides[security.require_admin] = lambda: ADMIN
        app.dependency_overrides[security.current_user] = lambda: ADMIN
        self.client = TestClient(app)

    def _close_db(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None


class RevealKeyApiTest(_Base):
    def test_create_then_reveal_returns_the_same_key(self) -> None:
        created = self.client.post('/api/keys', json={'name': '客服组', 'realm': 'cn'})
        self.assertEqual(created.status_code, 200, created.text)
        key = created.json()['key']
        self.assertTrue(key.startswith('wbk_'))

        revealed = self.client.post(f'/api/keys/{created.json()["id"]}/reveal')
        self.assertEqual(revealed.status_code, 200, revealed.text)
        self.assertEqual(revealed.json()['key'], key, '取回的不是创建时那把密钥')
        self.assertEqual(revealed.json()['name'], '客服组')

    def test_list_never_carries_the_plaintext(self) -> None:
        """列表对只读账号开放 —— 明文一旦进列表，等于把「看全部密钥」发了出去。"""
        created = self.client.post('/api/keys', json={'name': 'a', 'realm': 'cn'}).json()
        listed = self.client.get('/api/keys')
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertNotIn(created['key'], listed.text, '列表里出现了完整密钥')
        self.assertNotIn('key_plain', listed.text)
        # 前缀照旧显示（那是给用户认钥匙用的），但只有前 12 位
        for row in listed.json():
            self.assertNotIn('key', row, '列表里出现了明文密钥字段')
            self.assertEqual(row['prefix'], created['key'][:12])

    def test_reveal_is_audited_without_the_secret(self) -> None:
        created = self.client.post('/api/keys', json={'name': 'audit-me', 'realm': 'cn'})
        self.client.post(f'/api/keys/{created.json()["id"]}/reveal')
        rows = db.query("SELECT action, target, detail FROM audit_logs WHERE action = 'reveal_key'")
        self.assertEqual(len(rows), 1, '查看明文没有留审计')
        detail = rows[0]['detail'] or ''
        self.assertIn('前缀=', detail)
        self.assertNotIn(created.json()['key'], detail, '审计日志里写进了明文')

    def test_reveal_unknown_id_is_404(self) -> None:
        self.assertEqual(self.client.post('/api/keys/99999/reveal').status_code, 404)

    def test_legacy_key_says_rebuild_instead_of_empty(self) -> None:
        """保存明文之前建的密钥：库里只有哈希，必须说清「新建一把替换」。"""
        created = self.client.post('/api/keys', json={'name': 'old', 'realm': 'cn'})
        key_id = created.json()['id']
        db.execute("UPDATE api_keys SET key_plain = '' WHERE id = ?", (key_id,))

        response = self.client.post(f'/api/keys/{key_id}/reveal')
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()['detail']
        self.assertIn('无法再取回', detail)
        self.assertIn('新建', detail, '没说清用户下一步该干什么')

    def test_packet_key_falls_back_to_its_share_row(self) -> None:
        """红包密钥的明文存在 red_packet_shares.token 里，回看要能取到。"""
        created = self.client.post('/api/keys', json={'name': 'packet', 'realm': 'cn'})
        key_id = created.json()['id']
        token = created.json()['key']
        db.execute("UPDATE api_keys SET key_plain = '' WHERE id = ?", (key_id,))
        db.execute('INSERT INTO red_packets(title, quota_kind, total_amount, shares, mode, '
                   'models, created_by, created_at, expires_at) '
                   'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   ('t', 'token', 10, 1, 'even', '[]', 'admin',
                    int(time.time()), int(time.time()) + 3600))
        packet_id = db.query_one('SELECT id FROM red_packets ORDER BY id DESC LIMIT 1')['id']
        db.execute('INSERT INTO red_packet_shares(packet_id, key_id, amount, token) '
                   'VALUES(?, ?, ?, ?)', (packet_id, key_id, 10, token))

        revealed = self.client.post(f'/api/keys/{key_id}/reveal')
        self.assertEqual(revealed.status_code, 200, revealed.text)
        self.assertEqual(revealed.json()['key'], token)

    def test_reveal_requires_session_admin(self) -> None:
        """权限必须比「列表」严：读明文与改密钥同一档（会话管理员）。

        这条按源码钉住 —— 依赖被换掉时功能测试仍会通过（那里 override 了
        `require_admin`），只有源码形状能保证线上真的是管理员才能看。
        """
        source = (Path(__file__).resolve().parents[1] / 'routers' / 'keys.py'
                  ).read_text(encoding='utf-8')
        block = source.split("@router.post('/{key_id}/reveal')", 1)[1].split(') -> dict:', 1)[0]
        self.assertIn('require_admin', block, '回看明文的接口放宽了权限')


class RevealKeyBackupTest(unittest.TestCase):
    """明文不跟着异地备份走（镜像可能在别人的云 PG 里）。"""

    def test_key_plain_is_blanked_when_mirroring(self) -> None:
        columns = ['id', 'name', 'key_hash', 'key_plain', 'prefix']
        rows = [(1, 'a', 'hash-a', 'wbk_real_secret', 'wbk_real')]
        out = pgsync._filter_rows('api_keys', columns, rows)
        self.assertEqual(out, [(1, 'a', 'hash-a', '', 'wbk_real')],
                         '备份里仍然带着明文密钥')

    def test_other_tables_are_untouched(self) -> None:
        rows = [(1, 'x')]
        self.assertEqual(pgsync._filter_rows('api_keys_ips', ['a', 'b'], rows), rows)

    def test_old_mirror_without_the_column_still_syncs(self) -> None:
        """列还不存在时（老库 / 老镜像）不要整表丢掉，只照原样搬。"""
        rows = [(1, 'wbk_x')]
        self.assertEqual(pgsync._filter_rows('api_keys', ['id', 'prefix'], rows), rows)


class KeyPlainColumnTest(_Base):
    def test_column_exists_after_migration(self) -> None:
        """老库升级后必须有这一列，否则「再复制一次」在升级过的机器上全是 409。"""
        names = {r['name'] for r in db.query('PRAGMA table_info(api_keys)')}
        self.assertIn('key_plain', names)

    def test_create_key_stores_hash_and_plain_consistent(self) -> None:
        created = keysvc.create_key(name='x', realm='cn')
        row = db.query_one('SELECT key_hash, key_plain, prefix FROM api_keys WHERE id = ?',
                           (created['id'],))
        self.assertEqual(row['key_plain'], created['key'])
        self.assertEqual(row['key_hash'], keysvc._hash(created['key']))
        self.assertEqual(row['prefix'], created['key'][:12])


if __name__ == '__main__':
    unittest.main()

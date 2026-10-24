"""PostgreSQL 异地备份：配置、结构生成与搬运逻辑。

这一批里最值得钉住的是**搬运的语义**，因为它的错误都很安静：

  · 全量覆盖被写成「合并」→ 镜像里已经删掉的记录留在本地，恢复出来的数据
    比源多，而且没有任何报错；
  · 列投影取错（取并集而不是交集）→ 用一份稍旧的镜像恢复时整场失败，或更糟：
    把「本地没有的列」当成空值灌进去；
  · 恢复失败没有整体回滚 → 留下半个库，比恢复前还糟。

所以下面用「SQLite 文件当假 PG」把整套搬运跑通：搬运逻辑与两端具体是什么库
无关（见 pgsync 里 Source/Sink 的说明），因此这些用例不需要真的起一个 PG。
真连 PG 的用例只在设了 `WB_TEST_PG_DSN` 时才跑（见文件末尾）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.services import pgsync  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (config.DB_PATH, config.AUTH_DIR, config.USERS_FILE,
                      config.DATA_DIR, pgsync.STATUS_FILE)
        root = Path(self._tmp.name)
        config.DB_PATH = root / 'm.db'
        config.AUTH_DIR = root / 'auths'
        config.USERS_FILE = root / 'users.json'
        config.DATA_DIR = root
        pgsync.STATUS_FILE = root / 'pgsync-status.json'
        from server import db
        db._conn = None
        db.connect()
        self.db = db

    def tearDown(self) -> None:
        if self.db._conn is not None:
            self.db._conn.close()
        self.db._conn = None
        (config.DB_PATH, config.AUTH_DIR, config.USERS_FILE,
         config.DATA_DIR, pgsync.STATUS_FILE) = self._orig
        self._tmp.cleanup()


class TypeAndDdlTest(unittest.TestCase):
    """SQLite 亲和类型 → PG 类型，以及建表语句的生成。"""

    def test_type_mapping(self) -> None:
        cases = [
            ('INTEGER', 'BIGINT'), ('INT', 'BIGINT'), ('BIGINT', 'BIGINT'),
            ('REAL', 'DOUBLE PRECISION'), ('DOUBLE', 'DOUBLE PRECISION'),
            ('TEXT', 'TEXT'), ('VARCHAR(64)', 'TEXT'), ('INTEGER(11)', 'BIGINT'),
            ('BLOB', 'BYTEA'),
            # 没见过的类型不能猜成数值：宽一点（TEXT）只是少一层校验，
            # 猜错成数值则会让恢复直接失败。
            ('', 'TEXT'), (None, 'TEXT'), ('WHATEVER', 'TEXT'),
        ]
        for declared, want in cases:
            with self.subTest(declared=declared):
                self.assertEqual(pgsync.pg_type(declared), want)

    def test_ddl_carries_pk_and_notnull(self) -> None:
        cols = [
            {'name': 'id', 'type': 'INTEGER', 'notnull': False, 'pk': 1},
            {'name': 'name', 'type': 'TEXT', 'notnull': True, 'pk': 0},
            {'name': 'credit', 'type': 'REAL', 'notnull': True, 'pk': 0},
        ]
        ddl = pgsync.pg_ddl('api_keys', cols)
        self.assertIn('"id" BIGINT', ddl)
        self.assertIn('"name" TEXT NOT NULL', ddl)
        self.assertIn('"credit" DOUBLE PRECISION NOT NULL', ddl)
        self.assertIn('PRIMARY KEY ("id")', ddl)

    def test_ddl_without_pk(self) -> None:
        ddl = pgsync.pg_ddl('t', [{'name': 'a', 'type': 'TEXT',
                                   'notnull': False, 'pk': 0}])
        self.assertNotIn('PRIMARY KEY', ddl)

    def test_mirror_table_detection(self) -> None:
        self.assertTrue(pgsync._is_mirror_table(pgsync.META_TABLE))
        self.assertTrue(pgsync._is_mirror_table('_wb_anything'))
        self.assertFalse(pgsync._is_mirror_table('api_keys'))


class DsnTest(unittest.TestCase):
    """连接串拼装。密码里的特殊字符是这里唯一真正容易错的地方。"""

    def _cfg(self, **patch):
        cfg = dict(pgsync.DEFAULT_CONFIG)
        cfg.update(patch)
        return cfg

    def test_basic(self) -> None:
        dsn = pgsync.build_dsn(self._cfg(host='db.local', port=5433,
                                         dbname='wb', user='u', password='p'))
        self.assertEqual(dsn, 'postgresql://u:p@db.local:5433/wb')

    def test_password_with_special_chars(self) -> None:
        """密码里的 @ / : / # 必须被转义。

        不转义的后果是 libpq 把 `@` 之后当成主机名 —— 报「连不上」，
        而错误信息里看不出是密码被截断了，排查成本极高。
        """
        dsn = pgsync.build_dsn(self._cfg(host='h', dbname='d', user='u',
                                         password='p@ss:w/rd#1'))
        self.assertNotIn('p@ss:w', dsn)
        self.assertIn('p%40ss%3Aw%2Frd%231', dsn)

    def test_sslmode_only_when_not_default(self) -> None:
        base = self._cfg(host='h', dbname='d', user='u', password='p')
        self.assertNotIn('sslmode', pgsync.build_dsn(base))
        self.assertIn('sslmode=require',
                      pgsync.build_dsn({**base, 'sslmode': 'require'}))

    def test_sslmode_must_be_a_libpq_mode(self) -> None:
        """非法 sslmode 要在拼连接串时就给出可选值。

        否则 libpq 只回一句 "invalid sslmode value: yes"，用户不知道
        合法值是什么；而且这条错误是在备份任务线程里才出现的。
        """
        base = self._cfg(host='h', dbname='d', user='u', password='p')
        with self.assertRaises(ValueError) as ctx:
            pgsync.build_dsn({**base, 'sslmode': 'yes'})
        self.assertIn('verify-full', str(ctx.exception))
        # 大小写与首尾空格归一
        self.assertIn('sslmode=verify-full',
                      pgsync.build_dsn({**base, 'sslmode': ' VerIfy-Full '}))

    def test_empty_host_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pgsync.build_dsn(self._cfg(host='   '))


class ConfigTest(_Case):
    """配置的存取与密码掩码。"""

    def test_save_and_public_masks_password(self) -> None:
        pgsync.save_config({'host': 'h', 'port': '5433', 'dbname': 'd',
                            'user': 'u', 'password': 'secret'})
        cfg = pgsync.get_config()
        self.assertEqual(cfg['port'], 5433)          # 字符串端口归一成整数
        self.assertEqual(cfg['password'], 'secret')  # 库里仍是明文
        self.assertEqual(pgsync.public_config()['password'], pgsync._MASK)

    def test_mask_does_not_overwrite_password(self) -> None:
        """**核心**：把掩码原样存回来不能改掉密码。

        界面回填的是 `********`，用户只改了个端口就点保存 —— 若按字面执行，
        备份会从这一刻起连不上，而界面上看不出任何异常。
        """
        pgsync.save_config({'host': 'h', 'dbname': 'd', 'user': 'u',
                            'password': 'secret'})
        pgsync.save_config({'port': 6000, 'password': pgsync._MASK})
        self.assertEqual(pgsync.get_config()['password'], 'secret')
        self.assertEqual(pgsync.get_config()['port'], 6000)

    def test_empty_password_keeps_existing(self) -> None:
        pgsync.save_config({'host': 'h', 'dbname': 'd', 'password': 'secret'})
        pgsync.save_config({'password': ''})
        self.assertEqual(pgsync.get_config()['password'], 'secret')

    def test_unknown_keys_ignored(self) -> None:
        pgsync.save_config({'host': 'h', 'dbname': 'd', 'evil': 'x'})
        self.assertNotIn('evil', pgsync.get_config())

    def test_interval_clamped(self) -> None:
        pgsync.save_config({'interval_minutes': -5})
        self.assertEqual(pgsync.get_config()['interval_minutes'], 0)
        pgsync.save_config({'interval_minutes': '15'})
        self.assertEqual(pgsync.get_config()['interval_minutes'], 15)

    def test_invalid_sslmode_rejected_on_save(self) -> None:
        with self.assertRaises(ValueError):
            pgsync.save_config({'sslmode': 'yes'})
        # 被拒的保存不能改动已存配置
        self.assertEqual(pgsync.get_config()['sslmode'], 'prefer')

    def test_merge_form_for_test_connection(self) -> None:
        """「先测后存」：表单里的新值要生效，掩码的密码要沿用已存的。"""
        pgsync.save_config({'host': 'old', 'dbname': 'd', 'password': 'secret'})
        merged = pgsync.merge_form(pgsync.get_config(),
                                   {'host': 'new', 'password': pgsync._MASK})
        self.assertEqual(merged['host'], 'new')
        self.assertEqual(merged['password'], 'secret')


class SourceIntrospectionTest(_Case):
    """从 SQLite 现读表与列 —— 「表结构不写死」的那一半。"""

    def test_tables_exclude_internal(self) -> None:
        src = pgsync._SqliteSource(config.DB_PATH)
        try:
            tables = src.tables()
        finally:
            src.close()
        self.assertIn('api_keys', tables)
        self.assertIn('request_logs', tables)
        # sqlite 自己的内部表不能出现在镜像清单里
        self.assertFalse([t for t in tables if t.startswith('sqlite_')])
        # 令牌整表不随备份走；settings 是混合表，按 key 白名单同步
        self.assertNotIn('api_tokens', tables)
        self.assertIn('settings', tables)

    def test_columns_match_schema(self) -> None:
        src = pgsync._SqliteSource(config.DB_PATH)
        try:
            cols = src.columns('api_keys')
        finally:
            src.close()
        names = [c['name'] for c in cols]
        self.assertIn('key_hash', names)
        self.assertIn('quota_credit', names)
        by_name = {c['name']: c for c in cols}
        self.assertEqual(by_name['id']['pk'], 1)
        self.assertEqual(pgsync.pg_type(by_name['quota_credit']['type']),
                         'DOUBLE PRECISION')

    def test_rows_batched_and_projected(self) -> None:
        keys = ['model_map', 'daily_tasks_plan', 'task_claim_schedule',
                'token_refresh_plan', 'proxy_pool']
        for key in keys:
            self.db.execute('INSERT INTO settings(key, value) VALUES(?, ?)',
                            (key, '{}'))
        src = pgsync._SqliteSource(config.DB_PATH)
        try:
            chunks = list(src.rows('settings', ['key'], 2))
        finally:
            src.close()
        self.assertEqual([len(c) for c in chunks], [2, 2, 1])  # 按批切
        self.assertIn(chunks[0][0][0], keys)                    # 只取投影的那一列

    def test_settings_rows_are_allowlisted(self) -> None:
        for key in ('model_map', 'github_token', 'update_proxy',
                    'http_proxy', 'credits_snapshot'):
            self.db.execute('INSERT INTO settings(key, value) VALUES(?, ?)',
                            (key, 'x'))
        src = pgsync._SqliteSource(config.DB_PATH)
        try:
            rows = [row for chunk in src.rows('settings', ['key', 'value'], 100)
                    for row in chunk]
        finally:
            src.close()
        self.assertIn('model_map', [row[0] for row in rows])
        for secret in ('github_token', 'update_proxy', 'http_proxy',
                       'credits_snapshot'):
            self.assertNotIn(secret, [row[0] for row in rows])


class CopyTest(_Case):
    """搬运语义：全量覆盖、列交集、行数精确。"""

    def _make(self, path: Path, rows: int) -> None:
        conn = sqlite3.connect(str(path))
        conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, extra TEXT)')
        conn.executemany('INSERT INTO t VALUES (?, ?, ?)',
                         [(i, f'n{i}', f'e{i}') for i in range(rows)])
        conn.commit()
        conn.close()

    def test_full_copy_roundtrip(self) -> None:
        root = Path(self._tmp.name)
        src_path, dst_path = root / 'src.db', root / 'dst.db'
        self._make(src_path, 7)
        self._make(dst_path, 0)
        src = pgsync._SqliteSource(src_path)
        dst = sqlite3.connect(str(dst_path))
        try:
            result = pgsync.copy_data(src, pgsync._SqliteSink(dst))
            dst.commit()
            got = dst.execute('SELECT COUNT(*) FROM t').fetchone()[0]
        finally:
            src.close()
            dst.close()
        self.assertEqual(result['tables'], {'t': 7})
        self.assertEqual(result['rows'], 7)
        self.assertEqual(got, 7)

    def test_overwrite_not_merge(self) -> None:
        """**核心**：目标已有的行必须先被清掉。

        「恢复」的语义是「让本地等于镜像」。若退化成合并，镜像里已经删掉的
        记录会留在本地 —— 数据看起来更多，却与源不一致，且没有任何报错。
        搬运本身不清表（那是调用方的事），这里钉住的是调用方的约定。
        """
        root = Path(self._tmp.name)
        src_path, dst_path = root / 'src.db', root / 'dst.db'
        self._make(src_path, 3)
        self._make(dst_path, 10)
        src = pgsync._SqliteSource(src_path)
        dst = sqlite3.connect(str(dst_path))
        try:
            dst.execute('DELETE FROM t')          # 调用方的「先清空」
            pgsync.copy_data(src, pgsync._SqliteSink(dst))
            dst.commit()
            got = dst.execute('SELECT COUNT(*) FROM t').fetchone()[0]
        finally:
            src.close()
            dst.close()
        self.assertEqual(got, 3)

    def test_column_intersection(self) -> None:
        """两边列集不同时只搬交集 —— 用稍旧的镜像也能恢复。"""
        root = Path(self._tmp.name)
        src_path, dst_path = root / 'src.db', root / 'dst.db'
        conn = sqlite3.connect(str(src_path))
        conn.execute('CREATE TABLE t (id INTEGER, a TEXT, b TEXT)')
        conn.execute("INSERT INTO t VALUES (1, 'A', 'B')")
        conn.commit()
        conn.close()
        conn = sqlite3.connect(str(dst_path))
        conn.execute('CREATE TABLE t (id INTEGER, a TEXT, c TEXT)')
        conn.commit()
        conn.close()

        src = pgsync._SqliteSource(src_path)
        dst = sqlite3.connect(str(dst_path))
        try:
            pgsync.copy_data(src, pgsync._SqliteSink(dst))
            dst.commit()
            row = dst.execute('SELECT id, a, c FROM t').fetchone()
        finally:
            src.close()
            dst.close()
        self.assertEqual(row, (1, 'A', None))   # b 丢掉，c 留默认值

    def test_missing_table_skipped(self) -> None:
        root = Path(self._tmp.name)
        src_path, dst_path = root / 'src.db', root / 'dst.db'
        conn = sqlite3.connect(str(src_path))
        conn.execute('CREATE TABLE only_in_src (id INTEGER)')
        conn.commit()
        conn.close()
        conn = sqlite3.connect(str(dst_path))
        conn.execute('CREATE TABLE other (id INTEGER)')
        conn.commit()
        conn.close()
        src = pgsync._SqliteSource(src_path)
        dst = sqlite3.connect(str(dst_path))
        try:
            result = pgsync.copy_data(src, pgsync._SqliteSink(dst))
        finally:
            src.close()
            dst.close()
        self.assertEqual(result['rows'], 0)


class StatusTest(_Case):
    """状态文件：前端靠它渲染进度，残留的 running 会让界面永远转圈。"""

    def test_absent_file_is_idle(self) -> None:
        st = pgsync.read_status()
        self.assertFalse(st['running'])
        self.assertIsNone(st['ok'])
        self.assertEqual(st['logs'], [])

    def test_roundtrip(self) -> None:
        rep = pgsync._Reporter('export')
        rep.log('开始')
        rep.step('导出 api_keys（1/19）', percent=5)
        rep.finish(True, '导出完成')
        st = pgsync.read_status()
        self.assertFalse(st['running'])
        self.assertTrue(st['ok'])
        self.assertEqual(st['percent'], 100)
        self.assertEqual(st['kind'], 'export')
        self.assertTrue(any('开始' in e['text'] for e in st['logs']))

    def test_stale_running_is_reported_interrupted(self) -> None:
        """进程重启后残留的 running=true 必须被判定为「中断」。

        否则界面会一直显示「正在导出」，而其实没有任何任务在跑，
        用户既等不到结果也没法重新发起。
        """
        pgsync._write_status({'running': True, 'kind': 'export', 'ok': None,
                              'step': '导出中', 'percent': 40, 'logs': [],
                              'started_at': 1, 'finished_at': 0})
        st = pgsync.read_status()
        self.assertFalse(st['running'])
        self.assertFalse(st['ok'])
        self.assertIn('中断', st['step'])

    def test_corrupt_file_does_not_raise(self) -> None:
        pgsync.STATUS_FILE.write_text('{ not json', encoding='utf-8')
        st = pgsync.read_status()
        self.assertFalse(st['running'])


class StartJobTest(_Case):
    """任务启动的前置校验，以及**锁不能泄漏**。"""

    def test_rejects_without_host(self) -> None:
        ok, message = pgsync.start_job('export')
        self.assertFalse(ok)
        self.assertIn('地址', message)

    def test_lock_released_after_rejection(self) -> None:
        """配置不全被拒之后，锁必须还回去。

        漏掉这一处，功能会永久卡在「已有任务在进行中」，
        而且只有重启面板才能恢复 —— 这一条就是为它写的。
        """
        pgsync.start_job('export')
        self.assertFalse(pgsync._job_lock.locked())
        pgsync.save_config({'host': 'h', 'dbname': 'd'})
        # 锁已还，所以这次能真的进入「尝试启动」而不是被互斥挡住
        ok, message = pgsync.start_job('nonsense')
        self.assertFalse(ok)
        self.assertIn('未知', message)

    def test_rejects_unknown_kind(self) -> None:
        ok, _ = pgsync.start_job('delete-everything')
        self.assertFalse(ok)


class TestConnectionTest(unittest.TestCase):
    """探测连通性：不能抛异常，结果一律以文案回给界面。"""

    def test_missing_driver_or_bad_host(self) -> None:
        cfg = dict(pgsync.DEFAULT_CONFIG)
        cfg['host'] = '127.0.0.1'
        cfg['port'] = 1        # 保留端口，必然连不上
        cfg['dbname'] = 'nope'
        cfg['user'] = 'nobody'
        result = pgsync.test_connection(cfg)
        self.assertIn('ok', result)
        self.assertIn('message', result)
        if not result['ok']:
            self.assertTrue(result['message'])

    def test_empty_host(self) -> None:
        result = pgsync.test_connection(dict(pgsync.DEFAULT_CONFIG))
        self.assertFalse(result['ok'])


class RouterTest(unittest.TestCase):
    """接口层：鉴权边界与端到端（不打真 PG）。

    「谁能调」这件事必须钉住：导出会把全部数据（含密钥哈希）复制出去，
    恢复会**覆盖本地库** —— 只读令牌或普通登录用户都不该碰得到。
    """

    @classmethod
    def setUpClass(cls) -> None:
        import json as _json
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR,
                     config.DATA_DIR, pgsync.STATUS_FILE)
        config.DB_PATH = d / 'm.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.DATA_DIR = d
        pgsync.STATUS_FILE = d / 'pgsync-status.json'
        from server import security
        cls.security = security
        config.USERS_FILE.write_text(_json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('admin-pw')}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from fastapi.testclient import TestClient
        from server.main import app
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR,
         config.DATA_DIR, pgsync.STATUS_FILE) = cls._orig
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.security._fail.clear()
        self.security._user_fail.clear()
        self.c.cookies.clear()
        db.set_setting(pgsync.CONFIG_KEY, dict(pgsync.DEFAULT_CONFIG))

    def tearDown(self) -> None:
        self.security._fail.clear()
        self.security._user_fail.clear()
        self.c.cookies.clear()

    def _login(self) -> None:
        r = self.c.post('/api/login', json={'username': 'admin', 'password': 'admin-pw'})
        self.assertEqual(r.status_code, 200, r.text)
        self.c.cookies.update(dict(r.cookies))

    def test_requires_login(self) -> None:
        self.assertEqual(self.c.get('/api/settings/pg-sync').status_code, 401)
        self.assertEqual(self.c.post('/api/settings/pg-sync', json={}).status_code, 401)
        self.assertEqual(self.c.post('/api/settings/pg-sync/export').status_code, 401)
        self.assertEqual(self.c.post('/api/settings/pg-sync/import').status_code, 401)

    def test_get_and_save(self) -> None:
        self._login()
        r = self.c.get('/api/settings/pg-sync')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn('config', body)
        self.assertIn('status', body)
        self.assertFalse(body['status']['running'])

        r = self.c.post('/api/settings/pg-sync', json={
            'host': 'pg.internal', 'port': 5432, 'dbname': 'wb',
            'user': 'u', 'password': 'pw', 'enabled': True, 'interval_minutes': 60})
        self.assertEqual(r.status_code, 200, r.text)
        # 读回来时密码必须是掩码，不能回显明文
        self.assertEqual(r.json()['config']['password'], pgsync._MASK)
        self.assertEqual(r.json()['config']['host'], 'pg.internal')

        # 保存后再读一次：掩码回传不能把密码改掉
        self.c.post('/api/settings/pg-sync', json={'password': pgsync._MASK, 'port': 5433})
        self.assertEqual(pgsync.get_config()['password'], 'pw')
        self.assertEqual(pgsync.get_config()['port'], 5433)

    def test_invalid_sslmode_returns_400(self) -> None:
        self._login()
        r = self.c.post('/api/settings/pg-sync', json={'sslmode': 'yes'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('verify-full', r.text)

    def test_export_rejected_without_host(self) -> None:
        self._login()
        r = self.c.post('/api/settings/pg-sync/export')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()['ok'])
        # 被拒之后锁必须还回去，否则功能会永久卡死
        self.assertFalse(pgsync._job_lock.locked())

    def test_test_endpoint_reports_failure_not_500(self) -> None:
        """探测连不上时要回 200 + ok:false，而不是 500。

        500 在界面上是一句「Internal Server Error」，用户看不出是地址填错、
        密码不对还是驱动没装 —— 而这三件事的处置方式完全不同。
        """
        self._login()
        r = self.c.post('/api/settings/pg-sync/test',
                        json={'host': '127.0.0.1', 'port': 1, 'dbname': 'nope',
                              'user': 'x', 'password': 'y'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('ok', r.json())

    def test_status_endpoint(self) -> None:
        self._login()
        r = self.c.get('/api/settings/pg-sync/status')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('running', r.json())


@unittest.skipUnless(os.environ.get('WB_TEST_PG_DSN'),
                     '未设置 WB_TEST_PG_DSN，跳过真实 PG 用例')
class RealPgTest(_Case):  # pragma: no cover - 只在有 PG 的环境跑
    """真实 PG 的端到端：导出 → 改本地 → 恢复 → 核对。

    只在设了 `WB_TEST_PG_DSN`（如 `postgresql://u:p@127.0.0.1/testdb`）时执行，
    因为它需要一台真的 PostgreSQL。CI 上可以起一个 service 容器再设这个变量。
    """

    def _cfg(self) -> dict:
        from urllib.parse import urlsplit
        parts = urlsplit(os.environ['WB_TEST_PG_DSN'])
        return {'host': parts.hostname or '', 'port': parts.port or 5432,
                'dbname': (parts.path or '/').lstrip('/'),
                'user': parts.username or '', 'password': parts.password or '',
                'sslmode': 'prefer', 'keep_local_backup': True,
                'enabled': False, 'interval_minutes': 0}

    def test_export_then_import(self) -> None:
        """数据表往返生效；settings 只同步白名单 key，敏感 key 留在本地。

        安全边界，不是便利性取舍：镜像可能在别人的数据库里，能写回
        github_mirror / update_proxy 就等于能改更新链路，能写回 api_tokens
        就等于能塞一把 admin 范围的管理令牌；但 model_map / 每日任务 / 代理池
        这些业务配置应该继续能备份恢复。
        """
        cfg = self._cfg()
        self.db.execute('INSERT INTO account_notes(uid, note, updated_at) '
                        'VALUES(?, ?, ?)', ('pg-roundtrip', 'before', 1))
        self.db.execute('INSERT INTO settings(key, value) VALUES(?, ?)',
                        ('model_map', '{"pg": "x"}'))
        self.db.execute('INSERT INTO settings(key, value) VALUES(?, ?)',
                        ('github_mirror', '"https://local"'))
        reporter = pgsync._Reporter('export')
        exported = pgsync.export_to_pg(cfg, reporter)
        self.assertGreater(exported['rows'], 0)
        self.assertIn('settings', exported['tables'])
        self.assertNotIn('api_tokens', exported['tables'])

        self.db.execute('DELETE FROM account_notes WHERE uid = ?', ('pg-roundtrip',))
        self.db.execute("DELETE FROM settings WHERE key = 'model_map'")
        self.db.execute("UPDATE settings SET value = '\"https://local-kept\"' "
                        "WHERE key = 'github_mirror'")
        reporter = pgsync._Reporter('import')
        pgsync.import_from_pg(cfg, reporter)
        row = self.db.query_one('SELECT note FROM account_notes WHERE uid = ?',
                                ('pg-roundtrip',))
        self.assertIsNotNone(row, '数据表应随恢复回来')
        self.assertEqual(row['note'], 'before')
        row = self.db.query_one("SELECT value FROM settings WHERE key = 'model_map'")
        self.assertIsNotNone(row, '白名单里的业务配置应随恢复回来')
        self.assertEqual(json.loads(row['value']), {'pg': 'x'})
        row = self.db.query_one(
            "SELECT value FROM settings WHERE key = 'github_mirror'")
        self.assertEqual(json.loads(row['value']), 'https://local-kept',
                         '敏感 key 不该被镜像覆盖')


if __name__ == '__main__':
    unittest.main()

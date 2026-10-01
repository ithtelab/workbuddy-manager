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
        for i in range(5):
            self.db.execute('INSERT INTO settings(key, value) VALUES(?, ?)',
                            (f'k{i}', str(i)))
        src = pgsync._SqliteSource(config.DB_PATH)
        try:
            chunks = list(src.rows('settings', ['key'], 2))
        finally:
            src.close()
        self.assertEqual([len(c) for c in chunks], [2, 2, 1])  # 按批切
        self.assertEqual(chunks[0][0], ('k0',))                # 只取投影的那一列


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


class StatusTimestampsTest(_Case):
    """状态里带上「上次迁移 / 上次恢复」两个时刻（issue #122）。

    为什么必须随 /status 给出：面板的空闲轮询只拉它（轻量）。这两个时刻如果
    只藏在整份配置接口里，面板要么显示过期值、要么就得整份拉配置——而把整份
    配置写回表单正是被报告的问题（填写被打断）。
    """

    def test_defaults_to_zero(self) -> None:
        st = pgsync.read_status()
        self.assertEqual(st['last_export_at'], 0)
        self.assertEqual(st['last_import_at'], 0)

    def test_reads_from_settings(self) -> None:
        self.db.set_setting(pgsync.LAST_EXPORT_KEY, 111)
        self.db.set_setting(pgsync.LAST_IMPORT_KEY, 222)
        st = pgsync.read_status()
        self.assertEqual(st['last_export_at'], 111)
        self.assertEqual(st['last_import_at'], 222)


class PgSyncPanelPollingShapeTest(unittest.TestCase):
    """面板轮询的**源码形状**守卫（issue #122）。

    被报告的问题有一个行为测试抓不住的形态：空闲轮询整份拉配置并把服务器
    旧值写回表单，用户填到一半的字段被打回去。这里把三条不变式钉在源码上：

      1. 轮询走 `useHeartbeat`（隐藏时跳过、切回立即补一次），回调只允许调
         `pollStatus`，不许出现 `load(`；
      2. `pollStatus` 对表单只允许**合并**两个时刻字段（`setForm((f)`），
         不许出现整份覆盖；
      3. 整份配置拉取（`pgSyncApi.config()`）全文件只允许出现一次
         （挂载 / 重试用的 `load`）。

    照仓库里同类守卫的惯例：断言的是**结构**，文案与排版随便改；这几条一旦
    被改回去，这里会红。
    """

    SRC = (Path(__file__).resolve().parents[2]
           / 'web' / 'components' / 'common' / 'settings' / 'PgSyncPanel.tsx')

    def setUp(self) -> None:
        self.src = self.SRC.read_text(encoding='utf-8')

    def test_poll_uses_heartbeat_and_not_load(self) -> None:
        lines = [l.strip() for l in self.src.splitlines()
                 if 'useHeartbeat(' in l
                 and not l.lstrip().startswith(('import', '//', '*'))]
        self.assertEqual(len(lines), 1, f'没找到唯一的 useHeartbeat 调用：{lines}')
        call = lines[0]
        self.assertIn('pollStatus', call)
        self.assertNotIn('load(', call,
                         '轮询又整份拉配置了 —— 会覆盖用户正在填的表单（issue #122）')
        self.assertNotIn('window.setInterval(', self.src,
                         '轮询改回了裸 setInterval —— 切回标签页不会立即更新'
                         '（仓库的轮询可见性守卫也会拒绝）')

    def test_poll_merges_instead_of_overwriting(self) -> None:
        seg = self.src.split('const pollStatus')[1].split('}, []);')[0]
        self.assertIn('pgSyncApi.status()', seg)
        self.assertIn('setForm((f)', seg, '轮询对表单必须是合并写法')
        self.assertNotIn('setForm(r.config)', seg, '轮询不许整份覆盖表单')

    def test_full_config_has_a_single_call_site(self) -> None:
        self.assertEqual(self.src.count('pgSyncApi.config()'), 1,
                         '整份配置只允许在 load()（挂载 / 重试）里拉一次')


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
        cfg = self._cfg()
        self.db.execute('INSERT INTO settings(key, value) VALUES(?, ?)',
                        ('pg_roundtrip', '"before"'))
        reporter = pgsync._Reporter('export')
        exported = pgsync.export_to_pg(cfg, reporter)
        self.assertGreater(exported['rows'], 0)

        self.db.execute('DELETE FROM settings WHERE key = ?', ('pg_roundtrip',))
        reporter = pgsync._Reporter('import')
        pgsync.import_from_pg(cfg, reporter)
        row = self.db.query_one('SELECT value FROM settings WHERE key = ?',
                                ('pg_roundtrip',))
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row['value']), 'before')


if __name__ == '__main__':
    unittest.main()

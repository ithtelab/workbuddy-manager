"""复核补的测试：恢复的作用域、两种跳过原因、连接超时。

这三条都来自维护者复核：第一条钉住一个**容易被「好心改坏」**的行为（镜像里没有的
表不能清空），第二条与第三条是复核时改掉的两处（日志口径 / 连接超时）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest import mock

from server import config, db
from server.services import pgsync

from .test_pgsync import _Case  # 复用同一套临时 HOME/DB 装置

_ROOT = Path(__file__).resolve().parents[2]


class _StubPg:
    """把 PG 连接换成桩：只记录 connect 的参数，并提供一个空的上下文管理器。"""

    def __init__(self, kwargs_sink: list[dict]):
        self._sink = kwargs_sink

    def connect(self, dsn, **kwargs):  # noqa: ANN001
        self._sink.append(kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # 导出流程会在事务里调它们（桩只要不炸即可）。cursor() 给一个「查什么都没有」
    # 的假游标：`_prev_tables` 会因此走「按首次导出处理」那条分支（正是真实首次
    # 导出的形态），而不是抛出一个被它自己吞掉的异常、在测试输出里留一段吓人的栈。
    def commit(self):
        pass

    def rollback(self):
        pass

    def cursor(self):
        return _StubCursor()


class _StubCursor:
    def execute(self, *a, **kw):
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ImportScopeTest(_Case):
    """恢复时，**镜像里没有的表不会被清空**（且日志如实这么说）。

    为什么值得单独钉：恢复的直觉是「让本地等于镜像」，于是很容易顺手把这些表也
    DELETE 掉；但镜像可能来自更早的版本（那时这张表还不存在），清空等于删掉镜像里
    根本没有的数据，**不可恢复**。第一版的日志写的是「恢复后将保持为空」——
    与实际行为相反，照着它去理解会以为本地被清干净了。
    """

    def _run_import(self, mirror_tables: dict, logs: list[str]) -> None:
        class FakeSource:
            def __init__(self, conn):  # noqa: ANN001
                pass

            def tables(self):
                return list(mirror_tables)

            def columns(self, table):
                return mirror_tables[table]['columns']

            def rows(self, table, columns, batch=pgsync.BATCH):
                yield mirror_tables[table]['rows']

            def close(self):
                pass

        class Reporter:
            data: dict = {}

            def log(self, text, level='info'):
                logs.append(str(text))

            def step(self, text, percent=None):
                pass

        with mock.patch.object(pgsync, '_PgSource', FakeSource), \
             mock.patch.object(pgsync, '_require_psycopg', lambda: _StubPg([])), \
             mock.patch.object(pgsync, '_local_backup', lambda: None):
            pgsync.import_from_pg(
                {'host': 'stub', 'port': 5432, 'dbname': 'x', 'user': 'u',
                 'password': 'p', 'keep_local_backup': False, 'sslmode': 'prefer'},
                Reporter())

    def test_tables_missing_from_the_mirror_are_left_alone(self) -> None:
        self.db.execute('CREATE TABLE IF NOT EXISTS mirror_has(id INTEGER, v TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS local_only(id INTEGER, v TEXT)')
        self.db.execute("INSERT INTO mirror_has(id, v) VALUES(1, '旧值')")
        self.db.execute("INSERT INTO local_only(id, v) VALUES(1, '只在本地的数据')")
        logs: list[str] = []
        self._run_import(
            {'mirror_has': {'columns': [{'name': 'id', 'type': 'INTEGER', 'notnull': False, 'pk': 0},
                                        {'name': 'v', 'type': 'TEXT', 'notnull': False, 'pk': 0}],
                            'rows': [(1, '镜像里的新值')]}},
            logs)
        mirrored = self.db.query_one('SELECT v FROM mirror_has WHERE id = 1')
        kept = self.db.query_one('SELECT v FROM local_only WHERE id = 1')
        self.assertEqual(mirrored['v'], '镜像里的新值', '镜像里有的表没被覆盖')
        self.assertIsNotNone(kept, '镜像里没有的表被清空了 —— 那是不可恢复的损失')
        self.assertEqual(kept['v'], '只在本地的数据')
        said = [l for l in logs if 'local_only' in l]
        self.assertTrue(said, '没有任何日志提到这张表被跳过')
        self.assertIn('不会改动', said[0], f'日志口径与实际不符：{said[0]}')
        self.assertNotIn('保持为空', said[0], f'日志在说反话：{said[0]}')

    def test_mirror_only_tables_are_not_created_locally(self) -> None:
        """镜像里有、本地没有的表：**不建表、不写入**（镜像来自更新的版本时就是这样）。

        复核时我一度以为这里会走到 `_SqliteSink.accepts` 的「本地没有这张表」分支，
        写了条测试才发现根本到不了 —— 恢复只遍历 `available & local` 的交集
        （那条分支是给别的调用方留的防御）。所以这里断言真实行为：镜像独有的表
        被安静跳过，不凭空建表，也不出现在「恢复不会改动它们」的提示里。
        """
        self.db.execute('DROP TABLE IF EXISTS only_in_mirror')
        logs: list[str] = []
        self._run_import(
            {'only_in_mirror': {'columns': [{'name': 'a', 'type': 'TEXT', 'notnull': False, 'pk': 0}],
                                'rows': [('x',)]}},
            logs)
        tables = {str(r['name']) for r in self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('only_in_mirror', tables, '镜像独有的表被凭空建到了本地')
        self.assertFalse([line for line in logs if 'only_in_mirror' in line],
                         f'不该有关于这张表的提示（它不在本地表清单里）：{logs}')

    def test_mirror_table_without_common_columns_is_skipped_not_cleared(self) -> None:
        """两边同名但列完全对不上：跳过，且**不清空**（清掉等于凭空删数据）。"""
        self.db.execute('CREATE TABLE IF NOT EXISTS renamed_cols(z INTEGER)')
        self.db.execute('INSERT INTO renamed_cols(z) VALUES(1)')
        logs: list[str] = []
        self._run_import(
            {'renamed_cols': {'columns': [{'name': 'a', 'type': 'TEXT', 'notnull': False, 'pk': 0}],
                              'rows': [('y',)]}},
            logs)
        self.assertIn('没有公共列', '\n'.join(logs), f'缺「没有公共列」的口径：{logs}')
        got = self.db.query_one('SELECT COUNT(*) AS c FROM renamed_cols')
        self.assertEqual(int(got['c']), 1, '列对不上就把本地那张表清空了 —— 数据凭空没了')


class RestoreConcurrencyTest(_Case):
    """恢复的耗时部分不得持有全局 DB 锁（审计 2026-10-18）。

    旧实现把整段「清空 + 灌入」包在 `db._lock` 里：大库恢复几秒到几十秒，
    期间网关的日志写入、鉴权、统计全部排队。新实现先写临时库，只在
    「关连接 → 换文件 → 重开」的毫秒级窗口持锁 —— 这两条用例分别钉住
    「复制期间可读写」与「失败不碰线上库」。
    """

    TABLE = 'restore_probe'

    def setUp(self) -> None:
        super().setUp()
        self.db.execute(f'CREATE TABLE IF NOT EXISTS {self.TABLE}(id INTEGER, v TEXT)')
        self.db.execute(f"INSERT INTO {self.TABLE} VALUES(1, '旧值')")

    def _run_import(self, rows_factory) -> None:
        table = self.TABLE

        class FakeSource:
            def __init__(self, conn):  # noqa: ANN001
                pass

            def tables(self):
                return [table]

            def columns(self, _table):
                return [
                    {'name': 'id', 'type': 'INTEGER', 'notnull': False, 'pk': 0},
                    {'name': 'v', 'type': 'TEXT', 'notnull': False, 'pk': 0},
                ]

            def rows(self, _table, _columns, batch=pgsync.BATCH):
                yield from rows_factory()

            def close(self):
                pass

        class Reporter:
            data: dict = {}

            def log(self, text, level='info'):
                pass

            def step(self, text, percent=None):
                pass

        with mock.patch.object(pgsync, '_PgSource', FakeSource), \
             mock.patch.object(pgsync, '_require_psycopg', lambda: _StubPg([])), \
             mock.patch.object(pgsync, '_local_backup', lambda: None):
            pgsync.import_from_pg(
                {'host': 'stub', 'port': 5432, 'dbname': 'x', 'user': 'u',
                 'password': 'p', 'keep_local_backup': False, 'sslmode': 'prefer'},
                Reporter())

    def test_online_queries_are_not_blocked_while_copying(self) -> None:
        gate = threading.Event()

        def rows():
            def probe():
                # 真正走一遍 query 路径；若全局锁被恢复线程占着，这里会卡住。
                db.query_one('SELECT 1')
                gate.set()

            threading.Thread(target=probe, daemon=True).start()
            self.assertTrue(gate.wait(3), '恢复复制期间在线查询被阻塞')
            yield [(1, '新值')]

        self._run_import(rows)
        got = self.db.query_one(f'SELECT v FROM {self.TABLE} WHERE id = 1')
        self.assertEqual(got['v'], '新值', '恢复没有真正落到线上库')

    def test_failure_during_copy_leaves_live_data_untouched(self) -> None:
        def rows():
            yield [(1, '新值')]
            raise RuntimeError('镜像读到一半断了')

        with self.assertRaises(RuntimeError):
            self._run_import(rows)
        got = self.db.query_one(f'SELECT v FROM {self.TABLE} WHERE id = 1')
        self.assertEqual(got['v'], '旧值', '恢复失败却把线上库改掉了')
        leftovers = list(Path(self._tmp.name).glob('.pgsync-restore-*.db'))
        self.assertFalse(leftovers, f'失败后临时库没有清理：{leftovers}')


class ReplaceDatabaseTest(_Case):
    """`db.replace_database`：换库成功、失败回滚、残留文件清理。"""

    def _staged_copy(self, value: str) -> Path:
        staging = Path(self._tmp.name) / 'staging.db'
        prober = Path(self._tmp.name) / 'probe.db'
        with db._lock:
            db.connect().execute('VACUUM INTO ?', (str(prober),))
        conn = sqlite3.connect(str(prober))
        try:
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('swap_probe', ?)",
                         (json.dumps(value),))
            conn.commit()
        finally:
            conn.close()
        os.replace(prober, staging)
        return staging

    def test_swap_replaces_data_and_cleans_temp_files(self) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('swap_probe', ?)",
                        (json.dumps('旧'),))
        staging = self._staged_copy('新')
        db.replace_database(staging)
        row = self.db.query_one("SELECT value FROM settings WHERE key = 'swap_probe'")
        self.assertEqual(json.loads(row['value']), '新')
        self.assertFalse(staging.exists(), '临时库没有被移走')
        self.assertFalse(Path(str(config.DB_PATH) + '.swap-old').exists(),
                         '旧库备份残留')

    def test_swap_failure_restores_old_database(self) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('swap_probe', ?)",
                        (json.dumps('旧'),))
        staging = self._staged_copy('新')
        real_replace = db.os.replace

        def flaky(src, dst):
            if Path(src) == staging:
                raise OSError('磁盘写满了')
            return real_replace(src, dst)

        with mock.patch.object(db.os, 'replace', flaky):
            with self.assertRaises(OSError):
                db.replace_database(staging)
        row = self.db.query_one("SELECT value FROM settings WHERE key = 'swap_probe'")
        self.assertEqual(json.loads(row['value']), '旧', '替换失败后旧库没有被放回来')

    def test_recovers_if_previous_swap_died_between_renames(self) -> None:
        """上次换库死在「旧库已改名、新库还没就位」时，不能把仅存的数据删掉。"""
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('swap_probe', ?)",
                        (json.dumps('旧'),))
        staging = self._staged_copy('新')
        target = Path(config.DB_PATH)
        old = Path(str(target) + '.swap-old')

        # 模拟崩溃现场：连接已关，正式库不在，只剩 .swap-old
        with db._lock:
            db._close_connection_locked()
        os.replace(target, old)
        self.assertFalse(target.exists())

        real_replace = db.os.replace

        def flaky(src, dst):
            if Path(src) == staging:
                raise OSError('磁盘写满了')
            return real_replace(src, dst)

        with mock.patch.object(db.os, 'replace', flaky):
            with self.assertRaises(OSError):
                db.replace_database(staging)
        row = self.db.query_one("SELECT value FROM settings WHERE key = 'swap_probe'")
        self.assertEqual(json.loads(row['value']), '旧',
                         '崩溃残骸被当成垃圾删掉，仅存的数据丢了')


class ValidationOrderTest(_Case):
    """配置校验要**先于**驱动检查。

    复核时发现：`test_connection` 原来先 `_require_psycopg()`。于是没装驱动的
    机器上填了个空地址，用户看到的是「未安装 PostgreSQL 驱动」——而真正要改的是
    地址。顺带这也让「空地址」那条用例在没装驱动的环境里**假通过**（它断言的
    只是 ok=False，走的却是另一条分支）。
    """

    def test_empty_host_is_reported_even_without_the_driver(self) -> None:
        with mock.patch.object(pgsync, '_require_psycopg',
                               side_effect=AssertionError('不该走到要驱动这一步')):
            result = pgsync.test_connection(dict(pgsync.DEFAULT_CONFIG))
        self.assertFalse(result['ok'])
        self.assertIn('地址', result['message'],
                      f'没填地址时给出的不是地址相关的提示：{result["message"]}')


class ConnectTimeoutTest(_Case):
    """导出与恢复的连接都要带 connect_timeout。

    黑洞地址（SYN 被丢）下 libpq 默认挂到 OS 的 TCP 超时（分钟级），期间任务锁
    一直被占着、界面停在「进行中」，用户分不清是网络慢还是卡死。
    """

    def test_both_flows_pass_a_timeout(self) -> None:
        calls: list[dict] = []
        sink = type('S', (), {
            'accepts': lambda self, t, c: list(c),
            'prepare': lambda self, t, c: None,
            'write': lambda self, t, c, r: len(r),
            'drop': lambda self, t: None,
        })()
        rep = type('R', (), {
            'data': {},
            'log': lambda self, *a, **k: None,
            'step': lambda self, *a, **k: None,
        })()
        cfg = {'host': 'stub', 'port': 5432, 'dbname': 'x', 'user': 'u',
               'password': 'p', 'sslmode': 'prefer'}
        with mock.patch.object(pgsync, '_require_psycopg', lambda: _StubPg(calls)), \
             mock.patch.object(pgsync, '_write_meta', lambda pg, tables: None), \
             mock.patch.object(pgsync, '_PgSink', lambda pg: sink):
            pgsync.export_to_pg(cfg, rep)
        self.assertTrue(calls, '导出没有调用 psycopg.connect')
        self.assertEqual(calls[-1].get('connect_timeout'), pgsync.CONNECT_TIMEOUT,
                         f'导出连接没带超时：{calls[-1]}')

class PgSyncPanelPollingTest(unittest.TestCase):
    """issue #122：备份页的轮询不得覆盖正在填的表单。

    报障：填 PG 同步配置时输入框每约 15 秒被服务端旧值打回去 —— 空闲轮询走的是
    **完整重载**，顺带把表单 state 整体重置。这里钉住两件事，都是「只有真人填一遍
    才会发现」的类型：

      ① 轮询（空闲 15 秒 / 运行中 1.5 秒）只拉进度，一次都不碰表单；
      ② 服务端配置回填表单时必须过脏检查（有未保存输入就不覆盖），保存成功后把
         脏标记清掉。

    行为验收在 `dev/verify_pgsync_form_ui.py`（真浏览器里打字等两个轮询周期），
    这里钉结构：改回「空闲也全量重载」会立刻被拦下。
    """

    PANEL = (_ROOT / 'web' / 'components' / 'common' / 'settings'
             / 'PgSyncPanel.tsx')

    def setUp(self) -> None:
        self.src = self.PANEL.read_text(encoding='utf-8')

    def test_轮询回调只拉进度(self) -> None:
        block = re.search(r'window\.setInterval\(\(\) => \{([\s\S]*?)\}, interval\)', self.src)
        self.assertIsNotNone(block, '找不到轮询回调')
        body = block.group(1)
        self.assertIn('pollStatus', body, '轮询居然不拉进度了')
        self.assertNotIn('load(', body,
                         '轮询回调里又出现了完整重载 —— 那就是 issue #122 复发：'
                         '空闲时用服务端配置整体覆盖表单')

    def test_回填表单要过脏检查(self) -> None:
        loader = re.search(
            r'const load = useCallback\(async \(\) => \{([\s\S]*?)\}, \[\]\)', self.src)
        self.assertIsNotNone(loader, '找不到配置加载函数')
        body = loader.group(1)
        self.assertIn('dirtyRef.current', body,
                      '回填表单没有脏检查：用户没保存的输入会被旧值打回去')
        self.assertIn('setForm((f) =>', body,
                      '回填要走函数式更新（基于当前表单判断，而不是闭包里的旧值）')

    def test_脏标记的置位与清零(self) -> None:
        self.assertRegex(self.src, r'const patch = \(next[\s\S]{0,160}?dirtyRef\.current = true',
                         'patch() 没置脏标记 —— 之后任何回填都会覆盖用户输入')
        self.assertRegex(self.src, r'setForm\(r\.config\);\n\s*dirtyRef\.current = false',
                         '保存成功后没清脏标记 —— 之后服务端值再也回填不进表单')

    def test_守卫不是空转(self) -> None:
        """把轮询回调换回修前的写法，第一条必须能看出来。"""
        broken = self.src.replace(
            '      void pollStatus();\n',
            '      if (running) { void pollStatus(); } else { void load(); }\n', 1)
        self.assertNotEqual(broken, self.src, '替换没生效，这条反证没有意义')
        block = re.search(r'window\.setInterval\(\(\) => \{([\s\S]*?)\}, interval\)', broken)
        self.assertIn('load(', block.group(1),
                      '换回修前的写法后守卫仍看不出来 —— 这条守卫是空转的')


if __name__ == '__main__':
    unittest.main()

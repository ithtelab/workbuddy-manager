"""请求日志里的提示词缓存三段（issue #69）。

## 需求现场

用户想在请求日志里看到「这次请求有没有真的命中缓存」——同一个前缀重复发时，
不带缓存键的调用会全价计费，带键才命中；他要靠这个判断配置有没有生效。

## 数据来源（不是我估算的）

腾讯在流式末帧 `usage` 里给 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` /
`prompt_cache_write_tokens`，**上游自己的缓存统计（`/v1/stats` 的 cache_hit_rate）
就是从这三个字段来的**。所以我们直接记，不自己按 prompt_tokens 推算。

## 这里钉住的

  · 三段都要**区分「上游没给」与「给了 0」**：老上游不返回这三个字段 → NULL
    （界面显示「—」/「未采集」），给了 0 → 真的这次没命中。混起来用户会以为
    缓存生效了（或以为一直没生效），两种结论都是错的；
  · `usage` **整份传进 `_record`**，由它一处解析 credit 与这三段。四条协议路径
    （chat、Responses、Anthropic、测试台的流式与非流式各一条，共 8 处）各自都有
    一份 usage，分开取迟早漏一条，而漏的表现是「某个协议的缓存统计永远是空的」——
    这不是假设：**写这个功能时真漏了三条**（Responses / Anthropic / 测试台的
    非流式，当初做扣费时只取了 credit），界面上看起来「有数据、只是没命中」，
    比报错更难发现。所以下面有一条扫描把这条不变量钉住；
  · 脏数据（bool / 字符串 / 负数 / 缺字段）不能让记账崩：`_record` 在请求收尾
    路径上，抛异常会影响用户请求。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import gateway as G  # noqa: E402


class UsageCacheParseTest(unittest.TestCase):
    """解析：能取到就取到，取不到给 None，脏数据不抛。"""

    def test_normal(self) -> None:
        self.assertEqual(
            G._usage_cache({'prompt_cache_hit_tokens': 7808,
                            'prompt_cache_miss_tokens': 192,
                            'prompt_cache_write_tokens': 0}),
            (7808, 192, 0))

    def test_partial(self) -> None:
        """只给一段时，其余是 None（不是 0）。"""
        self.assertEqual(G._usage_cache({'prompt_cache_hit_tokens': 12}),
                         (12, None, None))

    def test_old_upstream_gives_nothing(self) -> None:
        """老上游的 usage 里没有这三个字段 → 三段都是 None。"""
        self.assertEqual(G._usage_cache({'prompt_tokens': 100, 'completion_tokens': 5}),
                         (None, None, None))
        self.assertEqual(G._usage_cache(None), (None, None, None))
        self.assertEqual(G._usage_cache({}), (None, None, None))

    def test_dirty_values_do_not_raise(self) -> None:
        for bad in (True, False, 'abc', -1, [], {}, None):
            with self.subTest(value=bad):
                got = G._usage_cache({'prompt_cache_hit_tokens': bad})
                self.assertEqual(got, (None, None, None),
                                 f'{bad!r} 应视为「没给」')

    def test_numeric_string_is_accepted(self) -> None:
        """线上见过把数字写成字符串的形态，能认就认。"""
        self.assertEqual(G._usage_cache({'prompt_cache_hit_tokens': '7'}),
                         (7, None, None))

    def test_zero_is_kept_as_zero(self) -> None:
        """**给了 0 要留住 0** —— 那代表这次确实没命中，与 NULL（没数据）不同。"""
        self.assertEqual(G._usage_cache({'prompt_cache_hit_tokens': 0}),
                         (0, None, None))


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._db = config.DB_PATH
        config.DB_PATH = root / 'manager.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._db
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _record(self, usage: dict | None, **kw) -> None:
        G._record(None, '1.2.3.4', 'glm-5.2', '', 200, 10, 5, 20, 'UA', None, True,
                  usage=usage, **kw)

    def _row(self) -> dict:
        return db.query_one('SELECT * FROM request_logs ORDER BY id DESC LIMIT 1')


class RecordStoresCacheTest(_DbCase):
    """落库：三段写进去，缺数据时是 NULL 而不是 0。"""

    def test_values_are_stored(self) -> None:
        self._record({'prompt_cache_hit_tokens': 7808,
                      'prompt_cache_miss_tokens': 192,
                      'prompt_cache_write_tokens': 0})
        row = self._row()
        self.assertEqual(row['cache_hit_tokens'], 7808)
        self.assertEqual(row['cache_miss_tokens'], 192)
        self.assertEqual(row['cache_write_tokens'], 0)

    def test_missing_is_null_not_zero(self) -> None:
        self._record({'prompt_tokens': 10})
        row = self._row()
        for col in ('cache_hit_tokens', 'cache_miss_tokens', 'cache_write_tokens'):
            self.assertIsNone(row[col], f'{col} 应为 NULL（「没数据」不是 0）')

    def test_credit_still_comes_from_usage(self) -> None:
        """回归：改成整份传 usage 之后，扣费仍要从 usage 取到。"""
        self._record({'credit': 1.25, 'prompt_cache_hit_tokens': 5})
        row = self._row()
        self.assertEqual(row['credit'], 1.25)
        self.assertEqual(row['cache_hit_tokens'], 5)

    def test_explicit_credit_wins(self) -> None:
        """显式传 credit 时以它为准（保留给没有 usage 的调用方）。"""
        self._record({'credit': 9.0}, credit=1.5)
        self.assertEqual(self._row()['credit'], 1.5)

    def test_dirty_usage_does_not_break_recording(self) -> None:
        """脏 usage 不能让记账抛错 —— 它在请求收尾路径上。"""
        self._record({'prompt_cache_hit_tokens': True, 'credit': 'x'})
        row = self._row()
        self.assertIsNone(row['cache_hit_tokens'])
        self.assertIsNone(row['credit'])


class LogsEndpointExposesCacheTest(_DbCase):
    """日志接口要带出这三列。

    注意 SQL 是 `SELECT l.*`（新列自动查得到），但**响应体是逐字段挑的** ——
    只加库列不加响应字段，接口里就静默没有它，界面也就拿不到。所以这里断言的是
    接口响应，而不是 SQL。
    """

    def test_api_returns_the_columns(self) -> None:
        from server.routers import logs as L
        self._record({'prompt_cache_hit_tokens': 100, 'prompt_cache_miss_tokens': 0})
        out = L.list_logs(page=1, size=10, user={'role': 'admin'})
        items = out.get('items') or out.get('logs') or []
        self.assertTrue(items, f'接口没返回日志：{list(out)[:5]}')
        self.assertEqual(items[0]['cache_hit_tokens'], 100)

    def test_all_three_are_listed(self) -> None:
        """三段都要在接口响应里（少一段界面就拿不到那一段）。

        注：`SELECT l.*` 取的是全部列，但**响应是逐字段挑的** —— 只加库列不加响应
        字段，接口里就静默没有它（本用例第一版就是这么红的）。所以这里三段逐个断言，
        而不是只断言 SQL。
        """
        from server.routers import logs as L
        self._record({'prompt_cache_hit_tokens': 7, 'prompt_cache_miss_tokens': 3,
                      'prompt_cache_write_tokens': 1})
        item = (L.list_logs(page=1, size=10, user={'role': 'admin'}).get('items') or [])[0]
        self.assertEqual(item['cache_hit_tokens'], 7)
        self.assertEqual(item['cache_miss_tokens'], 3)
        self.assertEqual(item['cache_write_tokens'], 1)


class MigrationFromOlderDbTest(unittest.TestCase):
    """升级路径：老库补列，且补完还能照常写入。

    线上装的大多是**已有库**。SQLite 的 `CREATE TABLE IF NOT EXISTS` 不会给已存在的
    表补列，所以必须靠显式 ALTER；漏了的后果不是「少一列」而是**更糟**：
    `add_request_log` 的 INSERT 里点了这个列名 → 整条写入失败 → 用户升级后
    「请求日志一片空白」（而 `_record` 是旁路，异常只写 warning，请求本身照常返回，
    所以从调用方根本看不出来）。因此这里除了断言补列成功，还要真写一条进去。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / 'old.db'
        self._orig = config.DB_PATH

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _make_old_db(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self._path)
        conn.executescript(
            'CREATE TABLE request_logs ('
            '  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, key_id INTEGER,'
            '  ip TEXT, model TEXT, mapped_model TEXT, status INTEGER DEFAULT 0,'
            '  prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,'
            '  latency_ms INTEGER DEFAULT 0, ua TEXT, error TEXT, stream INTEGER DEFAULT 0,'
            '  first_token_ms INTEGER, credit REAL, realm TEXT);'
        )
        conn.execute('INSERT INTO request_logs(ts, latency_ms, prompt_tokens) VALUES(1, 1234, 7)')
        conn.commit()
        conn.close()
        cols = {r[1] for r in sqlite3.connect(
            self._path).execute('PRAGMA table_info(request_logs)')}
        self.assertNotIn('cache_hit_tokens', cols)

    def test_old_db_gains_the_three_columns_and_still_accepts_writes(self) -> None:
        self._make_old_db()
        config.DB_PATH = self._path
        db._conn = None
        db.connect()

        cols = {r[1] for r in db._conn.execute('PRAGMA table_info(request_logs)')}
        for col in ('cache_hit_tokens', 'cache_miss_tokens', 'cache_write_tokens'):
            self.assertIn(col, cols, f'升级后缺列 {col}，写入会整条失败')
        old = db.query_one('SELECT prompt_tokens, cache_hit_tokens FROM request_logs WHERE id = 1')
        self.assertEqual(old['prompt_tokens'], 7, '历史记录不能丢')
        self.assertIsNone(old['cache_hit_tokens'], '历史记录没这个数据，应为 NULL')

        # 关键：迁移后的表要能照常写（这是漏 ALTER 时真正炸掉的地方）
        G._record(None, '1.2.3.4', 'glm-5.2', '', 200, 10, 5, 20, 'UA', None, True,
                  usage={'prompt_cache_hit_tokens': 5, 'prompt_cache_miss_tokens': 5})
        row = db.query_one('SELECT cache_hit_tokens, cache_miss_tokens FROM request_logs ORDER BY id DESC LIMIT 1')
        self.assertEqual(row['cache_hit_tokens'], 5)
        self.assertEqual(row['cache_miss_tokens'], 5)

    def test_migration_is_idempotent(self) -> None:
        self._make_old_db()
        config.DB_PATH = self._path
        for _ in range(2):
            db._conn = None
            db.connect()  # 重复执行不能报错
        cols = [r[1] for r in db._conn.execute('PRAGMA table_info(request_logs)')]
        self.assertEqual(cols.count('cache_hit_tokens'), 1)


class EveryUsageReadingCallSiteHandsUsageOver(unittest.TestCase):
    """扫描：读了 usage 的调用点，必须把 usage 交出去。

    这是本功能最容易漏、也最难被发现的一环（见文件头：写的时候就漏了三条）。
    漏了不报错、不留痕，只在界面上表现为「那条协议的缓存永远是空的」，
    用户会以为是上游没返回缓存字段。

    判据用的是 AST 而不是正则：`_record(...)` 的第 6 个位置参数是 prompt token，
    只要那个表达式引自 `usage`，这次调用就必须带 `usage=` 关键字。gateway 里是
    先 `pt = int(usage.get('prompt_tokens') or 0)` 再传 `pt`，所以从 usage 赋值出来
    的局部名也算数（否则那两处会被漏掉 —— 第一版就是 6 而不是 8）。
    错误/拒绝路径（403/429/400/502）本来就没有 usage，不受影响。
    """

    MODULES = ('server/routers/gateway.py', 'server/routers/responses.py',
               'server/routers/anthropic.py', 'server/routers/playground.py')

    def test_every_usage_reading_call_site_passes_usage(self) -> None:
        import ast

        root = Path(__file__).resolve().parents[2]
        checked: list[str] = []
        offenders: list[str] = []
        for rel in self.MODULES:
            tree = ast.parse((root / rel).read_text(encoding='utf-8'))
            # 从 usage 里取出来的局部变量（如 gateway 的 pt / ct）
            derived = {
                n.targets[0].id
                for n in ast.walk(tree)
                if isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and 'usage' in ast.unparse(n.value)
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or len(node.args) <= 5:
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
                if name != '_record':
                    continue
                tokens = ast.unparse(node.args[5])
                if 'usage' not in tokens and tokens not in derived:
                    continue
                checked.append(f'{rel}:{node.lineno}')
                if 'usage' not in {k.arg for k in node.keywords}:
                    offenders.append(f'{rel}:{node.lineno}')
        # 成功路径一共 8 处（四个模块 × 流式/非流式）。数量对不上说明扫描的判据
        # 过期了（有人改了调用形态），这时必须回来改守卫，而不是让它空转通过。
        self.assertEqual(len(checked), 8,
                         f'扫到的成功路径不是 8 处，守卫可能已空转：{checked}')
        self.assertEqual(offenders, [],
                         '这些调用点读了 usage 却没把 usage 交出去，'
                         '对应协议的缓存三段会永远是空的：' + ', '.join(offenders))


if __name__ == '__main__':
    unittest.main()

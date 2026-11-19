"""用量/日志的版本（realm）归属与过滤。

背景：管理端支持国内版与国际版（共用账号池，按模型名的 `cn:` / `global:`
前缀路由）。界面顶部有版本切换，但**用量统计与请求日志此前完全没有版本维度**
——切到国际版看到的还是两个版本混在一起的数据，页面上只能写一句
「含两种版本，不按版本过滤」来免责。

这次把 realm 落进数据层（`request_logs.realm` / `usage_daily.realm`），
让统计与日志真正能按版本切分。

判据来自**请求的模型名前缀**，不是调用方用哪把密钥：
网关把模型名原样转发给上游，由上游按前缀选择账号池 —— 所以「这次调用走了
哪个版本」完全由前缀决定。无前缀归 cn（存量客户端与历史数据都是这个形态，
归 cn 才能让它们落在原来的那一侧）。

本文件锁住：
  1. 前缀判定（含大小写、空白、裸名）
  2. 记录时写入正确的 realm
  3. 统计/日志按 realm 过滤真的生效
  4. 修复接口（repair / rebuild）**不会丢掉 realm**
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TMP = tempfile.mkdtemp()
os.environ.setdefault('WB_DATA_DIR', _TMP)

from server import db  # noqa: E402


class RealmOfModelTest(unittest.TestCase):
    """前缀判定：上游的路由协议就是 `cn:` / `global:`。"""

    def test_global_prefix(self) -> None:
        for m in ('global:gpt-5.4', 'GLOBAL:gpt-5.4', ' global:gpt-5.4 '):
            self.assertEqual(db.realm_of_model(m), 'global', m)

    def test_cn_prefix_and_bare_name(self) -> None:
        for m in ('cn:glm-5.2', 'glm-5.2', 'deepseek-v4.1-flash'):
            self.assertEqual(db.realm_of_model(m), 'cn', m)

    def test_empty_is_cn(self) -> None:
        """缺 model 归 cn —— 与「历史数据无前缀」同一口径。"""
        for m in (None, '', '   '):
            self.assertEqual(db.realm_of_model(m), 'cn')


class RealmPersistTest(unittest.TestCase):
    """记录时写入 realm，且按 realm 能查出各自的量。"""

    def setUp(self) -> None:
        self._dir = Path(tempfile.mkdtemp())
        self._orig = (db.config.DB_PATH, db._conn)
        db.config.DB_PATH = self._dir / 'realm.db'
        db._conn = None
        db.connect()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = self._orig[1]
        db.config.DB_PATH = self._orig[0]

    def _insert_log(self, model: str, pt: int = 10, ct: int = 5) -> None:
        realm = db.realm_of_model(model)
        db.execute(
            'INSERT INTO request_logs(ts, key_id, ip, model, mapped_model, status, '
            'prompt_tokens, completion_tokens, latency_ms, first_token_ms, ua, error, '
            'stream, credit, realm) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (int(time.time()), 1, '127.0.0.1', model, model, 200, pt, ct, 100, None,
             'ua', None, 1, 0.5, realm),
        )
        db.bump_usage(1, model, pt, ct, 0.5, realm=realm)

    def test_logs_carry_realm(self) -> None:
        self._insert_log('global:gpt-5.4')
        self._insert_log('glm-5.2')
        rows = {r['model']: r['realm'] for r in db.query('SELECT model, realm FROM request_logs')}
        self.assertEqual(rows['global:gpt-5.4'], 'global')
        self.assertEqual(rows['glm-5.2'], 'cn')

    def test_usage_split_by_realm(self) -> None:
        """两个版本走不同行累计，不互相污染。"""
        self._insert_log('global:gpt-5.4', pt=100, ct=50)
        self._insert_log('global:gpt-5.4', pt=100, ct=50)
        self._insert_log('glm-5.2', pt=7, ct=3)
        g = db.query_one("SELECT requests, prompt_tokens FROM usage_daily WHERE realm='global'")
        c = db.query_one("SELECT requests, prompt_tokens FROM usage_daily WHERE realm='cn'")
        self.assertEqual(int(g['requests']), 2)
        self.assertEqual(int(g['prompt_tokens']), 200)
        self.assertEqual(int(c['requests']), 1)
        self.assertEqual(int(c['prompt_tokens']), 7)

    def test_relogin_repair_keeps_realm(self) -> None:
        """rebuild 以日志为准重建 —— 不能把 realm 丢掉（否则版本过滤失效）。"""
        self._insert_log('global:gpt-5.4', pt=11, ct=1)
        self._insert_log('glm-5.2', pt=22, ct=2)
        db.rebuild_usage_from_logs()
        realms = {r['realm'] for r in db.query('SELECT DISTINCT realm FROM usage_daily')}
        self.assertEqual(realms, {'global', 'cn'}, '重建后 realm 丢失/错误')

    def test_backfill_keeps_realm_and_does_not_merge(self) -> None:
        """回填的两个版本同名模型必须是两行（键含 realm）。"""
        # 同名模型分别落在两个版本（理论上不同版本可以有同名模型）
        for m in ('global:shared-model', 'cn:shared-model'):
            realm = db.realm_of_model(m)
            db.execute(
                'INSERT INTO request_logs(ts, key_id, ip, model, mapped_model, status, '
                'prompt_tokens, completion_tokens, latency_ms, first_token_ms, ua, error, '
                'stream, credit, realm) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (int(time.time()), 1, '127.0.0.1', m, m, 200, 5, 5, 10, None,
                 'ua', None, 1, 0.1, realm),
            )
        db.backfill_usage_from_logs()
        rows = db.query("SELECT model, realm, requests FROM usage_daily WHERE model LIKE '%shared-model'")
        self.assertEqual(len(rows), 2, '两个版本的同名模型被合并成一行了')
        self.assertEqual({r['realm'] for r in rows}, {'global', 'cn'})


class StatsRealmFilterTest(unittest.TestCase):
    """统计接口按 realm 过滤（端到端，走 HTTP）。"""

    def setUp(self) -> None:
        import server.security as sec
        from fastapi.testclient import TestClient
        from server.main import app

        self._dir = Path(tempfile.mkdtemp())
        self._orig = (db.config.DB_PATH, db._conn)
        db.config.DB_PATH = self._dir / 'stats.db'
        db._conn = None
        db.connect()
        db.execute("INSERT INTO api_keys(name, key_hash, prefix, enabled, created_at) "
                   "VALUES('k','h','p',1,?)", (int(time.time()),))
        # 两个版本各写 3 次 / 1 次
        for _ in range(3):
            self._log('global:gpt-5.4', 100, 50)
        self._log('glm-5.2', 7, 3)
        app.dependency_overrides[sec.current_user] = lambda: {'username': 't', 'role': 'admin'}
        self.c = TestClient(app)
        self.addCleanup(self._restore)
        self.addCleanup(app.dependency_overrides.clear)

    def _restore(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = self._orig[1]
        db.config.DB_PATH = self._orig[0]

    def _log(self, model: str, pt: int, ct: int) -> None:
        realm = db.realm_of_model(model)
        db.execute(
            'INSERT INTO request_logs(ts, key_id, ip, model, mapped_model, status, '
            'prompt_tokens, completion_tokens, latency_ms, first_token_ms, ua, error, '
            'stream, credit, realm) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (int(time.time()), 1, '127.0.0.1', model, model, 200, pt, ct, 10, None,
             'ua', None, 1, 0.2, realm),
        )
        db.bump_usage(1, model, pt, ct, 0.2, realm=realm)

    def test_summary_filters_by_realm(self) -> None:
        all_ = self.c.get('/api/stats/summary').json()
        gl = self.c.get('/api/stats/summary?realm=global').json()
        cn = self.c.get('/api/stats/summary?realm=cn').json()
        self.assertEqual(all_['today_requests'], 4)
        self.assertEqual(gl['today_requests'], 3)
        self.assertEqual(cn['today_requests'], 1)
        # token：global 3×(100+50)=450；cn 10
        self.assertEqual(gl['today_tokens'], 450)
        self.assertEqual(cn['today_tokens'], 10)

    def test_by_model_filters_by_realm(self) -> None:
        gl = self.c.get('/api/stats/by-model?realm=global').json()
        cn = self.c.get('/api/stats/by-model?realm=cn').json()
        self.assertEqual([m['name'] for m in gl], ['global:gpt-5.4'])
        self.assertEqual([m['name'] for m in cn], ['glm-5.2'])

    def test_by_key_includes_realm_scoped_amounts(self) -> None:
        """密钥不分版本，但按版本时统计的是「该版本上的调用量」——两侧都出现。"""
        gl = self.c.get('/api/stats/by-key?realm=global').json()
        cn = self.c.get('/api/stats/by-key?realm=cn').json()
        self.assertEqual(sum(k['requests'] for k in gl), 3)
        self.assertEqual(sum(k['requests'] for k in cn), 1)

    def test_daily_filters_by_realm(self) -> None:
        gl = self.c.get('/api/stats/daily?days=7&realm=global').json()
        cn = self.c.get('/api/stats/daily?days=7&realm=cn').json()
        self.assertEqual(sum(d['requests'] for d in gl), 3)
        self.assertEqual(sum(d['requests'] for d in cn), 1)

    def test_logs_filter_by_realm(self) -> None:
        gl = self.c.get('/api/logs?realm=global').json()
        cn = self.c.get('/api/logs?realm=cn').json()
        self.assertEqual(gl['total'], 3)
        self.assertEqual(cn['total'], 1)
        # 每条记录带回 realm，供界面标注
        self.assertTrue(all(i['realm'] == 'global' for i in gl['items']))

    def test_null_realm_counts_as_cn(self) -> None:
        """历史记录 realm 为 NULL → 按 cn 归类（与 realm_of_model 一致）。"""
        db.execute(
            'INSERT INTO request_logs(ts, key_id, ip, model, mapped_model, status, '
            'prompt_tokens, completion_tokens, latency_ms, first_token_ms, ua, error, '
            'stream, credit, realm) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
            (int(time.time()), 1, '127.0.0.1', 'old-model', 'old-model', 200, 1, 1,
             10, None, 'ua', None, 1, 0.0),
        )
        cn = self.c.get('/api/logs?realm=cn').json()
        gl = self.c.get('/api/logs?realm=global').json()
        self.assertEqual(cn['total'], 2, 'NULL 的历史记录应算作国内版')
        self.assertEqual(gl['total'], 3)

    def test_no_realm_returns_everything(self) -> None:
        """不传 realm 时保持既有行为（全量），避免破坏旧的调用方。"""
        all_ = self.c.get('/api/logs').json()
        self.assertEqual(all_['total'], 4)


class RebuildWithDirtyDataTest(unittest.TestCase):
    """重建 / 回填统计必须能处理**脏数据**（线上 bug 的回归测试）。

    报障现象：用量页点「重建统计」→ `Internal Server Error`（500）。

    两个独立原因，都在 SQL 的分组与归一化上：

      1. **GROUP BY 复用别名会绑定到源列**。SQLite 解析 `GROUP BY realm` 时，
         因为 FROM 的表里也有名为 realm 的列，该名字优先绑定**源列**，而不是
         输出别名 `COALESCE(realm,'cn')`。于是 `realm IS NULL` 与 `realm='cn'`
         被分成两组，但两组的输出值都是 'cn' —— 写回 usage_daily 时撞主键
         `(day,key_id,model,realm)`。model 列同理。

      2. **空串的归一化两处不一致**：SQL 的 `COALESCE(realm,'cn')` 只处理 NULL，
         而 Python 的 `row['realm'] or 'cn'` 连空串一起兜住。于是 SQL 把它分成
         独立一组、写库时却折成 'cn'，第二次撞键。

    历史日志里 realm 为 NULL 是**常态**（该列是后加的，旧记录全是 NULL），
    所以这不是理论边界 —— 任何有升级历史的部署点这个按钮都会 500。
    """

    def setUp(self) -> None:
        self._dir = Path(tempfile.mkdtemp())
        self._orig = (db.config.DB_PATH, db._conn)
        db.config.DB_PATH = self._dir / 'dirty.db'
        db._conn = None
        db.connect()
        self.addCleanup(self._restore)
        self._key = db.execute(
            "INSERT INTO api_keys(name,key_hash,prefix,enabled,models,realm,created_at) "
            "VALUES('t','h','p',1,'[]','cn',?)", (int(time.time()),))

    def _restore(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = self._orig[1]
        db.config.DB_PATH = self._orig[0]

    def _log(self, model, realm, pt=10, ct=5, credit=1.0) -> None:
        db.execute(
            'INSERT INTO request_logs(ts, key_id, ip, model, mapped_model, status, '
            'prompt_tokens, completion_tokens, latency_ms, first_token_ms, ua, error, '
            'stream, credit, realm) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (int(time.time()), self._key, '127.0.0.1', model, model, 200, pt, ct,
             100, None, 'ua', None, 1, credit, realm),
        )

    def test_rebuild_tolerates_null_and_empty_realm(self) -> None:
        """realm 为 NULL / 空串时必须能重建（旧记录全是 NULL）。"""
        self._log('glm-5.2', 'cn')
        self._log('glm-5.2', None)      # 历史记录：该列是后加的
        self._log('glm-5.2', '')
        out = db.rebuild_usage_from_logs()   # 修复前在这里抛 IntegrityError
        self.assertEqual(out['rows_after'], 1, '三种形态应归并成同一行')
        row = db.query_one('SELECT realm, requests FROM usage_daily')
        self.assertEqual(row['realm'], 'cn')
        self.assertEqual(int(row['requests']), 3)

    def test_rebuild_tolerates_null_and_empty_model(self) -> None:
        """model 为 NULL / 空串时同理（两者归一后是同一行）。"""
        self._log(None, 'cn')
        self._log('', 'cn')
        db.rebuild_usage_from_logs()
        self.assertEqual(db.query_one('SELECT COUNT(*) c FROM usage_daily')['c'], 1)
        self.assertEqual(int(db.query_one('SELECT requests FROM usage_daily')['requests']), 2)

    def test_rebuild_keeps_realms_separate(self) -> None:
        """归一化不能把两个版本合并 —— 它们本来就是不同的行。"""
        self._log('glm-5.2', 'cn')
        self._log('glm-5.2', 'global')
        db.rebuild_usage_from_logs()
        realms = {r['realm'] for r in db.query('SELECT realm FROM usage_daily')}
        self.assertEqual(realms, {'cn', 'global'})

    def test_rebuild_total_matches_logs(self) -> None:
        """重建后的请求数必须与日志条数一致（不重不漏）。"""
        for realm in ('cn', None, '', 'global', None, 'cn'):
            self._log('m', realm)
        out = db.rebuild_usage_from_logs()
        total = db.query_one('SELECT SUM(requests) r FROM usage_daily')['r']
        logs = db.query_one('SELECT COUNT(*) c FROM request_logs')['c']
        self.assertEqual(int(total), int(logs))
        self.assertEqual(out['requests_delta'], logs)

    def test_backfill_tolerates_dirty_data_and_is_idempotent(self) -> None:
        """「修复统计」走的是回填路径，同样必须扛住脏数据且可重复执行。"""
        self._log('glm-5.2', 'cn')
        self._log('glm-5.2', None)
        self._log('glm-5.2', '')
        first = db.backfill_usage_from_logs()    # 修复前同样会抛
        self.assertEqual(db.query_one('SELECT COUNT(*) c FROM usage_daily')['c'], 1)
        second = db.backfill_usage_from_logs()
        self.assertEqual(second['requests'], 0, '重复执行不应重复计数')
        # 回填结果与直接重建应当一致
        self.assertEqual(first['requests'], 3)

    def test_rebuild_is_atomic_on_failure(self) -> None:
        """重建失败时必须**整体回滚**，不能留下被清空的统计表。

        这是线上现象的另一半：此前是「先 DELETE（已提交）再逐条 INSERT」，
        插入中途失败就把 usage_daily 清空/半写，而 request_logs 完好 ——
        用户看到「今天有 77 次调用、统计却是 0」，而且每点一次重建就再破坏一次。
        原子化之后，失败即回滚，原有统计原样保留。
        """
        self._log('glm-5.2', 'cn')
        self._log('glm-5.2', 'global')
        db.rebuild_usage_from_logs()
        n_before = db.query_one('SELECT COUNT(*) c FROM usage_daily')['c']
        self.assertEqual(n_before, 2)

        # 把表换成只接受 realm='cn' 的版本 → 插 global 那行必然失败
        db._conn.execute('DROP TABLE usage_daily')
        db._conn.execute(
            'CREATE TABLE usage_daily(day TEXT NOT NULL, key_id INTEGER NOT NULL, '
            'model TEXT NOT NULL, requests INTEGER, prompt_tokens INTEGER, '
            'completion_tokens INTEGER, credit REAL, realm TEXT, '
            "CHECK (realm = 'cn'))")
        db._conn.execute(
            "INSERT INTO usage_daily VALUES('2026-01-01',1,'keep',99,0,0,0,'cn')")
        db._conn.commit()

        with self.assertRaises(Exception):
            db.rebuild_usage_from_logs()

        row = db.query_one('SELECT model, requests FROM usage_daily')
        self.assertEqual(row['model'], 'keep', '失败后原有数据被清掉了（没有回滚）')
        self.assertEqual(int(row['requests']), 99)

    def test_group_by_binds_to_expression_not_column(self) -> None:
        """直接钉住那个 SQLite 行为，免得后人又把 GROUP BY 写回别名。

        `GROUP BY realm`（别名与源列同名）会绑定源列，把 NULL 与 'cn' 分成两组；
        `GROUP BY COALESCE(NULLIF(realm,''),'cn')` 才与投影一致。
        """
        c = db._conn
        c.execute('CREATE TEMP TABLE _probe(realm TEXT)')
        c.executemany('INSERT INTO _probe VALUES(?)', [('cn',), (None,), ('',)])
        bad = c.execute(
            "SELECT COALESCE(NULLIF(realm,''),'cn') AS realm, COUNT(*) n "
            "FROM _probe GROUP BY realm").fetchall()
        good = c.execute(
            "SELECT COALESCE(NULLIF(realm,''),'cn') AS realm, COUNT(*) n "
            "FROM _probe GROUP BY COALESCE(NULLIF(realm,''),'cn')").fetchall()
        self.assertEqual(len(bad), 3, '别名分组会切成三组（NULL/空串/cn 各一组）')
        self.assertEqual(len(good), 1, '按表达式分组才是正确的一组')
        self.assertEqual(good[0][1], 3)


class RealmColumnMigrationTest(unittest.TestCase):
    """老库升级：加列必须成功，且历史行有合理的默认归类。"""

    def test_migration_adds_columns(self) -> None:
        import sqlite3
        d = Path(tempfile.mkdtemp())
        old_path = db.config.DB_PATH
        old_conn = db._conn
        try:
            # 造一个「没有 realm 列」的老库
            db.config.DB_PATH = d / 'old.db'
            db._conn = None
            conn = sqlite3.connect(str(db.config.DB_PATH))
            conn.executescript(
                'CREATE TABLE request_logs (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, '
                'key_id INTEGER, ip TEXT, model TEXT, mapped_model TEXT, status INTEGER, '
                'prompt_tokens INTEGER, completion_tokens INTEGER, latency_ms INTEGER, '
                'ua TEXT, error TEXT, stream INTEGER, credit REAL);'
                'CREATE TABLE usage_daily (day TEXT NOT NULL, key_id INTEGER NOT NULL, '
                'model TEXT NOT NULL, requests INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0, '
                'completion_tokens INTEGER DEFAULT 0, credit REAL DEFAULT 0, '
                'PRIMARY KEY (day, key_id, model));'
            )
            conn.commit()
            conn.close()

            db.connect()  # 触发迁移
            log_cols = {r[1] for r in db._conn.execute('PRAGMA table_info(request_logs)')}
            use_cols = {r[1] for r in db._conn.execute('PRAGMA table_info(usage_daily)')}
            self.assertIn('realm', log_cols, 'request_logs 未加上 realm 列')
            self.assertIn('realm', use_cols, 'usage_daily 未加上 realm 列')
        finally:
            if db._conn is not None:
                db._conn.close()
            db._conn = old_conn
            db.config.DB_PATH = old_path


if __name__ == '__main__':
    unittest.main()


class BumpUsageRealmOverrideTest(unittest.TestCase):
    """`bump_usage(realm=...)` 的显式传参必须优先于按模型名推导。

    为什么这值得单独测：在当前调用链里两者**恰好一致**（网关切传的就是
    db.realm_of_model 的结果），所以「忽略参数、只按模型推导」的实现也能通过
    其余全部用例 —— 这一点是被反证试出来的。而语义上「显式指定优先」是有
    意义的：将来若改成按密钥或请求头决定版本，调用方就得能覆盖推导值。
    """

    def setUp(self) -> None:
        self._dir = Path(tempfile.mkdtemp())
        self._orig = (db.config.DB_PATH, db._conn)
        db.config.DB_PATH = self._dir / 'override.db'
        db._conn = None
        db.connect()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = self._orig[1]
        db.config.DB_PATH = self._orig[0]

    def test_explicit_realm_wins_over_prefix(self) -> None:
        # 模型名没有前缀（推导=cn），但显式要求记到 global
        db.bump_usage(1, 'glm-5.2', 10, 5, 0.1, realm='global')
        rows = db.query('SELECT realm FROM usage_daily')
        self.assertEqual([r['realm'] for r in rows], ['global'],
                         '显式传入的 realm 被忽略了 —— 应以参数为准')

    def test_fallback_to_prefix_when_omitted(self) -> None:
        """不传 realm 时按模型前缀兜底（调用方遗漏时不至于全部归 cn）。"""
        db.bump_usage(1, 'global:gpt-5.4', 10, 5, 0.1)
        rows = db.query('SELECT realm FROM usage_daily')
        self.assertEqual([r['realm'] for r in rows], ['global'])

    def test_invalid_realm_falls_back_to_prefix(self) -> None:
        """非法值（非 cn/global）不能写进库 —— 退回推导值。"""
        db.bump_usage(1, 'global:gpt-5.4', 10, 5, 0.1, realm='weird')
        rows = db.query('SELECT realm FROM usage_daily')
        self.assertEqual([r['realm'] for r in rows], ['global'])


class UpstreamPoolCountsTest(unittest.TestCase):
    """仪表盘「反代上游」面板的计数来源。

    这里锁的是一个**曾经全显示 0** 的缺陷，根因值得记下来：
      1. 最初读 `upstream.healthy` —— 那是**全局**汇总，切到国际版会显示
         两个版本加起来的数，于是改成从账号明细自己数；
      2. 但账号明细里**根本没有 healthy 字段**（它是汇总层才有的），
         `Boolean(undefined)` 恒为 false → 健康账号永远显示 0（用户截图所示）；
      3. 上游其实已经按版本分好组了：`/status` 的 `realm_totals.cn` / `.global`。

    上游 /status 的响应形状（读其 internal/server/handler.go 确认）：
        {"accounts":[...], "total":N, "healthy":N, "cooling":N, "disabled":N,
         "realm_totals":{"cn":{...},"global":{...}}, ...}
    其中 healthy/cooling/disabled 是**汇总键**，账号条目上没有。

    前端是 TS，测试在 Python 侧——因此这里用与前端**同一套判定逻辑**做核对，
    并在 test_frontend_uses_realm_totals 里断言前端确实读了 realm_totals
    （防止有人改回去读明细）。
    """

    UPSTREAM = {
        'connected': True,
        'accounts': [
            {'uid': 'g1', 'realm': 'global', 'cooling': False, 'disabled': False},
            {'uid': 'c1', 'realm': 'cn', 'cooling': True, 'disabled': False},
            {'uid': 'c2', 'realm': 'cn', 'cooling': False, 'disabled': False},
        ],
        'total': 3, 'healthy': 2, 'cooling': 1, 'disabled': 0,
        'realm_totals': {
            'cn': {'total': 2, 'healthy': 1, 'cooling': 1, 'disabled': 0,
                   'in_flight_full': 0},
            'global': {'total': 1, 'healthy': 1, 'cooling': 0, 'disabled': 0,
                       'in_flight_full': 0},
        },
    }

    @staticmethod
    def _compute_pool(upstream: dict, realm: str) -> dict:
        """与 dashboard/page.tsx 的 pool 计算保持一致。"""
        per = (upstream.get('realm_totals') or {}).get(realm)
        if per:
            return {'total': per['total'], 'healthy': per['healthy'],
                    'cooling': per['cooling'], 'disabled': per['disabled'],
                    'known': True}
        has_top = isinstance(upstream.get('total'), int)
        return {'total': upstream.get('total', 0), 'healthy': upstream.get('healthy', 0),
                'cooling': upstream.get('cooling', 0), 'disabled': upstream.get('disabled', 0),
                'known': has_top, 'globalOnly': has_top}

    def test_global_view_uses_global_counts(self) -> None:
        p = self._compute_pool(self.UPSTREAM, 'global')
        self.assertEqual((p['healthy'], p['cooling'], p['disabled']), (1, 0, 0))

    def test_cn_view_uses_cn_counts(self) -> None:
        p = self._compute_pool(self.UPSTREAM, 'cn')
        self.assertEqual((p['healthy'], p['cooling'], p['disabled']), (1, 1, 0))

    def test_counts_are_not_all_zero(self) -> None:
        """回归断言：这个面板曾经恒显示 0（账号明细里没有 healthy 字段）。"""
        for realm in ('cn', 'global'):
            p = self._compute_pool(self.UPSTREAM, realm)
            self.assertGreater(p['healthy'], 0,
                               f'{realm} 视图健康账号为 0 —— 又能是读了明细里的 healthy？')

    def test_falls_back_to_global_totals_with_flag(self) -> None:
        """老上游没有 realm_totals 时退回全局汇总，并标记口径。"""
        old = {k: v for k, v in self.UPSTREAM.items() if k != 'realm_totals'}
        p = self._compute_pool(old, 'global')
        self.assertTrue(p['globalOnly'], '应标记为全局口径')
        self.assertEqual(p['healthy'], 2)

    def test_frontend_uses_realm_totals(self) -> None:
        """前端必须**从 realm_totals 取该版本的计数**，而不是从账号明细自己数。

        为什么这是回归测试：这个面板曾**恒显示 0**。根因是从账号明细统计时读了
        条目上的 healthy —— 而**账号明细里根本没有这个字段**（它是 /status 的
        汇总层字段），布尔转换恒为 false。上游其实早已按版本分好组
        （realm_totals.cn / .global），直接用就不会错。

        断言写成「必须存在 perRealm.healthy 的读取」，而不是「必须没有 .healthy」——
        后者会误伤两处**合法**用法：顶层汇总的 `upstream?.healthy`（老上游回退）
        与展示用的 `pool.healthy`。正向断言既精确又不会被无关代码绊倒。

        2026-09-27 跟随后续改动：这段判定搬进了 `web/lib/account-status.ts` 的
        `poolTotals()`（首页要按分组逐块显示池计数，判定只能有一处）。所以现在断言
        **那个模块**读了 realm_totals，并额外要求**仪表盘不再自己读** —— 两边各算
        一份正是当初「首页说在线、账号页说未加载」的成因。
        """
        root = Path(__file__).resolve().parents[2] / 'web'
        src = (root / 'lib' / 'account-status.ts').read_text(encoding='utf-8')
        # 1) 真的读了上游按版本分组的计数
        self.assertIn('.realm_totals?.[realm]', src,
                      'account-status 没有实际读 realm_totals —— 上游已按版本分好组，别自己数')
        # 2) 健康数取自该分组（而不是从明细条目上取）
        self.assertIn('perRealm.healthy', src, '没有从 realm_totals 取 healthy')
        self.assertIn('perRealm.cooling', src, '没有从 realm_totals 取 cooling')
        self.assertIn('perRealm.disabled', src, '没有从 realm_totals 取 disabled')
        # 3) 不得再出现「遍历账号明细、在条目上取 healthy」的写法
        #    （这是产生「全 0」的那个 bug；用明细条目变量名定位，避免误伤汇总字段）
        import re
        for pat, why in (
            (r'\(\s*it\s*\)\s*=>[^\n]*\bhealthy\b', '在账号明细条目 it 上取 healthy'),
            (r'\bit\s*\.\s*healthy\b', '在账号明细条目 it 上取 healthy'),
        ):
            self.assertIsNone(re.search(pat, src), f'{why} —— 该字段不存在，会恒为 0')
        # 4) 仪表盘用这个函数，且不再自己算一份（判定只允许有一处）
        page = (root / 'app' / '(main)' / 'dashboard' / 'page.tsx').read_text(encoding='utf-8')
        self.assertIn('poolTotals(', page,
                      '仪表盘没有用共用的 poolTotals() —— 池计数判定又分家了')
        self.assertNotIn('.realm_totals', page,
                         '仪表盘自己又读了一次 realm_totals —— 判定必须只有一处'
                         '（两处各算一份，正是「首页说在线、账号页说未加载」的成因）')

    def test_type_declares_realm_totals(self) -> None:
        ts = (Path(__file__).resolve().parents[2] / 'web' / 'lib' / 'types.ts'
              ).read_text(encoding='utf-8')
        self.assertIn('realm_totals', ts, 'UpstreamStatus 类型缺 realm_totals')

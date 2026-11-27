"""请求日志的「首帧延迟」与「首字延迟」分开记（issue #493）。

## 需求现场

排查「同一模型换账号后有时快有时慢」时发现：同一次请求，上游 wb2api 逐单行里的
`TTFB` 是 1.48s，而管理面板记的「首字延迟」是 125s（2026-11-26 实测）。

两边都没记错，量的是两件事：

  · 首帧 = 第一个**带负载的 `data:` 帧**到达（可能只有一个 role 字段）；
  · `first_token_ms` = 第一个**含正文或推理**的 delta。

`effort=max` 的请求推理阶段很长，中间可能一个 delta 都没有，于是两个时刻能差上
百秒。要回答「上游多久开始回话」（建连慢？排队？），只能看前者。

## 这里钉住的

  · 口径：首帧只认**带负载的 `data:` 帧**。保活注释帧（`: keepalive`）与
    `data: [DONE]` 都不算 —— 前者是排队时的心跳，把它算成首帧会让这个数字退化
    成「建连延迟」；后者只是结束标记，不代表上游产出过内容。
    这一条必须用**真实流**跑出来：源码字符串断言挡不住口径写错，
    「第一个非空 chunk」在源码里看着也像是对的。
  · `first_frame_ms` 单独一列：建表、增量迁移（老库 ALTER 补列）都要有，
    且重复 `connect()` 不能重复加列、不能给旧行编造数值。
  · 写库走 `add_request_log(**fields)`，漏传一列**不会报错**，只会永远是 NULL
    —— 所以必须有回读断言，不能只测「函数没抛异常」。
  · NULL 语义与 `first_token_ms` 一致：没采集到就是 NULL，不是 0
    （0 是合法值：上游同一秒内就回帧，界面靠 `!= null` 区分）。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, security  # noqa: E402
from server.routers import gateway as G  # noqa: E402

# 只有 role 的帧：上游建连后常见的首帧，没有任何正文/推理内容。
ROLE_FRAME = b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
# 正文帧：`_scan_sse` 认它才是「首个 token」。
CONTENT_FRAME = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
# SSE 注释帧：排队期间的心跳，不是数据帧。
KEEPALIVE_FRAME = b': keepalive\n\n'
DONE_FRAME = b'data: [DONE]\n\n'

# 上游「想了想」的时长。要明显大于计时抖动，又不能让测试变慢。
THINK_S = 0.30


class _Upstream:
    """可编排的 SSE 上游：`script` 是 (延迟秒, 字节) 序列。"""

    status_code = 200
    headers = {'content-type': 'text/event-stream'}

    def __init__(self, script: list[tuple[float, bytes]]) -> None:
        self.script = script
        self.closed = False

    async def aiter_bytes(self):
        for delay, payload in self.script:
            if delay:
                await asyncio.sleep(delay)
            yield payload

    async def aclose(self) -> None:
        self.closed = True


class _Client:
    def __init__(self, resp: _Upstream) -> None:
        self._resp = resp

    def build_request(self, *a, **kw):
        return object()

    async def send(self, req, stream=True):
        return self._resp

    async def post(self, url, **kw):
        return self._resp

    async def aclose(self) -> None:
        return None


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

    def _row(self) -> dict:
        return db.query_one('SELECT * FROM request_logs ORDER BY id DESC LIMIT 1')


def _log(**over) -> None:
    """按最省的姿势插一条 request_logs，只为测列与迁移。"""
    base = dict(
        ts=1, key_id=None, ip='1.2.3.4', model='m', mapped_model='',
        status=200, prompt_tokens=1, completion_tokens=1, latency_ms=100,
        first_token_ms=None, first_frame_ms=None, ua='UA', error=None,
        stream=1, credit=None, realm='cn', account=None, cache_hit_tokens=0,
        cache_miss_tokens=0, cache_write_tokens=0, effort=None,
        channel='api', account_expected=1,
    )
    base.update(over)
    db.add_request_log(**base)


# ── 列与写库 ────────────────────────────────────────────────


class FirstFrameColumnTest(_DbCase):
    def test_column_exists(self) -> None:
        cols = {r['name'] for r in db.query('PRAGMA table_info(request_logs)')}
        self.assertIn('first_frame_ms', cols)
        self.assertIn('first_token_ms', cols)

    def test_roundtrip_keeps_both(self) -> None:
        """两列必须各自落到自己的位置：值张冠李戴在界面上看不出来。"""
        _log(first_token_ms=7000, first_frame_ms=1480)
        row = self._row()
        self.assertEqual(row['first_frame_ms'], 1480)
        self.assertEqual(row['first_token_ms'], 7000)

    def test_zero_is_a_real_value(self) -> None:
        """0 是合法值（同一秒内回帧），不能被当成「没值」写成 NULL。"""
        _log(first_token_ms=0, first_frame_ms=0)
        row = self._row()
        self.assertEqual(row['first_frame_ms'], 0)
        self.assertEqual(row['first_token_ms'], 0)

    def test_null_when_not_collected(self) -> None:
        """不传就是 NULL，不能默认成 0（0 会被界面读成「秒回」）。"""
        _log(stream=0)
        row = self._row()
        self.assertIsNone(row['first_frame_ms'])
        self.assertIsNone(row['first_token_ms'])

    def test_reconnect_is_idempotent_and_old_rows_stay_null(self) -> None:
        """老库缺列的迁移：补一次列，重复 connect() 不重复加，旧行保持 NULL。"""
        # 1) 去掉这一列，模拟升级前的库（SQLite 3.35+ 支持 DROP COLUMN）。
        db._conn.execute('ALTER TABLE request_logs DROP COLUMN first_frame_ms')
        db._conn.commit()
        cols = {r['name'] for r in db.query('PRAGMA table_info(request_logs)')}
        self.assertNotIn('first_frame_ms', cols, 'DROP COLUMN 未生效，用例前提不成立')
        # 2) 插入一条「老版本」的记录（那时还没有这一列）。
        db._conn.execute(
            'INSERT INTO request_logs(ts, ip, model, status, latency_ms, '
            'first_token_ms, stream) VALUES(?, ?, ?, ?, ?, ?, ?)',
            (100, '1.1.1.1', 'm', 200, 5000, 4000, 1),
        )
        db._conn.commit()
        db._conn.close()
        db._conn = None

        # 3) 升级：connect() 应当补列。
        db.connect()
        cols = [r['name'] for r in db.query('PRAGMA table_info(request_logs)')]
        self.assertEqual(
            1, cols.count('first_frame_ms'),
            '迁移应恰好补一列（重复加列说明幂等判定失效）',
        )
        # 旧行不能被人为填一个数字：那时根本没采集过。
        old = db.query_one('SELECT first_frame_ms FROM request_logs WHERE ts=100')
        self.assertIsNone(old['first_frame_ms'], '旧记录的首帧必须是 NULL')

        # 4) 再 connect() 一次（进程重启 / 热重载）：不报错、也不多出第二列。
        db._conn.close()
        db._conn = None
        db.connect()
        cols = [r['name'] for r in db.query('PRAGMA table_info(request_logs)')]
        self.assertEqual(1, cols.count('first_frame_ms'))
        # 补列之后新写入的记录能正常落值。
        _log(ts=101, latency_ms=900, first_token_ms=800, first_frame_ms=120)
        self.assertEqual(120, self._row()['first_frame_ms'])


class RecordWiringTest(unittest.TestCase):
    """`_record` 必须把 first_frame 一路带到 add_request_log。"""

    def test_record_passes_first_frame(self) -> None:
        captured: dict = {}

        def fake_add(**kw):
            captured.update(kw)

        with mock.patch.object(db, 'add_request_log', side_effect=fake_add), \
                mock.patch.object(db, 'realm_of_model', return_value='cn'):
            G._record(
                None, '1.2.3.4', 'deepseek-v4.1-flash', '', 200, 10, 5,
                9000, 'UA', None, True,
                first_token=7000, first_frame=1480, effort='max',
            )
        self.assertEqual(captured.get('first_frame_ms'), 1480)
        self.assertEqual(captured.get('first_token_ms'), 7000)

    def test_record_defaults_frame_to_none(self) -> None:
        captured: dict = {}

        def fake_add(**kw):
            captured.update(kw)

        with mock.patch.object(db, 'add_request_log', side_effect=fake_add), \
                mock.patch.object(db, 'realm_of_model', return_value='cn'):
            G._record(
                None, '1.2.3.4', 'deepseek-v4.1-flash', '', 200, 10, 5,
                9000, 'UA', None, True, first_token=7000,
            )
        self.assertIsNone(captured.get('first_frame_ms'))


# ── 口径：什么才算首帧 ──────────────────────────────────────


class HasDataFrameTest(unittest.TestCase):
    def test_keepalive_is_not_a_frame(self) -> None:
        self.assertFalse(G._has_data_frame(': keepalive\n\n'))

    def test_done_is_not_a_frame(self) -> None:
        self.assertFalse(G._has_data_frame('data: [DONE]\n\n'))

    def test_empty_data_is_not_a_frame(self) -> None:
        self.assertFalse(G._has_data_frame('data:\n\n'))
        self.assertFalse(G._has_data_frame('data:   \n\n'))

    def test_role_only_frame_is_a_frame(self) -> None:
        self.assertTrue(G._has_data_frame(ROLE_FRAME.decode()))

    def test_empty_stream_is_not_a_frame(self) -> None:
        self.assertFalse(G._has_data_frame(''))
        self.assertFalse(G._has_data_frame('\n\n'))

    def test_frame_after_keepalive_is_found(self) -> None:
        blob = KEEPALIVE_FRAME.decode() + KEEPALIVE_FRAME.decode() + ROLE_FRAME.decode()
        self.assertTrue(G._has_data_frame(blob))

    def test_split_line_pieces(self) -> None:
        """`data:` 被 chunk 边界切开时，单独两半都不是帧 —— 这正是调用方
        需要拼残片的原因，这里钉住两半各自的判定结果。"""
        self.assertFalse(G._has_data_frame('data'))
        self.assertFalse(G._has_data_frame('{"choices":[]}\n\n'))
        self.assertTrue(G._has_data_frame('data' + ': {"choices":[]}'))


# ── 端到端：真实流跑一遍 ────────────────────────────────────


class _StreamCase(unittest.TestCase):
    """起一个假上游 + TestClient，把请求真正打完，再从库里回读。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        self._orig_idle = config.STREAM_IDLE_TIMEOUT
        config.DB_PATH = Path(self._tmp.name) / 'e2e.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        # 看门狗不能被 300ms 的「思考」误触发；本用例只关心打点口径。
        config.STREAM_IDLE_TIMEOUT = 60
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)
        self.token = keysvc.create_key('t')['key']

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        config.STREAM_IDLE_TIMEOUT = self._orig_idle
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _chat(self, script: list[tuple[float, bytes]]):
        upstream = _Upstream(script)
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: _Client(upstream)):
            r = self.client.post(
                '/v1/chat/completions',
                headers={'Authorization': f'Bearer {self.token}'},
                json={'model': 'glm-5.2',
                      'messages': [{'role': 'user', 'content': 'hi'}],
                      'stream': True},
            )
        return r, upstream

    def _post(self, path: str, body: dict, script: list[tuple[float, bytes]],
              headers: dict | None = None):
        upstream = _Upstream(script)
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: _Client(upstream)):
            r = self.client.post(
                path, json=body,
                headers=headers or {'Authorization': f'Bearer {self.token}'},
            )
        return r, upstream

    def _row(self) -> dict:
        row = db.query_one(
            'SELECT first_frame_ms, first_token_ms, error FROM request_logs '
            'ORDER BY id DESC LIMIT 1')
        self.assertIsNotNone(row, '流式请求必须落一条日志')
        return row


class RoleOnlyFirstFrameTest(_StreamCase):
    def test_frame_precedes_token_and_both_are_stored(self) -> None:
        """上游先给 role-only 帧，想 ~300ms 后才给正文帧。

        这正是 issue #493 的现场：首帧早就到了，首字还在等推理。
        """
        r, upstream = self._chat([
            (0.0, ROLE_FRAME),
            (THINK_S, CONTENT_FRAME),
            (0.0, DONE_FRAME),
        ])
        self.assertEqual(200, r.status_code, r.text[:300])
        self.assertTrue(upstream.closed)

        row = self._row()
        first_frame, first_token = row['first_frame_ms'], row['first_token_ms']
        self.assertFalse((row['error'] or '').strip(), '正常流不应记错误')
        self.assertIsNotNone(first_frame, 'role-only 帧必须算首帧')
        self.assertIsNotNone(first_token, '正文帧必须算首字')
        # 首帧在 role 帧到达时就该定下，不该等到正文。
        self.assertLess(first_frame, int(THINK_S * 1000),
                        f'首帧 {first_frame}ms 太晚：像是把正文当首帧了')
        self.assertGreaterEqual(first_token, int(THINK_S * 1000 * 0.6),
                                f'首字 {first_token}ms 太早：正文帧之前不该有首字')
        self.assertLess(first_frame, first_token,
                        '首帧必须早于首字（这是两列并存的意义）')


class KeepaliveFirstFrameTest(_StreamCase):
    def test_keepalive_is_not_counted_as_first_frame(self) -> None:
        """保活帧先到、数据帧 300ms 后才到：首帧必须落在数据帧那一刻。

        如果按「第一个非空 chunk」打点，这里会得到 ~0ms —— 那是建连延迟，
        与上游 TTFB 无关，还会让「排队很久」的请求看起来很快。
        """
        r, _ = self._chat([
            (0.0, KEEPALIVE_FRAME),
            (0.0, KEEPALIVE_FRAME),
            (THINK_S, ROLE_FRAME),
            (0.0, CONTENT_FRAME),
            (0.0, DONE_FRAME),
        ])
        self.assertEqual(200, r.status_code, r.text[:300])

        row = self._row()
        first_frame = row['first_frame_ms']
        self.assertIsNotNone(first_frame, '数据帧到达后应记首帧')
        self.assertGreaterEqual(
            first_frame, int(THINK_S * 1000 * 0.6),
            f'首帧 {first_frame}ms 把保活帧算进去了',
        )
        self.assertLess(first_frame, int(THINK_S * 1000 * 3),
                        f'首帧 {first_frame}ms 明显偏晚，打点位置可能不在首帧处')
        self.assertIsNotNone(row['first_token_ms'])

    def test_keepalive_only_stream_has_no_frame(self) -> None:
        """只有保活帧的流（上游挂住但还在心跳）：没有数据帧，两列都是 NULL。"""
        r, _ = self._chat([
            (0.0, KEEPALIVE_FRAME),
            (0.0, KEEPALIVE_FRAME),
            (0.0, KEEPALIVE_FRAME),
        ])
        self.assertEqual(200, r.status_code, r.text[:300])
        row = self._row()
        self.assertIsNone(row['first_frame_ms'])
        self.assertIsNone(row['first_token_ms'])


class EmptyStreamTest(_StreamCase):
    def test_done_only_stream_is_null(self) -> None:
        """只有 `[DONE]` 的空流：上游一个内容都没有，`[DONE]` 不是首帧。"""
        r, _ = self._chat([
            (0.0, KEEPALIVE_FRAME),
            (THINK_S, DONE_FRAME),
        ])
        self.assertEqual(200, r.status_code, r.text[:300])
        row = self._row()
        self.assertIsNone(row['first_frame_ms'],
                          '`data: [DONE]` 不是首帧，应记 NULL')
        self.assertIsNone(row['first_token_ms'])


class AllProtocolsFirstFrameTest(_StreamCase):
    """三条流式路径都要打点：漏一条的表现是「某个协议的首帧永远是 —」。

    三条路径的上游都是同一套 chat SSE（Anthropic / Responses 是翻译层），
    所以用同一段脚本跑，逐协议断言首帧早于首字、且都落库。
    """

    SCRIPT = [
        (0.0, KEEPALIVE_FRAME),
        (THINK_S, ROLE_FRAME),
        # role 与正文之间留 50ms：毫秒精度下两者才分得开（真实上游也不会同刻度）。
        (0.05, CONTENT_FRAME),
        (0.0, DONE_FRAME),
    ]

    def _assert_frame_before_token(self, label: str) -> None:
        row = self._row()
        frame, token = row['first_frame_ms'], row['first_token_ms']
        self.assertIsNotNone(frame, f'{label}: 首帧没有落库')
        self.assertIsNotNone(token, f'{label}: 首字没有落库')
        self.assertGreaterEqual(
            frame, int(THINK_S * 1000 * 0.6),
            f'{label}: 首帧 {frame}ms 把保活帧算进去了',
        )
        self.assertLess(frame, token, f'{label}: 首帧必须早于首字')

    def test_chat(self) -> None:
        r, _ = self._chat(self.SCRIPT)
        self.assertEqual(200, r.status_code, r.text[:300])
        self._assert_frame_before_token('chat')

    def test_anthropic_messages(self) -> None:
        r, _ = self._post(
            '/v1/messages',
            {'model': 'glm-5.2', 'max_tokens': 16, 'stream': True,
             'messages': [{'role': 'user', 'content': 'hi'}]},
            self.SCRIPT,
            headers={'x-api-key': self.token, 'anthropic-version': '2023-06-01'},
        )
        self.assertEqual(200, r.status_code, r.text[:300])
        self._assert_frame_before_token('anthropic')

    def test_responses(self) -> None:
        r, _ = self._post(
            '/v1/responses',
            {'model': 'glm-5.2', 'input': 'hi', 'stream': True},
            self.SCRIPT,
        )
        self.assertEqual(200, r.status_code, r.text[:300])
        self._assert_frame_before_token('responses')


class InsertShapeTest(_DbCase):
    """`add_request_log` 的列名 / 占位符 / 取值三处必须一一对齐。

    漏一列不会抛异常（全是 `fields.get`），只会永久写 NULL —— 上面的回读
    测试负责发现「漏了」，这里在改 INSERT 时就把数量钉死。
    """

    def test_column_placeholder_and_value_counts(self) -> None:
        src = Path(db.__file__).read_text(encoding='utf-8')
        start = src.index('INSERT INTO request_logs')
        seg = src[start:start + 2000]
        cols_sql = seg.split('VALUES(', 1)[0]
        placeholders = seg.split('VALUES(', 1)[1].split(')', 1)[0].count('?')
        # 三处数量一致：24 列 / 24 个 ? / 24 个取值（取值个数由真跑覆盖，
        # 数量不对 sqlite 会直接报 OperationalError）。
        column_count = cols_sql.split('request_logs(', 1)[1].count(',') + 1
        self.assertEqual(column_count, placeholders,
                         '列名数与占位符数不一致')
        self.assertIn('first_frame_ms', cols_sql)
        self.assertIn('first_token_ms', cols_sql)
        self.assertLess(
            seg.index("fields.get('first_token_ms')"),
            seg.index("fields.get('first_frame_ms')"),
            '取值顺序必须与列名顺序一致（token 在前、frame 在后）',
        )
        # 真跑一次，确认三元组的实际长度也对得上（占位符数量与取值个数）。
        _log(first_token_ms=7, first_frame_ms=3)
        row = self._row()
        self.assertEqual(row['first_token_ms'], 7)
        self.assertEqual(row['first_frame_ms'], 3)


if __name__ == '__main__':
    unittest.main()

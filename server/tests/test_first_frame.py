"""首帧延迟（first_frame_ms）：只认可用的 `data:` 帧。

## 为什么不是「第一个非空 chunk」

上游开保活时会先发 SSE 注释帧（`: keepalive\\n\\n`）。按 chunk 打点的话，它比真正
的第一帧更早，记出来的「首帧」比上游 TTFB 还小，两边不可比（#485 里有实测形态）。
所以口径与 #485 的空闲看门狗一致：**只有 `data:` 行且 payload 非空、非 `[DONE]`
才算一帧**。

## 这里钉住的边界

  · 保活注释先到 → 不算首帧；
  · 只有 `[DONE]` → 不算首帧（空流）；
  · 空 chunk / 空 payload → 不算；
  · role-only 帧 → **算**首帧（它是真实的第一帧，只是没有正文）；这正是它与
    `first_token_ms`（要等含正文/推理的 delta）的区别。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import gateway as G  # noqa: E402


class CarriesDataFrameTest(unittest.TestCase):
    def test_keepalive_comment_not_counted(self) -> None:
        self.assertFalse(G._carries_data_frame(': keepalive\n\n'))
        self.assertFalse(G._carries_data_frame('\n\n'))
        self.assertFalse(G._carries_data_frame(''))

    def test_done_only_not_counted(self) -> None:
        self.assertFalse(G._carries_data_frame('data: [DONE]\n\n'))
        self.assertFalse(G._carries_data_frame('data:\n\n'))

    def test_role_only_frame_counted(self) -> None:
        # 真实的第一帧，只是没有正文：算首帧，但不该算首字
        self.assertTrue(G._carries_data_frame(
            'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'))

    def test_content_frame_counted(self) -> None:
        self.assertTrue(G._carries_data_frame(
            'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'))

    def test_keepalive_then_data_in_same_chunk(self) -> None:
        self.assertTrue(G._carries_data_frame(': keepalive\n\ndata: {"choices":[]}\n\n'))

    def test_first_frame_lands_on_data_not_keepalive(self) -> None:
        """按轮询顺序模拟：保活帧先到，首帧应落在第一个 data: chunk 上。"""
        chunks = [': keepalive\n\n', ': keepalive\n\n',
                  'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                  'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n']
        stamps = []
        for i, piece in enumerate(chunks):
            if G._carries_data_frame(piece):
                stamps.append(i)
        self.assertEqual(stamps[0], 2)   # 不是 0，也不是 1


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


class FirstFrameColumnTest(_DbCase):
    def test_column_exists(self) -> None:
        cols = {r['name'] for r in db.query('PRAGMA table_info(request_logs)')}
        self.assertIn('first_frame_ms', cols)

    def test_roundtrip_keeps_both(self) -> None:
        db.add_request_log(
            ts=1, key_id=None, ip='1.2.3.4', model='deepseek-v4.1-flash',
            mapped_model='', status=200, prompt_tokens=10, completion_tokens=5,
            latency_ms=9000, first_token_ms=7000, first_frame_ms=1480,
            ua='UA', error=None, stream=1, credit=None, realm='cn',
            account='a', cache_hit_tokens=0, cache_miss_tokens=0,
            cache_write_tokens=0, effort='max', channel='dsh',
            account_expected=1,
        )
        row = self._row()
        self.assertEqual(row['first_frame_ms'], 1480)
        self.assertEqual(row['first_token_ms'], 7000)

    def test_null_when_not_collected(self) -> None:
        db.add_request_log(
            ts=2, key_id=None, ip='1.2.3.4', model='deepseek-v4.1-flash',
            mapped_model='', status=200, prompt_tokens=10, completion_tokens=5,
            latency_ms=100, first_token_ms=None,
            ua='UA', error=None, stream=0, credit=None, realm='cn',
            account=None, cache_hit_tokens=0, cache_miss_tokens=0,
            cache_write_tokens=0, effort=None, channel='api',
            account_expected=0,
        )
        row = self._row()
        self.assertIsNone(row['first_frame_ms'])


class RecordWiringTest(unittest.TestCase):
    def test_record_passes_first_frame(self) -> None:
        captured: dict = {}

        def fake_add(**kw):
            captured.update(kw)

        with mock.patch.object(db, 'add_request_log', side_effect=fake_add), \
                mock.patch.object(db, 'realm_of_model', return_value='cn'):
            G._record(None, '1.2.3.4', 'deepseek-v4.1-flash', '', 200, 10, 5,
                      9000, 'UA', None, True,
                      first_token=7000, first_frame=1480, effort='max')
        self.assertEqual(captured.get('first_frame_ms'), 1480)
        self.assertEqual(captured.get('first_token_ms'), 7000)


class LogsReadPathTest(unittest.TestCase):
    def test_logs_item_exposes_field(self) -> None:
        src = (Path(__file__).resolve().parents[1] / 'routers' / 'logs.py').read_text(encoding='utf-8')
        self.assertIn("'first_frame_ms': r['first_frame_ms']", src)


if __name__ == '__main__':
    unittest.main()

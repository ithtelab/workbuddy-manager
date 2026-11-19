"""「已登记积分」的只读查询（issue #140）。

需求方的用法是：拿只读管理令牌定时拉一次池子的积分登记值，**不能**因此触发上游
查询 —— 他们那边的池面板 5 秒一刷，若每次都问腾讯，等于让看板变成压测工具。

这里钉三件事：
  1. 快照现在带**登记时刻**（新形态 `{credits, at}`），升级前存下的纯数字形态仍能读，
     积分流水（余额增加留痕）的比对不会因为这次改动丢基线；
  2. 接口只读：**一次上游请求都不发**（用「调用即失败」的桩钉住）；
  3. 返回值如实标注：给了 `snapshot_at` 与每行的 `registered_at`，调用方得以显示
     「这是什么时候登记的」。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, security  # noqa: E402
from server.services import credits as creditsvc  # noqa: E402
from server.services import tencent  # noqa: E402


class CreditsSnapshotShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / 'm.db'
        db._conn = None
        db.connect()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        self._tmp.cleanup()

    def test_record_balance_stores_time_and_value(self) -> None:
        creditsvc._cache.clear()
        creditsvc.record_balance('u1', '一号', 100)
        entry = creditsvc._load_snapshot()['u1']
        self.assertEqual(entry['credits'], 100)
        self.assertIsInstance(entry['at'], int, '登记时刻缺失：只读查询就没有时间可标')
        rows = creditsvc.snapshot_entries()
        self.assertEqual(rows['accounts'], [{'uid': 'u1', 'credits': 100,
                                             'registered_at': entry['at']}])
        # 服务层不认识账号文件：known 标记由**接口层**按本机账号补（见下面端点用例）
        self.assertEqual(rows['snapshot_at'], entry['at'], '顶层 snapshot_at 应是最新登记时刻')

    def test_legacy_numeric_snapshot_still_compared(self) -> None:
        """升级前存的是纯数字：仍要能读出上一次的值，否则积分流水会丢一次比对。"""
        db.set_setting('credits_snapshot', json.dumps({'u2': 50}))
        delta = creditsvc.record_balance('u2', '二号', 80)
        self.assertEqual(delta, 30, '旧形态没读出来 → 余额增加没有被记成流水')

    def test_entries_skip_broken_rows(self) -> None:
        db.set_setting('credits_snapshot', json.dumps({'u3': 'not-a-number', 'u4': 10}))
        rows = creditsvc.snapshot_entries()
        self.assertEqual([r['uid'] for r in rows['accounts']], ['u4'])


    def test_forget_removes_registration(self) -> None:
        """删号路径调用 forget：登记与缓存一起清掉（issue #404）。"""
        creditsvc._cache.clear()
        creditsvc.record_balance('bye', '再见', 42)
        creditsvc._cache['bye'] = (0, True, 42, '', [])
        creditsvc.forget('bye')
        self.assertNotIn('bye', creditsvc._load_snapshot())
        self.assertNotIn('bye', creditsvc._cache)


class CreditsSnapshotEndpointTest(unittest.TestCase):
    """端到端：读这个接口不能碰上游。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / 'm.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        config.AUTH_DIR = Path(self._tmp.name) / 'auths'
        db._conn = None
        db.connect()
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('p')}], 'api_keys': []})
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        self._tmp.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        from server.main import app
        c = TestClient(app)
        assert c.post('/api/login', json={'username': 'admin', 'password': 'p'}).status_code == 200
        return c

    def _write_account(self, uid: str) -> None:
        config.AUTH_DIR.mkdir(parents=True, exist_ok=True)
        (config.AUTH_DIR / f'workbuddy-{uid}.json').write_text(
            json.dumps({'account': {'uid': uid}, 'auth': {'accessToken': 'T',
                                                          'expiresAt': int(time.time()) + 3600}}),
            encoding='utf-8')

    def test_deleted_account_is_dropped_from_snapshot(self) -> None:
        """已删账号不再出现在快照里（issue #404）：快照不是审计日志，
        留着历史登记会让按 uid/按域汇总的余额虚高。"""
        config.AUTH_DIR = Path(self._tmp.name) / 'auths'
        creditsvc.record_balance('still-here', '还在', 100)
        creditsvc.record_balance('deleted', '已删', 200)
        self._write_account('still-here')

        body = self._client().get('/api/accounts/credits-snapshot').json()
        self.assertEqual([row['uid'] for row in body['accounts']], ['still-here'])
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['total_registered'], 2, '总量仍应能看出库里有两条登记')

    def test_disabled_account_hidden_by_default(self) -> None:
        """停用账号不再被调度，默认不该计入「还能花多少」；
        `include_inactive=true` 仍能查到它，且标成 active=false。"""
        config.AUTH_DIR = Path(self._tmp.name) / 'auths'
        creditsvc.record_balance('off-line', '被禁用', 300)
        self._write_account('off-line')
        (config.AUTH_DIR / 'workbuddy-off-line.json').rename(
            config.AUTH_DIR / 'workbuddy-off-line.json.disabled')

        body = self._client().get('/api/accounts/credits-snapshot').json()
        self.assertEqual(body['accounts'], [])
        self.assertEqual(body['active_count'], 0)

        all_rows = self._client().get(
            '/api/accounts/credits-snapshot?include_inactive=true').json()
        self.assertEqual(len(all_rows['accounts']), 1)
        self.assertTrue(all_rows['accounts'][0]['known'])
        self.assertFalse(all_rows['accounts'][0]['active'])

    def test_endpoint_reads_without_asking_upstream(self) -> None:
        creditsvc.record_balance('u9', '九号', 777)
        self._write_account('u9')

        async def boom(*a, **k):  # pragma: no cover - 调用即失败
            raise AssertionError('只读查询不该触发上游')

        with mock.patch.object(tencent, 'fetch_credits', boom):
            r = self._client().get('/api/accounts/credits-snapshot')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        row = next(x for x in body['accounts'] if x['uid'] == 'u9')
        self.assertEqual(row['credits'], 777)
        self.assertIsInstance(body['snapshot_at'], int)
        self.assertIsInstance(row['registered_at'], int)
        self.assertTrue(row['known'])
        self.assertTrue(row['active'])

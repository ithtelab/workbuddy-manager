"""账号列表的短 TTL 缓存（issue #444）：命中、失效与「加号立刻可见」。

钉住的是**缓存不能改变可见行为**：

  · 同一轮首屏里重复请求 `/api/accounts` 只扫描一次（这是 196 个账号时省时间的来源）；
  · 增删改、备注、禁用、重登等写操作必须让缓存立刻作废——否则用户加完账号
    回到列表看到「没有新号」，会以为导入失败；
  · 账号目录本身变化（直接往目录里写文件）也要自动失效，作为漏写失效调用的兜底；
  · TTL 到期后必须重新扫描，不能让运行时状态（冷却 / 积分）长时间停在旧值。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config, db, security
from server.routers import accounts
from server.services import accountcache, wb2api

UID_A = 'aaaa1111-0000-0000-0000-00000000000a'
UID_B = 'bbbb2222-0000-0000-0000-00000000000b'


def _payload(uid: str, nickname: str = '测试号') -> dict:
    return {
        'account': {'uid': uid, 'nickname': nickname, 'enterpriseId': ''},
        'auth': {
            'accessToken': 'AT',
            'refreshToken': 'RT',
            'expiresAt': int(time.time()) + 86400,
            'domain': 'www.codebuddy.cn',
            'realm': 'cn',
        },
    }


class _CacheCase(unittest.TestCase):
    def setUp(self) -> None:
        # 缓存是模块级的：每个用例必须从干净状态开始，否则上一条的响应会被
        # 当成命中（临时目录不同本来也会换键，但显式清空更不容易误判）。
        accountcache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.auth_dir = root / 'auths'
        self.auth_dir.mkdir()

        self._orig = (config.AUTH_DIR, config.DB_PATH, config.USERS_FILE)
        config.AUTH_DIR = self.auth_dir
        config.DB_PATH = root / 'm.db'
        config.USERS_FILE = root / 'users.json'

        def restore() -> None:
            if db._conn is not None:
                db._conn.close()
            db._conn = None
            config.AUTH_DIR, config.DB_PATH, config.USERS_FILE = self._orig

        self.addCleanup(restore)
        db._conn = None
        db.connect()

        self.group = {
            'id': None, 'name': '默认上游', 'base_url': 'http://127.0.0.1:1',
            'api_key': '', 'auth_dir': str(self.auth_dir), 'is_default': True,
            'enabled': True,
        }
        for patch in (
            mock.patch.object(accounts.upstreamsvc, 'default_upstream', return_value=self.group),
            mock.patch.object(accounts.reload, 'request_restart', return_value=False),
            mock.patch.object(accounts.reload, 'request_reload_or_restart', return_value=False),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        self.status_calls = 0

        async def fake_status(**_kwargs) -> dict:
            self.status_calls += 1
            return {'connected': True, 'accounts': []}

        for patch in (
            mock.patch.object(wb2api, 'get_status', new=fake_status),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        app = FastAPI()
        app.include_router(accounts.router)
        stub = lambda: {'username': 'admin', 'role': 'admin'}  # noqa: E731
        app.dependency_overrides[security.current_user] = stub
        app.dependency_overrides[security.require_admin] = stub
        app.dependency_overrides[security.require_session_admin] = stub
        self.client = TestClient(app)

    def _write(self, name: str, uid: str, nickname: str = '测试号') -> Path:
        path = self.auth_dir / name
        path.write_text(json.dumps(_payload(uid, nickname), ensure_ascii=False),
                        encoding='utf-8')
        return path

    def _list(self) -> dict:
        r = self.client.get('/api/accounts')
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class AccountCacheHitTest(_CacheCase):
    def test_second_request_is_served_from_cache(self) -> None:
        self._write('workbuddy-a.json', UID_A)
        first = self._list()
        second = self._list()
        self.assertEqual(self.status_calls, 1,
                         '同一秒内两次列表请求只应问一次上游 /status')
        self.assertEqual(first, second, '命中缓存返回的必须与重新计算的一致')

    def test_ttl_expiry_rescans(self) -> None:
        self._write('workbuddy-a.json', UID_A)
        self._list()
        with mock.patch.object(accountcache, 'TTL_SECONDS', 0.02):
            time.sleep(0.05)
            self._list()
        self.assertEqual(self.status_calls, 2, 'TTL 到期后必须重新扫描')

    def test_direct_directory_change_busts_cache(self) -> None:
        """兜底：往目录里直接写文件（不走接口）也要换键，不能返回旧列表。"""
        self._write('workbuddy-a.json', UID_A)
        self.assertEqual(self._list()['total'], 1)
        time.sleep(0.01)  # 确保目录 mtime 与上一次不同（秒级文件系统也不误判）
        self._write('workbuddy-b.json', UID_B)
        self.assertEqual(self._list()['total'], 2)


class AccountCacheInvalidationTest(_CacheCase):
    def test_upload_makes_new_account_visible_immediately(self) -> None:
        self._write('workbuddy-a.json', UID_A)
        self.assertEqual(self._list()['total'], 1)

        uploaded = json.loads(json.dumps(_payload(UID_B)))
        r = self.client.post('/api/accounts/upload', files=[
            ('files', ('workbuddy-b.json', json.dumps(uploaded), 'application/json')),
        ])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()['uploaded']), 1, r.text)

        # 上传后立刻列表：必须是 2 个。若缓存没失效，这里会是 1 —— 用户加完号
        # 看不到新号，会以为导入失败并再传一次。
        self.assertEqual(self._list()['total'], 2)

    def test_note_change_is_visible_immediately(self) -> None:
        self._write('workbuddy-a.json', UID_A)
        self.assertEqual(self._list()['accounts'][0]['note'], '')

        r = self.client.put('/api/accounts/workbuddy-a.json/note',
                            json={'note': '备用号'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._list()['accounts'][0]['note'], '备用号')

    def test_disable_is_visible_immediately(self) -> None:
        self._write('workbuddy-a.json', UID_A)
        self.assertEqual(self._list()['accounts'][0]['disabled_by_panel'], False)

        # 上游没开管理接口 → 回退改名（`.disabled` 后缀），列表要立刻反映。
        with mock.patch.object(
                wb2api, 'set_manual_disabled',
                new=mock.AsyncMock(return_value=(False, 'no route', 'no_route'))):
            r = self.client.post('/api/accounts/workbuddy-a.json/disabled',
                                 json={'disabled': True, 'reload': False})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._list()['accounts'][0]
        self.assertTrue(row['disabled_by_panel'])
        self.assertTrue(row['file'].endswith('.disabled'))


class AccountCacheUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        accountcache.clear()
        self.addCleanup(accountcache.clear)

    def test_invalidate_only_clears_one_group(self) -> None:
        accountcache.put((1, '/a', 1), {'total': 1})
        accountcache.put((2, '/b', 1), {'total': 2})
        accountcache.invalidate(1)
        self.assertIsNone(accountcache.get((1, '/a', 1)))
        self.assertIsNotNone(accountcache.get((2, '/b', 1)))

    def test_put_keeps_one_entry_per_group(self) -> None:
        accountcache.put((1, '/a', 1), {'total': 1})
        accountcache.put((1, '/a', 2), {'total': 2})
        self.assertEqual(accountcache.stats()['entries'], 1)
        self.assertIsNone(accountcache.get((1, '/a', 1)))
        self.assertEqual(accountcache.get((1, '/a', 2)), {'total': 2})

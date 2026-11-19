"""并发与原子性：任务单飞、更新锁、账号文件锁、上游配置原子写（2026-10-18 审计）。

这几处都是「检查与动作之间隔着一次 await / 一次 IO」的经典形态，症状也一致：
不报错、只是偶尔出现两个任务、两个更新进程、或某个字段被静默覆盖。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.services import taskrun, tencent, updater, wb2api  # noqa: E402


class TaskRunSingleFlightTest(unittest.TestCase):
    """并发点两次「执行」只能起一个脚本。"""

    def setUp(self) -> None:
        taskrun._state.update({'running': False, 'mode': '', 'target': '', 'group': '', 'auth_dir': '',
                               'started_at': 0, 'finished_at': 0, 'exit_code': None,
                               'timed_out': False, 'error': '', 'lines': []})
        taskrun._task = None

    def tearDown(self) -> None:
        if taskrun._task is not None:
            taskrun._task.cancel()
        taskrun._task = None
        taskrun._state['running'] = False

    def test_two_concurrent_starts_launch_once(self) -> None:
        started: list[str] = []

        async def fake_run(argv, mode, target, auth_dir='', group=''):  # noqa: ANN001, ANN202
            started.append(mode)
            try:
                await asyncio.sleep(0.2)
            finally:
                taskrun._state['running'] = False

        async def go():  # noqa: ANN202
            with mock.patch.object(taskrun, '_prepare', return_value=(True, {'argv': ['x'], 'auth_dir': '', 'group': ''})), \
                 mock.patch.object(taskrun, '_run', fake_run):
                return await asyncio.gather(taskrun.start_async('preview', 'ALL'),
                                            taskrun.start_async('preview', 'ALL'))

        (ok1, _), (ok2, _) = asyncio.run(go())
        self.assertNotEqual(ok1, ok2, '两次并发启动都成功了 —— 会起两个脚本')
        self.assertEqual(len(started), 1)

    def test_stop_waits_for_cleanup(self) -> None:
        cleaned: list[bool] = []

        async def fake_run(argv, mode, target, auth_dir='', group=''):  # noqa: ANN001, ANN202
            try:
                await asyncio.sleep(30)
            finally:
                cleaned.append(True)
                taskrun._state['running'] = False

        async def go():  # noqa: ANN202
            with mock.patch.object(taskrun, '_prepare', return_value=(True, {'argv': ['x'], 'auth_dir': '', 'group': ''})), \
                 mock.patch.object(taskrun, '_run', fake_run):
                ok, _ = await taskrun.start_async('preview', 'ALL')
                self.assertTrue(ok)
                # 让任务真正进入协程体（create_task 只是排队；在首次执行前取消，
                # 协程不会运行，finally 自然也不会执行 —— 这里要测的是「跑起来之后
                # 再停」的收尾）。
                await asyncio.sleep(0.01)
                stopped = await taskrun.stop()
                return stopped

        self.assertTrue(asyncio.run(go()))
        self.assertEqual(cleaned, [True], 'stop 没有等 _run 收尾（孤儿进程风险）')
        self.assertFalse(taskrun._state['running'])


class UpdateLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self._p = [
            mock.patch.object(updater.config, 'DATA_DIR', self.data),
            mock.patch.object(updater, 'LOCK_FILE', self.data / 'update.lock'),
        ]
        for patch in self._p:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self._p):
            patch.stop()
        self._tmp.cleanup()

    def test_second_claim_fails_until_released(self) -> None:
        self.assertTrue(updater._claim_lock())
        self.assertFalse(updater._claim_lock(), '同一次更新被抢占了两次')
        updater._release_lock()
        self.assertTrue(updater._claim_lock())

    def test_dead_pid_lock_is_reclaimed(self) -> None:
        updater.LOCK_FILE.write_text('999999', encoding='utf-8')
        old = time.time() - updater.LOCK_TTL - 10
        import os
        os.utime(updater.LOCK_FILE, (old, old))
        self.assertTrue(updater._claim_lock(), '过期锁没有被回收')


class AccountFileLockTest(unittest.TestCase):
    """代理与权重并发写不能互相覆盖（同一份 JSON 的两个字段）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.auth_dir = Path(self._tmp.name)
        self.file = self.auth_dir / 'workbuddy-u1.json'
        self.file.write_text(json.dumps({
            'account': {'uid': 'u1', 'nickname': 'n'},
            'auth': {'accessToken': 't', 'refreshToken': 'r', 'expiresAt': 1},
        }), encoding='utf-8')
        self._orig = tencent._atomic_write_json

    def tearDown(self) -> None:
        tencent._atomic_write_json = self._orig
        self._tmp.cleanup()

    def test_concurrent_proxy_and_weight_keep_both(self) -> None:
        real = self._orig

        def slow_write(path, payload):  # noqa: ANN001, ANN202
            time.sleep(0.02)          # 放大「读完之后、写之前」的窗口
            real(path, payload)

        tencent._atomic_write_json = slow_write
        errors: list[BaseException] = []

        def set_proxy() -> None:
            try:
                wb2api.set_account_proxy(self.file.name, 'route-a', self.auth_dir)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def set_weight() -> None:
            try:
                wb2api.set_account_weight(self.file.name, 2, self.auth_dir)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        for _ in range(10):
            t1 = threading.Thread(target=set_proxy)
            t2 = threading.Thread(target=set_weight)
            t1.start(); t2.start(); t1.join(); t2.join()
            data = json.loads(self.file.read_text(encoding='utf-8'))
            self.assertEqual(data.get('proxy'), 'route-a', '代理被并发写覆盖')
            self.assertEqual(data.get('weight'), 2, '权重被并发写覆盖')

        self.assertEqual(errors, [])


class WriteAuthFileKeepsMetadataTest(unittest.TestCase):
    """重新登录/导入按响应重建文件时，proxy / weight / device_token 必须保留。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.auth_dir = Path(self._tmp.name)
        self.uid = 'u1'
        self.file = self.auth_dir / f'workbuddy-{self.uid}.json'
        self.file.write_text(json.dumps({
            'account': {'uid': self.uid, 'nickname': 'old'},
            'auth': {'accessToken': 'old', 'refreshToken': 'r', 'expiresAt': 1},
            'device_token': 'dev', 'proxy': 'route-a', 'weight': 3,
        }), encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_metadata_survives_rebuild(self) -> None:
        tencent.write_auth_file({
            'uid': self.uid, 'nickname': 'new', 'access_token': 'new-token',
            'expires_at': 2, 'domain': 'd', 'realm': 'cn',
        }, self.auth_dir)
        data = json.loads(self.file.read_text(encoding='utf-8'))
        self.assertEqual(data['auth']['accessToken'], 'new-token')
        self.assertEqual(data.get('device_token'), 'dev')
        self.assertEqual(data.get('proxy'), 'route-a')
        self.assertEqual(data.get('weight'), 3, '重登把用户设的权重清掉了')


class UpstreamConfigAtomicTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / 'config.json'
        self.original = {'upstream': {'api_key': 'keep-me'}, 'pool': {'max_in_flight': 3}}
        self.path.write_text(json.dumps(self.original), encoding='utf-8')
        self._p = [
            mock.patch.object(config, 'UPSTREAM_CONFIG', self.path),
            mock.patch.object(wb2api.config, 'UPSTREAM_CONFIG', self.path),
        ]
        for patch in self._p:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self._p):
            patch.stop()
        self._tmp.cleanup()

    def test_failed_replace_leaves_original_intact(self) -> None:
        """写盘中途失败：原文件必须一字不改（旧实现是先截断再写）。"""
        with mock.patch.object(tencent.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                wb2api.save_upstream_config({'pool': {'max_in_flight': 9}})
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(data, self.original, '失败的保存把原配置破坏了')


if __name__ == '__main__':
    unittest.main()

"""Scheduled entrypoints use account gaps; manual requests keep their own context."""
import asyncio
from contextlib import ExitStack, nullcontext
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from server import config, db
from server.routers import accounts
from server.services import dailyplan, renew, taskpacing, taskrun


class ScheduledAccountSpacingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.cfg = self.root / 'config.json'
        self.configure(30, 120)
        self.patch(config, 'UPSTREAM_CONFIG', self.cfg)
        self.events = []
        self.patch(db, 'add_checkin_log')
        self.patch(db, 'add_task_logs')
        self.patch(db, 'checkin_done_since', return_value={'done': 1})
        self.patch(accounts, '_group', side_effect=lambda group: {'dir': self.root / str(group)})
        self.patch(accounts, '_require_dir', side_effect=lambda group: group['dir'])
        self.patch(accounts.wb2api, 'read_account_file', side_effect=self.raw)

    def patch(self, obj, name, *args, **kwargs):
        return self.stack.enter_context(mock.patch.object(obj, name, *args, **kwargs))

    def configure(self, lo, hi):
        self.cfg.write_text(json.dumps({'schedule': {'account_delay_min_seconds': lo,
                                                    'account_delay_max_seconds': hi}}))

    def raw(self, filename, *args):
        return {'auth': {'accessToken': 'test', 'refreshToken': 'test',
                         'realm': 'global' if filename == 'global' else 'cn'},
                'account': {'uid': filename}}

    def fixture(self, uid):
        return {'uid': uid, 'file': uid, 'nickname': uid,
                'checkin_supported': uid != 'global'}

    async def sleep(self, delay):
        self.events.append(('wait', delay))

    async def test_daily_scheduler_spaces_real_checkin_path_across_groups(self):
        self.patch(dailyplan, '_task_groups', return_value=[{'id': 1, 'name': 'one'}, {'id': 2, 'name': 'two'}])
        def listing(path):
            names = ['one', 'done', 'global'] if path.name == '1' else ['two', 'three']
            return [self.fixture(n) for n in names]
        self.patch(accounts.wb2api, 'list_auth_accounts', side_effect=listing)
        async def checkin(payload, realm):
            self.events.append(('request', payload['uid']))
            return 0, 'ok'
        self.patch(accounts, '_checkin_with_retry', side_effect=checkin)
        self.patch(dailyplan, 'get_plan', return_value={'checkin': {'hour': 9}})
        self.patch(dailyplan, 'is_due', side_effect=lambda task, plan: task == 'checkin')
        mark = self.patch(dailyplan, 'mark_ran')
        async def sleep(delay):
            if delay == 20: return
            if delay == dailyplan._TICK_SECONDS: raise asyncio.CancelledError
            await self.sleep(delay)
        self.patch(taskpacing.asyncio, 'sleep', side_effect=sleep)
        self.patch(taskpacing.random, 'randint', side_effect=[31, 89])
        with self.assertRaises(asyncio.CancelledError): await dailyplan._loop()
        self.assertEqual(self.events, [('request', 'one'), ('wait', 31),
                                      ('request', 'two'), ('wait', 89), ('request', 'three')])
        mark.assert_called_once_with('checkin')
        self.assertFalse(taskpacing.is_scheduled())

    async def test_compensation_spaces_status_queries_even_when_already_claimed(self):
        self.patch(accounts.wb2api, 'list_auth_accounts', return_value=[self.fixture(n) for n in ('one', 'two')])
        async def status(auth, realm):
            self.events.append(('status', auth['uid']))
            return True, {'already': auth['uid'] == 'one'}, ''
        self.patch(accounts.tencent, 'compensation_status', side_effect=status)
        self.patch(accounts.tencent, 'compensation_already_claimed', side_effect=lambda data: data['already'])
        claim = self.patch(accounts.tencent, 'claim_compensation', new=mock.AsyncMock(return_value=('claimed', 10, 'ok')))
        self.patch(taskpacing.asyncio, 'sleep', side_effect=self.sleep)
        self.patch(taskpacing.random, 'randint', return_value=42)
        with taskpacing.scheduled_execution():
            out = await accounts.claim_compensation_all(upstream_id=1, user={})
        self.assertEqual(self.events, [('status', 'one'), ('wait', 42), ('status', 'two')])
        self.assertEqual((out['already'], out['claimed']), (1, 1))
        claim.assert_awaited_once()

    async def test_zero_or_manual_batches_keep_concurrency(self):
        for scheduled, bounds in ((False, (30, 120)), (True, (0, 0))):
            with self.subTest(scheduled=scheduled):
                self.configure(*bounds)
                entered, ready = [], asyncio.Event()
                async def action(account):
                    entered.append(account)
                    if len(entered) == 2: ready.set()
                    await ready.wait()
                    return account
                with taskpacing.scheduled_execution() if scheduled else nullcontext():
                    out = await asyncio.wait_for(taskpacing.run_accounts('checkin', [1, 2], action), .5)
                self.assertEqual(out, [1, 2])

    async def test_cancellation_during_gap_prevents_next_account(self):
        waiting = asyncio.Event()
        called = []
        async def sleep(delay):
            waiting.set()
            await asyncio.Event().wait()
        async def action(account): called.append(account)
        self.patch(taskpacing.asyncio, 'sleep', side_effect=sleep)
        with taskpacing.scheduled_execution():
            task = asyncio.create_task(taskpacing.run_accounts('checkin', [1, 2], action))
            await asyncio.wait_for(waiting.wait(), .5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(called, [1])
        self.assertFalse(taskpacing.is_scheduled())

    async def test_manual_request_does_not_inherit_another_tasks_schedule(self):
        ready, release = asyncio.Event(), asyncio.Event()
        async def scheduled():
            with taskpacing.scheduled_execution():
                ready.set()
                await release.wait()
                return taskpacing.script_args()
        task = asyncio.create_task(scheduled())
        await ready.wait()
        self.assertEqual(taskpacing.script_args(), [])
        release.set()
        self.assertEqual((await task)[-1], '120')

    def test_config_defaults_and_boundaries(self):
        for lo, hi, expected in ((0, 0, (0, 0)), (0, 600, (0, 600)), (600, 600, (600, 600)),
                                 (True, 120, (0, 0)), (30, 2, (0, 0)), (-1, 120, (0, 0)),
                                 (1, 601, (0, 0)), (1.5, 120, (0, 0))):
            with self.subTest(lo=lo, hi=hi):
                self.configure(lo, hi)
                self.assertEqual(taskpacing.delay_bounds(), expected)
        for content in ('{}', '[]', '{"schedule":null}', 'broken'):
            self.cfg.write_text(content)
            self.assertEqual(taskpacing.delay_bounds(), (0, 0))
        self.cfg.unlink()
        self.assertEqual(taskpacing.delay_bounds(), (0, 0))

    def test_batch_keeps_config_snapshot(self):
        with taskpacing.scheduled_execution():
            self.configure(5, 10)
            self.assertEqual(taskpacing.delay_bounds(), (30, 120))
        self.assertEqual(taskpacing.delay_bounds(), (5, 10))

    async def test_zero_draw_keeps_sequence_without_sleep(self):
        self.configure(0, 120)
        sleep = self.patch(taskpacing.asyncio, 'sleep', new=mock.AsyncMock())
        draw = self.patch(taskpacing.random, 'randint', return_value=0)
        action = mock.AsyncMock(side_effect=lambda account: account)
        with taskpacing.scheduled_execution():
            self.assertEqual(await taskpacing.run_accounts('checkin', [1, 2], action), [1, 2])
        draw.assert_called_once_with(0, 120)
        sleep.assert_not_awaited()

    async def test_scheduled_script_arguments_survive_preparation_thread(self):
        self.patch(taskrun, 'available', return_value=(True, ''))
        self.patch(taskrun, '_script_path', return_value=self.root / 'task_runner.py')
        self.stack.enter_context(mock.patch.dict(taskrun._state, {'running': False}))
        launch = self.patch(taskrun, '_launch', return_value=(True, 'started'))
        for mode in ('claim', 'full'):
            with taskpacing.scheduled_execution(): await taskrun.start_async(mode)
            argv = launch.call_args.args[0]['argv']
            self.assertEqual(argv[-4:], ['--account-delay-min', '30', '--account-delay-max', '120'])
        with taskpacing.scheduled_execution():
            self.assertNotIn('--account-delay-min', taskrun.build_command('preview', 'ALL'))
        self.assertNotIn('--account-delay-min', taskrun.build_command('claim', 'ALL'))
        self.configure(0, 0)
        with taskpacing.scheduled_execution():
            self.assertNotIn('--account-delay-min', taskrun.build_command('claim', 'ALL'))

    async def test_script_group_boundary_also_waits(self):
        self.patch(dailyplan, '_task_groups', return_value=[{'name': 'one'}, {'name': 'two'}])
        async def start(*args, **kwargs):
            self.events.append(('start', kwargs['group_name']))
            return True, ''
        self.patch(taskrun, 'start_async', side_effect=start)
        self.patch(taskrun, 'wait_idle', new=mock.AsyncMock(return_value=True))
        self.patch(taskpacing.asyncio, 'sleep', side_effect=self.sleep)
        self.patch(taskpacing.random, 'randint', return_value=42)
        with taskpacing.scheduled_execution(): await dailyplan.run_task('claim')
        self.assertEqual(self.events, [('start', 'one'), ('wait', 42), ('start', 'two')])

    async def test_legacy_claim_scheduler_passes_account_spacing(self):
        self.patch(taskrun, 'get_schedule', return_value={'enabled': True, 'hours': [9]})
        self.patch(taskrun, '_last_claim_day', '')
        self.patch(taskrun.time, 'localtime', return_value=time.struct_time((2026, 11, 1, 9, 0, 0, 6, 305, -1)))
        self.patch(taskrun, '_script_path', return_value=self.root / 'task_runner.py')
        self.stack.enter_context(mock.patch.dict(taskrun._state, {'running': False}))
        captured = []
        async def start(*args): captured.append(taskrun.build_command('claim', 'ALL'))
        self.patch(taskrun, 'start_async', side_effect=start)
        self.patch(taskrun.asyncio, 'sleep', side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError): await taskrun._claim_loop()
        self.assertEqual(captured[0][-4:], ['--account-delay-min', '30', '--account-delay-max', '120'])

    async def test_token_renewal_loop_spaces_only_due_accounts(self):
        fixtures = [(None, {'file': name, 'expires_at': expires, 'disabled_by_panel': disabled})
                    for name, expires, disabled in [('one', 1, False), ('skip', 1, True),
                                                     ('later', 9999999999, False), ('two', 1, False)]]
        self.patch(renew, '_iter_accounts', return_value=fixtures)
        self.patch(renew.wb2api, 'read_account_file_any', side_effect=self.raw)
        async def refresh(payload):
            self.events.append(('refresh', payload['uid']))
            return True, '', {}
        self.patch(renew.tencent, 'refresh_token', side_effect=refresh)
        self.patch(renew.tencent, 'update_auth_tokens')
        self.patch(renew, '_record')
        self.patch(renew, 'get_plan', return_value={})
        self.patch(renew, 'plan_is_due', return_value=False)
        async def sleep(delay):
            if delay == 30: return
            if delay == renew._CHECK_INTERVAL_SECONDS: raise asyncio.CancelledError
            await self.sleep(delay)
        self.patch(taskpacing.asyncio, 'sleep', side_effect=sleep)
        self.patch(taskpacing.random, 'randint', return_value=42)
        with self.assertRaises(asyncio.CancelledError): await renew._loop()
        self.assertEqual(self.events, [('refresh', 'one'), ('wait', 42), ('refresh', 'two')])

    async def test_long_account_gap_does_not_trigger_script_idle_timeout(self):
        self.configure(600, 600)
        self.patch(taskrun, '_script_path', return_value=self.root / 'scripts/task_runner.py')
        self.patch(taskrun, '_record_history')
        self.stack.enter_context(mock.patch.dict(taskrun._state, {'running': True, 'lines': [],
                                     'exit_code': None, 'timed_out': False, 'error': ''}))
        proc = mock.Mock(returncode=0)
        proc.stdout.readline = mock.AsyncMock(return_value=b'')
        proc.wait = mock.AsyncMock(return_value=0)
        self.patch(taskrun.asyncio, 'create_subprocess_exec', new=mock.AsyncMock(return_value=proc))
        reads = 0
        async def read(awaitable, timeout):
            nonlocal reads
            await awaitable
            reads += 1
            if reads == 1: raise asyncio.TimeoutError
            return b''
        self.patch(taskrun.asyncio, 'wait_for', side_effect=read)
        # `taskrun.time` 是**全局** time 模块：这里打的桩会影响同一进程里所有
        # 调用 time.time() 的代码（db 清理、账号风控记账等）。用可迭代 + 兜底
        # 的最后值，避免其他模块多调一次就耗尽 side_effect（StopIteration）。
        clock = iter([0, 0, 400, 400, 401])
        self.patch(taskrun.time, 'time', side_effect=lambda: next(clock, 401))
        with taskpacing.scheduled_execution(): await taskrun._run(['python', 'script'], 'claim', 'ALL')
        self.assertFalse(taskrun._state['timed_out'])
        proc.kill.assert_not_called()
        self.assertEqual(taskrun._state['exit_code'], 0)

    async def test_forced_token_plan_also_uses_scheduled_context(self):
        self.patch(renew, 'renew_once', new=mock.AsyncMock(return_value={'renewed': 0, 'failed': 0}))
        self.patch(renew, 'get_plan', return_value={'interval_days': 3, 'hour': 9})
        self.patch(renew, 'plan_is_due', return_value=True)
        self.patch(renew, 'mark_plan_ran')
        self.patch(renew, '_record_plan')
        self.patch(renew, '_iter_accounts', return_value=[(None, self.fixture(n)) for n in ('one', 'two')])
        self.patch(renew.wb2api, 'read_account_file_any', side_effect=self.raw)
        async def refresh(payload):
            self.events.append(('refresh', payload['uid']))
            return True, '', {}
        self.patch(renew.tencent, 'refresh_token', side_effect=refresh)
        self.patch(renew.tencent, 'update_auth_tokens')
        self.patch(renew, '_record')
        async def sleep(delay):
            if delay == 30: return
            if delay == renew._CHECK_INTERVAL_SECONDS: raise asyncio.CancelledError
            await self.sleep(delay)
        self.patch(taskpacing.asyncio, 'sleep', side_effect=sleep)
        self.patch(taskpacing.random, 'randint', return_value=42)
        with self.assertRaises(asyncio.CancelledError): await renew._loop()
        self.assertEqual(self.events, [('refresh', 'one'), ('wait', 42), ('refresh', 'two')])

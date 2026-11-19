"""成长任务一键执行（issue #19）的回归测试。

这个功能会对腾讯发起**真实写请求**，所以测试的重点不是「跑得通」，而是
「不该发生的事不会发生」：

  1. 命令注入：账号标识直接进 argv，必须做字符白名单（`a;b`、`../x`、空串都得拒）。
  2. `full`（点亮）必须显式确认：它是唯一会伪造活跃上报的模式，手滑点到的代价
     是账号风控。接口层不加确认就等同把风险最高的操作变成一键。
  3. 定时只跑 `claim`：领奖是幂等的、不伪造行为；点亮绝不能进定时。
  4. 并发保护：一次只跑一个（叠着跑会放大风控信号，输出也会交错）。
  5. 上游脚本不存在时**前置拒绝**并说清怎么办，而不是跑到一半失败。
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security  # noqa: E402
from server.services import taskrun  # noqa: E402


class SummarizeTest(unittest.TestCase):
    """结果汇总要取**结果行**，不是参数行。

    实测踩到过：脚本开头打 `mode=DRY-RUN accounts=[...]`（运行参数），结尾才是
    `task_runner done: ok=3 credit=+100`（结果）。按 `mode=` 匹配会把参数行当结果
    记进历史，等于没记结果。
    """

    def setUp(self) -> None:
        self._saved = dict(taskrun._state)

    def tearDown(self) -> None:
        taskrun._state.update(self._saved)

    def test_picks_done_line_not_mode_line(self) -> None:
        taskrun._state.update({
            'lines': [
                "mode=DRY-RUN accounts=['99a07e71'] only=all only_claim=False gap=1.0",
                '== 99a07e71 (测试号) ==',
                'task_runner done: accounts=1 total=0 ok=3 already=1 fail=0 credit=+100',
            ],
            'exit_code': 0, 'error': '', 'timed_out': False,
        })
        out = taskrun._summarize()
        self.assertTrue(out.startswith('task_runner done:'), out)
        self.assertNotIn('mode=', out, '抓到参数行了')
        self.assertIn('credit=+100', out)

    def test_falls_back_to_error(self) -> None:
        taskrun._state.update({'lines': [], 'exit_code': None,
                               'error': '无法启动脚本：No such file', 'timed_out': False})
        self.assertIn('无法启动脚本', taskrun._summarize())

    def test_falls_back_to_exit_code(self) -> None:
        taskrun._state.update({'lines': ['some output'], 'exit_code': 3,
                               'error': '', 'timed_out': False})
        self.assertIn('3', taskrun._summarize())


class CommandBuildTest(unittest.TestCase):
    """argv 构造：模式白名单 + 账号标识字符校验。"""

    def test_modes_map_to_expected_flags(self) -> None:
        prev = taskrun.build_command('preview', 'ALL')
        self.assertNotIn('--yes', prev, 'preview 必须是 dry-run（只读）')
        claim = taskrun.build_command('claim', 'ALL')
        self.assertIn('--yes', claim)
        self.assertIn('--only-claim', claim)
        full = taskrun.build_command('full', 'ALL')
        self.assertIn('--yes', full)
        self.assertNotIn('--only-claim', full)

    def test_account_injection_rejected(self) -> None:
        for bad in ('a;b', 'a b', '../x', 'a|b', '$(whoami)', '`id`', '', 'x' * 65,
                    'a\nb', 'a&b'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                taskrun.build_command('claim', bad)

    def test_legit_account_accepted(self) -> None:
        for good in ('ALL', '99a07e71', 'abcdef01', 'u-in', 'A' * 64):
            cmd = taskrun.build_command('claim', good)
            # 账号标识作为独立 argv 传入（紧跟脚本路径之后）
            self.assertIn(good, cmd, f'{good} 未出现在 argv 里')
            self.assertEqual(cmd[cmd.index(str(taskrun._script_path())) + 1], good)

    def test_unbuffered_flags_present(self) -> None:
        """必须让子进程**不缓冲**输出。

        非交互（管道）时 Python 的 stdout 是块缓冲，攒满 8KB 才刷；脚本每行约
        80 字节，一次全量要跑满约 100 行才吐出第一批——用户看到的是「点了做任务
        卡半天没输出，然后突然冒出一大段」（线上实测）。`-u` 与
        `PYTHONUNBUFFERED=1` 两处都要在：任一被忽略时另一个兜住。
        """
        for mode in ('preview', 'claim', 'full'):
            cmd = taskrun.build_command(mode, 'ALL')
            self.assertIn('-u', cmd, f'{mode}: 缺 -u，输出会被块缓冲')
            # -u 必须放在脚本路径**之前**，否则会被当成脚本参数
            self.assertLess(cmd.index('-u'), cmd.index(str(taskrun._script_path())),
                            f'{mode}: -u 必须在脚本路径之前')

    def test_unknown_mode_rejected(self) -> None:
        for bad in ('', 'FULL', 'full; rm -rf /', 'previewx', None):
            with self.assertRaises(ValueError):
                taskrun.build_command(bad, 'ALL')  # type: ignore[arg-type]


class AvailabilityTest(unittest.TestCase):
    def test_reports_reason_when_script_missing(self) -> None:
        """脚本缺失要**说明是什么、怎么办**，而不是只回「失败」。"""
        with mock.patch.object(taskrun, '_script_path', lambda: Path('/nope/task_runner.py')):
            ok, why = taskrun.available()
        self.assertFalse(ok)
        self.assertIn('task_runner.py', why)
        self.assertIn('workbuddy2api', why, '要说清这是上游的脚本')

    def test_refuses_to_start_when_unavailable(self) -> None:
        # _prepare 会把分组的账号目录传进来（issue #274），lambda 必须收下它
        with mock.patch.object(taskrun, 'available',
                               lambda auth_dir=None: (False, '脚本不在')):
            ok, msg = taskrun.start('claim', 'ALL')
        self.assertFalse(ok)
        self.assertIn('脚本不在', msg)

    def test_available_checks_the_selected_group_dir(self) -> None:
        """分组任务要按**该分组**的账号目录判断可用性（issue #274）。"""
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'task_runner.py'
            script.write_text('# stub\n', encoding='utf-8')
            missing = Path(td) / 'missing'
            with mock.patch.object(taskrun, '_script_path', return_value=script):
                ok, why = taskrun.available(missing)
                self.assertFalse(ok)
                self.assertIn(str(missing), why)
                ok, _ = taskrun.available(td)
                self.assertTrue(ok)

    def test_prepare_carries_group_auth_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'task_runner.py'
            script.write_text('# stub\n', encoding='utf-8')
            prev = taskrun._state['running']
            taskrun._state['running'] = False
            try:
                with mock.patch.object(taskrun, '_script_path', return_value=script):
                    ok, payload = taskrun._prepare('claim', 'ALL', td, '分组B')
            finally:
                taskrun._state['running'] = prev
        self.assertTrue(ok, payload)
        self.assertEqual(payload['auth_dir'], str(Path(td)))
        self.assertEqual(payload['group'], '分组B')


class ScriptExtractionTest(unittest.TestCase):
    """从上游容器提取脚本（issue #29）。

    报障现象：官方**镜像**部署上游时，面板说
    「未找到上游任务脚本（/opt/workbuddy2api/scripts/task_runner.py）」。
    原因是上游镜像把脚本 COPY 进容器内的 `/app/scripts/`，而宿主机挂载目录里
    根本没有 `scripts/`（用户只挂了 config.json 与 auths）—— 于是无论怎么配
    都找不到，除非手动从镜像里扒脚本出来（那就每次上游更新都要重来一遍）。

    修法：找不到就 `docker cp` 从容器里提取到本地缓存，并以**镜像 ID** 为指纹
    —— 上游重建镜像后自动重取，脚本与上游二进制始终同版本。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_data = config.DATA_DIR
        self._orig_auth = config.AUTH_DIR
        config.DATA_DIR = Path(self._tmp.name)
        config.AUTH_DIR = Path(self._tmp.name) / 'auths'
        config.AUTH_DIR.mkdir(parents=True, exist_ok=True)
        taskrun._last_extract_failure.update({'at': 0.0, 'reason': ''})

    def tearDown(self) -> None:
        config.DATA_DIR = self._orig_data
        config.AUTH_DIR = self._orig_auth
        taskrun._last_extract_failure.update({'at': 0.0, 'reason': ''})
        self._tmp.cleanup()

    def _fake_cp(self, *, rc: int = 0, files=('task_runner.py', 'task_common.py',
                                             'school_open_day_2026.py')):
        """伪造 docker cp：按需写出脚本文件。"""
        def _run(cmd, **kw):
            class R:
                returncode = rc
                stdout = ''
                stderr = '' if rc == 0 else 'No such container: workbuddy2api'
            if rc == 0:
                dest = taskrun._extract_dir()
                dest.mkdir(parents=True, exist_ok=True)
                for f in files:
                    (dest / f).write_text('# stub\n', encoding='utf-8')
            return R()
        return _run

    def test_extracts_whole_script_group(self) -> None:
        """要提取**整组**脚本，不能只拿 task_runner.py。

        task_runner `import task_common` 与 `school_open_day_2026`（后者又
        import task_common）；只拷一个文件的话，运行时会 ImportError——
        而那时用户已经点了「做任务」，看到的是脚本崩了。
        """
        with mock.patch('subprocess.run', side_effect=self._fake_cp()), \
             mock.patch.object(taskrun, '_upstream_image_id', return_value='img1'):
            ok, path = taskrun.extract_scripts()
        self.assertTrue(ok, path)
        dest = taskrun._extract_dir()
        for name in ('task_runner.py', 'task_common.py', 'school_open_day_2026.py'):
            self.assertTrue((dest / name).is_file(), f'缺少 {name}')

    def test_cp_uses_container_and_app_scripts(self) -> None:
        """docker cp 的来源必须是 `<容器>:/app/scripts/.`（上游镜像里的位置）。"""
        seen: list[list[str]] = []

        def _run(cmd, **kw):
            seen.append(list(cmd))
            class R:
                returncode = 0; stdout = ''; stderr = ''
            dest = taskrun._extract_dir()
            dest.mkdir(parents=True, exist_ok=True)
            (dest / 'task_runner.py').write_text('# x', encoding='utf-8')
            return R()

        with mock.patch('subprocess.run', side_effect=_run), \
             mock.patch.object(taskrun, '_upstream_image_id', return_value='img1'):
            taskrun.extract_scripts()
        self.assertTrue(seen, '没有调用 docker')
        cmd = seen[0]
        self.assertEqual(cmd[:2], ['docker', 'cp'])
        self.assertTrue(cmd[2].endswith(':/app/scripts/.'), cmd[2])
        self.assertIn(config.WB2API_CONTAINER, cmd[2])

    def test_host_mount_wins_over_extraction(self) -> None:
        """宿主机挂载目录里有脚本时**直接用**，不碰 docker。

        源码部署的用户没必要为这个功能装 docker / 挂 socket。
        """
        host = Path(self._tmp.name) / 'upstream' / 'scripts'
        host.mkdir(parents=True)
        (host / 'task_runner.py').write_text('# host\n', encoding='utf-8')
        with mock.patch.object(taskrun, '_host_script',
                              return_value=host / 'task_runner.py'), \
             mock.patch('subprocess.run') as sl:
            got = taskrun._script_path()
        self.assertEqual(got, host / 'task_runner.py')
        sl.assert_not_called()

    def test_cache_invalidated_when_image_changes(self) -> None:
        """上游重建镜像后必须重新提取 —— 否则脚本与上游版本漂移。"""
        with mock.patch('subprocess.run', side_effect=self._fake_cp()), \
             mock.patch.object(taskrun, '_upstream_image_id', return_value='img-old'):
            taskrun.extract_scripts()
        with mock.patch.object(taskrun, '_upstream_image_id', return_value='img-old'):
            self.assertTrue(taskrun._cache_is_fresh(), '同镜像应命中缓存')
        with mock.patch.object(taskrun, '_upstream_image_id', return_value='img-new'):
            self.assertFalse(taskrun._cache_is_fresh(), '换了镜像就该重取')

    def test_failure_is_cooled_down(self) -> None:
        """失败后要冷却，否则界面每 15 秒轮询会变成每 15 秒 fork 一次失败 docker。

        面板闲着时按 15 秒轮询 status()，而 status() → available() → 可能触发
        提取；没有冷却的话，用户只是把页面开着，就会持续产生失败的子进程。
        """
        calls = {'n': 0}

        def _fail(cmd, **kw):
            calls['n'] += 1
            class R:
                returncode = 1; stdout = ''; stderr = 'No such container'
            return R()

        with mock.patch('subprocess.run', side_effect=_fail):
            for _ in range(5):
                taskrun.available()
        self.assertEqual(calls['n'], 1, f'冷却没生效，试了 {calls["n"]} 次')

        # 冷却过去后允许再试（不能永久放弃）
        taskrun._last_extract_failure['at'] = time.time() - 61
        with mock.patch('subprocess.run', side_effect=_fail):
            taskrun.available()
        self.assertEqual(calls['n'], 2, '冷却过后应允许再试')

    def test_successful_extract_resets_cooldown(self) -> None:
        """成功要清掉失败标记，免得下一次因冷却被跳过。"""
        with mock.patch('subprocess.run', side_effect=self._fake_cp(rc=1)):
            taskrun.available()
        self.assertNotEqual(taskrun._last_extract_failure['at'], 0.0)

        taskrun._last_extract_failure['at'] = time.time() - 61
        with mock.patch('subprocess.run', side_effect=self._fake_cp()), \
             mock.patch.object(taskrun, '_upstream_image_id', return_value='img1'):
            taskrun.available()
        self.assertEqual(taskrun._last_extract_failure['at'], 0.0,
                         '成功后没清冷却标记')

    def test_error_message_mentions_both_deployment_shapes(self) -> None:
        """失败说明要覆盖两种部署形态的修法 —— 用户只知道「找不到脚本」。"""
        with mock.patch('subprocess.run', side_effect=self._fake_cp(rc=1)), \
             mock.patch.object(taskrun, '_host_script', return_value=None):
            ok, why = taskrun.available()
        self.assertFalse(ok)
        self.assertIn('docker.sock', why, '镜像部署要说清需要 docker 访问')
        self.assertIn('WB_UPSTREAM_DIR', why, '源码部署要指出挂载目录')


class TimeoutPolicyTest(unittest.TestCase):
    """超时策略：**按空闲判定**，不看总时长。

    初版用的是固定总时长（30 分钟），而脚本耗时随账号数线性增长——全量一轮每号
    约 40 个写动作、动作间隔 ≥1s，54 个账号光下限就约 36 分钟。于是大池子的
    **正常**全量会被中途杀掉，账号做一半、还得重跑，比不设超时更糟。

    正确模型：只要还有输出就说明活着（每个动作都会打一行），静默超阈值才判卡死。
    """

    def setUp(self) -> None:
        # start() 会先做前置检查（脚本存在 + 账号目录存在），测试里要让它过
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_auth = config.AUTH_DIR
        config.AUTH_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig_auth
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_idle_timeout_is_generous_but_bounded(self) -> None:
        """空闲阈值要明显大于「单个动作最长耗时」，且必须有兜底总上限。"""
        self.assertGreaterEqual(taskrun.IDLE_TIMEOUT_SECONDS, 120,
                                '太短会把慢动作误判成卡死')
        self.assertGreater(taskrun.MAX_TOTAL_SECONDS, 3600,
                           '兜底总上限太短会砍掉大池子的正常全量')
        # 36 分钟（54 账号下限估算）必须落在兜底上限之内
        self.assertGreater(taskrun.MAX_TOTAL_SECONDS, 36 * 60,
                           '兜底上限仍会砍掉 54 账号的正常全量')

    def _run_script(self, body: str, idle: int) -> dict:
        """跑一个替身脚本并返回最终状态（idle = 空闲阈值，压小便于测）。"""
        script = Path(self._tmp.name) / 'fake.py'
        script.write_text(body, encoding='utf-8')

        async def go() -> dict:
            with mock.patch.object(taskrun, '_script_path', lambda: script), \
                 mock.patch.object(taskrun, 'IDLE_TIMEOUT_SECONDS', idle):
                taskrun._state.update({'running': False, 'lines': [], 'error': '',
                                       'exit_code': None, 'timed_out': False})
                ok, msg = taskrun.start('preview', 'ALL')
                assert ok, msg
                for _ in range(300):
                    if not taskrun.status()['running']:
                        break
                    await asyncio.sleep(0.1)
                return taskrun.status()

        return asyncio.run(go())

    def test_progress_prevents_stall_detection(self) -> None:
        """持续有输出时不该被判卡死 —— 即使总时长超过旧阈值。"""
        st = self._run_script(
            'import time\n'
            'for i in range(6):\n'
            '    print(f"step {i}")\n'
            '    time.sleep(0.4)\n'
            'print("task_runner done: ok=6")\n',
            idle=1)
        self.assertFalse(st['timed_out'], f'持续有输出却被判超时：{st["lines"]}')
        self.assertEqual(st['exit_code'], 0)
        self.assertTrue(any('done' in ln for ln in st['lines']), f'没跑完：{st["lines"]}')

    def test_silent_process_is_killed(self) -> None:
        """真的卡死（长时间无任何输出）要终止，不能无限挂着。"""
        st = self._run_script(
            'import time\n'
            'print("starting")\n'
            'time.sleep(30)\n'
            'print("never")\n',
            idle=1)
        self.assertTrue(st['timed_out'], '静默进程没被判定卡死')
        self.assertTrue(any('卡死' in ln for ln in st['lines']),
                        f'缺少卡死说明：{st["lines"]}')


class StreamingOutputTest(unittest.TestCase):
    """首个输出行必须在**进程结束前**就能被读到（不能攒到最后一起出）。

    这是「卡半天没反应」那个问题的行为判据：判据不是「最终能读到输出」，而是
    「进程还在跑的时候就能读到」。

    测法：直接用 `build_command()` 造出真实 argv 跑一个替身脚本（立刻打印、
    随后睡 3 秒），看第一行到达时刻。**不经过 `taskrun.start()`** —— 那需要
    一个长期存活的事件循环（后台任务），在单测里 `asyncio.run` 收尾时会把
    未完成的子进程任务挂住，反倒测不准（写这版时踩到过：测试直接卡死）。

    ⚠️ **这条用例在 Windows 上分辨不出缓冲问题**（实测：摘掉 `-u` 它照样绿）——
    Windows 的管道 stdout 行为与 Linux 不同，子进程写管道是立即可见的。真正
    守这件事的是 `CommandBuildTest.test_unbuffered_flags_present`：它断言 argv
    里必须带 `-u`（反证过：摘掉后该用例变红）。这条行为用例只在 Linux 上才有
    分辨力，属于「换个平台能多一层保障」，不能拿它当唯一防线。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.script = self.root / 'fake_task.py'
        # 第一行用 flush 保证「若缓冲则第二行也要等」的语义清晰；替身脚本模拟
        # 真实脚本的形态：先打头部，再干慢活
        self.script.write_text(
            'import time\n'
            'print("mode=REAL accounts=[\'x\']")\n'
            'time.sleep(3)\n'
            'print("task_runner done: ok=1")\n',
            encoding='utf-8')
        self._orig_auth = config.AUTH_DIR
        config.AUTH_DIR = self.root
        self._patch = mock.patch.object(taskrun, '_script_path', lambda: self.script)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        config.AUTH_DIR = self._orig_auth
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_first_line_arrives_long_before_process_exit(self) -> None:
        async def measure() -> tuple[float | None, float]:
            argv = taskrun.build_command('preview', 'ALL')
            t0 = time.time()
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(self.root),
                env={**taskrun.os_environ(), 'WB2A_AUTHS': str(self.root),
                     'PYTHONUNBUFFERED': '1'},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
            first: float | None = None
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                if first is None:
                    first = time.time() - t0
            await proc.wait()
            return first, time.time() - t0

        first, total = asyncio.run(measure())
        self.assertIsNotNone(first, '一行输出都没有')
        # 脚本要睡 3 秒；第一行若被块缓冲，会跟第二行一起在 ~3s 处才出现
        self.assertLess(first or 99, 2.0,
                        f'第一行等了 {first:.2f}s（总时长 {total:.2f}s）—— '
                        '输出被块缓冲了，用户在界面上看到的就是「卡住没反应」')


class EventLoopResponsivenessTest(unittest.TestCase):
    """事件循环不能被 docker 调用冻住。

    背景（发版前自审发现）：脚本缺失时会触发 `docker inspect`（≤15s）与
    `docker cp`（≤60s）去尝试提取，而**这些是同步阻塞调用**。它们的调用方里
    有三个跑在事件循环里：定时领奖循环、启动任务的 async 接口、以及执行任务
    的 `_run()`。一旦在这条路径上同步跑 docker，整个服务（**包括对外的
    `/v1/*` 网关**）都会卡住 —— 而网关才是用户真正在用的东西。

    这里的判据是「心跳协程还能不能按时醒」：同步阻塞期间它一次都醒不了。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_data = config.DATA_DIR
        self._orig_auth = config.AUTH_DIR
        config.DATA_DIR = Path(self._tmp.name)
        config.AUTH_DIR = Path(self._tmp.name) / 'auths'
        config.AUTH_DIR.mkdir(parents=True, exist_ok=True)
        taskrun._last_extract_failure.update({'at': 0.0, 'reason': ''})
        taskrun._image_id_cache.update({'at': 0.0, 'id': ''})

    def tearDown(self) -> None:
        config.DATA_DIR = self._orig_data
        config.AUTH_DIR = self._orig_auth
        taskrun._last_extract_failure.update({'at': 0.0, 'reason': ''})
        taskrun._image_id_cache.update({'at': 0.0, 'id': ''})
        self._tmp.cleanup()

    def _slow_docker(self, delay: float):
        def _run(cmd, **kw):
            time.sleep(delay)
            class R:
                returncode = 1
                stdout = ''
                stderr = 'slow'
            return R()
        return _run

    @contextlib.contextmanager
    def _script_available(self):
        """把「脚本在位」造出来，让 `available()` 返回 True。

        为什么需要：`start()` 会先查 `available()`，脚本缺失时它在到达
        `get_running_loop()` 之前就返回了 —— 那种情况下「启动失败」是**预期的**，
        测不出「事件循环缺失」这类真问题（issue #31 正是这样漏掉的）。
        """
        dest = taskrun._extract_dir()
        dest.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        for name in ('task_runner.py', 'task_common.py', 'school_open_day_2026.py'):
            p = dest / name
            if not p.exists():
                p.write_text('# stub\n', encoding='utf-8')
                created.append(p)
        taskrun._last_extract_failure.update({'at': 0.0, 'reason': ''})
        taskrun._state['running'] = False
        try:
            with mock.patch.object(taskrun, '_host_script', return_value=None), \
                 mock.patch.object(taskrun, '_cache_is_fresh', return_value=True):
                yield dest
        finally:
            taskrun._state['running'] = False
            for p in created:
                p.unlink(missing_ok=True)

    def _heartbeat_during(self, coro_factory, delay: float) -> int:
        """跑 coro_factory() 期间统计心跳次数（事件循环没被冻住就会有几十次）。"""
        async def main() -> int:
            ticks: list[float] = []
            stop = asyncio.Event()

            async def beat() -> None:
                while not stop.is_set():
                    ticks.append(time.time())
                    await asyncio.sleep(0.05)

            hb = asyncio.create_task(beat())
            # 先让心跳跑一次再开始计时：`delay=0` 的占位调用可能在调度器还没轮到
            # 心跳任务时就返回，那一轮 ticks 会是 0（CI 上实测偶发）。这不是被测
            # 行为，只是起跑顺序，所以显式让出一次。
            await asyncio.sleep(0.01)
            with mock.patch('subprocess.run', side_effect=self._slow_docker(delay)):
                await coro_factory()
            stop.set()
            await hb
            return len(ticks)

        return asyncio.run(main())

    def test_available_async_does_not_block_loop(self) -> None:
        """`available_async()` 必须把 docker 调用挪出事件循环。"""
        ticks = self._heartbeat_during(lambda: taskrun.available_async(), 1.0)
        self.assertGreater(ticks, 5,
                           f'提取期间事件循环只跳了 {ticks} 次 —— docker 调用阻塞了循环')

    def test_start_async_does_not_block_loop(self) -> None:
        """启动任务的 async 接口同理。"""
        ticks = self._heartbeat_during(lambda: taskrun.start_async('claim', 'ALL'), 1.0)
        self.assertGreater(ticks, 5,
                           f'启动期间事件循环只跳了 {ticks} 次 —— 会连带卡住对外网关')

    def test_start_async_actually_starts(self) -> None:
        """`start_async()` 必须**真的把任务启动起来**，而不只是不阻塞。

        这是 issue #31 的回归测试。那个 bug 之所以能发出去，正是因为上一条测试
        **只数了心跳、丢掉了返回值**，而且当时 `available()` 恰好是失败的 ——
        `start()` 在到达 `get_running_loop()` 之前就提前返回了，于是「不阻塞」
        这条断言以**错误的原因**通过。真正的问题出在下一行：
        `start_async` 把整个 `start()` 丢进线程池，而线程池的工作线程没有运行中
        的事件循环，`get_running_loop()` 必然抛 RuntimeError →
        100% 报「当前环境没有事件循环，无法后台执行」。

        所以这里把 available() 造成功（脚本在位），并断言启动**成功**。
        """
        async def main() -> tuple[bool, str]:
            with mock.patch.object(taskrun, '_run', new=mock.AsyncMock()):
                return await taskrun.start_async('preview', 'ALL')

        with self._script_available():
            ok, msg = asyncio.run(main())
        self.assertTrue(ok, f'启动失败了：{msg}')
        self.assertIn('已开始执行', msg)
        # 断言「登记过运行态」，而不是要求此刻仍是 running：asyncio.run 收尾会取消
        # 未完成的 _run，其 finally 会把 running 复位 —— 那是正常收尾，与「根本
        # 没启动」是两回事。用 started_at / mode / target 判断有没有真的登记。
        self.assertGreater(taskrun._state['started_at'], 0, '没有登记启动时间')
        self.assertEqual(taskrun._state['mode'], 'preview')
        self.assertEqual(taskrun._state['target'], 'ALL')

    def test_sync_start_works_inside_loop(self) -> None:
        """同步版在事件循环线程里也要能启动（命令行/测试路径）。"""
        async def main() -> tuple[bool, str]:
            with mock.patch.object(taskrun, '_run', new=mock.AsyncMock()):
                return taskrun.start('claim', 'ALL')

        with self._script_available():
            ok, msg = asyncio.run(main())
        self.assertTrue(ok, f'同步版启动失败：{msg}')

    def test_sync_start_without_loop_reports_clearly(self) -> None:
        """没有事件循环时，同步版应保留原来的可读提示（而不是抛异常）。"""
        with self._script_available():
            ok, msg = taskrun.start('claim', 'ALL')   # 主线程无 loop
        self.assertFalse(ok)
        self.assertIn('事件循环', msg)

    def test_sync_call_still_blocks_by_design(self) -> None:
        """反证：同步版**确实**会阻塞 —— 证明上面两条测的是真问题。

        若哪天有人在事件循环里误用同步版，心跳会掉到接近 0；本用例把这个
        差异钉住，免得「都改成 async」这个结论被无意改回去。
        """
        ticks = self._heartbeat_during(
            lambda: asyncio.to_thread(lambda: None), 0.0)  # 占位：正常情况
        self.assertGreater(ticks, 0)

        async def call_sync() -> None:
            taskrun.available()   # 同步调用，直接在事件循环里跑

        ticks_blocked = self._heartbeat_during(call_sync, 1.0)
        self.assertLessEqual(ticks_blocked, 2,
                             '同步调用居然没阻塞循环？那这条测试的前提就不成立了，需重新审视')

    def test_image_id_is_cached(self) -> None:
        """镜像 ID 要缓存：它是被 15 秒轮询的路径，不缓存等于每轮 fork 一次 docker。"""
        calls = {'n': 0}

        def _run(cmd, **kw):
            calls['n'] += 1
            class R:
                returncode = 0
                stdout = 'sha256:abc\n'
                stderr = ''
            return R()

        taskrun._image_id_cache.update({'at': 0.0, 'id': ''})
        with mock.patch('subprocess.run', side_effect=_run):
            for _ in range(5):
                taskrun._upstream_image_id()
        self.assertEqual(calls['n'], 1, f'镜像 ID 没缓存，查了 {calls["n"]} 次 docker')


class ScheduleTest(unittest.TestCase):
    """定时领奖配置：输入校验 + 只存 claim 所需的东西。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 's.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_default_is_disabled(self) -> None:
        cfg = taskrun.get_schedule()
        self.assertFalse(cfg['enabled'])
        self.assertTrue(cfg['hours'])

    def test_roundtrip(self) -> None:
        taskrun.set_schedule(True, [9, 21])
        self.assertEqual(taskrun.get_schedule(), {'enabled': True, 'hours': [9, 21]})

    def test_hours_validated_and_deduped(self) -> None:
        taskrun.set_schedule(True, [21, 9, 21])
        self.assertEqual(taskrun.get_schedule()['hours'], [9, 21])
        for bad in ([24], [-1], ['9'], [True], [], 'x', None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                taskrun.set_schedule(True, bad)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            taskrun.set_schedule('yes', [10])  # type: ignore[arg-type]


class EndpointTest(unittest.TestCase):
    """接口层：权限、模式校验、full 需确认。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'e.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [
                {'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')},
                {'username': 'viewer', 'role': 'viewer', 'pwd_hash': security.make_hash('p')},
            ],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _login(self, username: str) -> None:
        r = self.client.post('/api/login', json={'username': username, 'password': 'p'})
        self.assertEqual(r.status_code, 200, r.text)

    def test_requires_admin(self) -> None:
        """只读用户不得触发（这些操作会对账号发起真实写请求）。"""
        self._login('viewer')
        self.assertEqual(self.client.get('/api/task-run').status_code, 403)
        self.assertEqual(self.client.post(
            '/api/task-run', json={'mode': 'preview', 'target': 'ALL'}).status_code, 403)
        self.assertEqual(self.client.post('/api/task-run/stop').status_code, 403)
        self.assertEqual(self.client.put(
            '/api/task-claim-schedule', json={'enabled': True, 'hours': [10]}).status_code, 403)

    def test_anonymous_requires_auth(self) -> None:
        self.assertEqual(self.client.get('/api/task-run').status_code, 401)

    def test_invalid_mode_rejected(self) -> None:
        self._login('admin')
        r = self.client.post('/api/task-run', json={'mode': 'nuke', 'target': 'ALL'})
        self.assertEqual(r.status_code, 400, r.text)

    def test_full_requires_explicit_confirm(self) -> None:
        """点亮模式必须显式确认 —— 它是唯一会伪造活跃上报的模式。"""
        self._login('admin')
        r = self.client.post('/api/task-run', json={'mode': 'full', 'target': 'ALL'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('风控', r.json()['detail'])
        # 传了 confirm 才进入执行流程（脚本不存在时是 409，而非 400）
        # 路由调的是 start_async（避免阻塞事件循环），patch 目标要一致
        with mock.patch.object(taskrun, 'start_async',
                               new=mock.AsyncMock(return_value=(True, '已开始'))):
            r2 = self.client.post('/api/task-run',
                                  json={'mode': 'full', 'target': 'ALL', 'confirm': True})
        self.assertEqual(r2.status_code, 200, r2.text)

    def test_claim_and_preview_need_no_confirm(self) -> None:
        self._login('admin')
        # 路由调的是 start_async（避免阻塞事件循环），patch 目标要一致 ——
        # 早先这里还 patch 同步的 start，patch 因此无效、测试实际走到真实路径，
        # 却又因为「脚本不存在」返回 409 而失败得莫名其妙（或反之被误当成通过）。
        with mock.patch.object(taskrun, 'start_async',
                               new=mock.AsyncMock(return_value=(True, '已开始'))):
            for mode in ('preview', 'claim'):
                r = self.client.post('/api/task-run', json={'mode': mode, 'target': 'ALL'})
                self.assertEqual(r.status_code, 200, f'{mode}: {r.text}')

    def test_conflict_returns_409(self) -> None:
        """已在跑 → 409（状态冲突），与「请求有错」400 区分开。"""
        self._login('admin')
        with mock.patch.object(taskrun, 'start_async',
                               new=mock.AsyncMock(return_value=(False, '已有任务正在执行'))):
            r = self.client.post('/api/task-run', json={'mode': 'claim', 'target': 'ALL'})
        self.assertEqual(r.status_code, 409, r.text)

    def test_schedule_validation(self) -> None:
        self._login('admin')
        r = self.client.put('/api/task-claim-schedule',
                            json={'enabled': True, 'hours': [9, 21]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {'enabled': True, 'hours': [9, 21]})
        bad = self.client.put('/api/task-claim-schedule',
                              json={'enabled': True, 'hours': [25]})
        self.assertEqual(bad.status_code, 400, bad.text)


class SchedulerOnlyClaimsTest(unittest.TestCase):
    """定时调度**只能**跑 claim —— 点亮绝不进定时。"""

    def test_scheduler_invokes_claim_only(self) -> None:
        calls: list[tuple[str, str]] = []

        async def fake_start(mode: str, target: str):
            calls.append((mode, target))
            return True, 'ok'

        # 固定"当前时间"落在配置的整点档内
        with mock.patch.object(taskrun, 'get_schedule', lambda: {'enabled': True, 'hours': [3]}), \
             mock.patch.object(taskrun, 'start_async', fake_start), \
             mock.patch.object(taskrun, '_last_claim_day', ''), \
             mock.patch('time.localtime',
                        lambda *a: __import__('time').struct_time(
                            (2026, 9, 16, 3, 1, 0, 2, 259, 0))):
            taskrun._last_claim_day = ''
            asyncio.run(self._one_tick())

        modes = {m for m, _ in calls}
        self.assertTrue(modes, '调度器应触发一次执行')
        self.assertEqual(modes, {'claim'},
                         f'定时只允许跑 claim，实际触发了 {modes}')

    async def _one_tick(self) -> None:
        """跑 _claim_loop 的一轮（让它 sleep 时抛错退出，避免挂住）。"""
        class _Boom(Exception):
            pass

        orig_sleep = asyncio.sleep

        async def fake_sleep(_s):  # type: ignore[no-untyped-def]
            raise _Boom

        with mock.patch.object(asyncio, 'sleep', fake_sleep):
            try:
                await taskrun._claim_loop()
            except _Boom:
                pass
        _ = orig_sleep

    def test_disabled_schedule_does_not_run(self) -> None:
        calls: list[tuple[str, str]] = []
        with mock.patch.object(taskrun, 'get_schedule', lambda: {'enabled': False, 'hours': [3]}), \
             mock.patch.object(taskrun, 'start_async',
                               new=mock.AsyncMock(
                                   side_effect=lambda m, t: (calls.append((m, t)), (True, 'ok'))[1])):
            asyncio.run(self._one_tick())
        self.assertEqual(calls, [], '未启用时不该触发')


class ChildStdioEncodingTest(unittest.TestCase):
    """任务子进程必须按 UTF-8 说话（issue #147）。

    报障：Windows 中文区域原生部署下，账号昵称里带 emoji（🏅）时任务一个账号都没跑
    就退出——脚本 `print(f"== {uid8} ({auth['nick']}) ==")` 那一行，Python 默认按
    ANSI 代码页（cp936）编码 stdout，emoji 编不出去 → `UnicodeEncodeError`。
    面板这侧读回来时用的是 UTF-8，两端本来就该是同一个编码。

    这里分两层验：
      · 环境形状：子进程环境里必须有 PYTHONUTF8 / PYTHONIOENCODING；
      · 真实效果：**故意让继承来的环境说 cp936**，脚本打印 emoji 仍要跑完。
        没有上面那两行时，子进程会照继承值走 cp936 → 崩（这条测试就是反证）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.script = self.root / 'fake_task.py'
        self._orig_auth = config.AUTH_DIR
        config.AUTH_DIR = self.root
        self._patch = mock.patch.object(taskrun, '_script_path', lambda: self.script)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        config.AUTH_DIR = self._orig_auth
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_child_env_pins_utf8_stdio(self) -> None:
        captured: dict = {}

        class _Stdout:
            """最小 stdout：立刻 EOF，让 pump 正常收尾（不给它制造异常路径）。"""

            async def readline(self):
                return b''

        class _Proc:
            stdout = _Stdout()
            returncode = 0

            async def wait(self):
                return 0

        async def fake_exec(*argv, **kwargs):  # noqa: ANN001
            captured.update(kwargs)
            return _Proc()

        with mock.patch.object(asyncio, 'create_subprocess_exec', fake_exec):
            asyncio.run(taskrun._run(taskrun.build_command('preview', 'ALL'),
                                     'preview', 'ALL'))
        env = captured.get('env') or {}
        self.assertEqual(env.get('PYTHONUTF8'), '1',
                         '子进程没开 UTF-8 模式 —— Windows 上会按 ANSI 代码页编码输出')
        self.assertTrue(str(env.get('PYTHONIOENCODING', '')).startswith('utf-8'),
                        f'PYTHONIOENCODING={env.get("PYTHONIOENCODING")!r}，应为 utf-8')

    def test_emoji_nickname_survives_a_cp936_inherited_env(self) -> None:
        """继承环境故意说自己用 cp936：面板传下去的 UTF-8 必须压过它。"""
        self.script.write_text(
            'print("\U0001f3c5 昵称带奖牌的任务开始")\n'
            'print("task_runner done: ok=1")\n',
            encoding='utf-8')
        inherited = {**taskrun.os_environ(),
                     'PYTHONIOENCODING': 'cp936', 'PYTHONUTF8': '0'}
        with mock.patch.object(taskrun, 'os_environ', lambda: inherited):
            asyncio.run(taskrun._run(taskrun.build_command('preview', 'ALL'),
                                     'preview', 'ALL'))
        text = '\n'.join(str(line) for line in taskrun.status().get('lines', []))
        self.assertNotIn('UnicodeEncodeError', text,
                         f'子进程仍在按 cp936 编码输出（issue #147 复发）：\n{text[-400:]}')
        self.assertIn('\U0001f3c5', text, 'emoji 没原样传回来（说明编码对不上）')
        self.assertIn('done: ok=1', text, '任务没跑完')


class ForceUtf8StdioTest(unittest.TestCase):
    """面板自身的标准输出也要钉成 UTF-8（同族故障：日志里带 emoji）。"""

    def test_reconfigures_with_utf8_and_replace(self) -> None:
        from server.stdio_utf8 import force_utf8_stdio
        calls: list[dict] = []

        class _Stream:
            def reconfigure(self, **kw):  # noqa: ANN003
                calls.append(kw)

        force_utf8_stdio((_Stream(), _Stream()))
        self.assertEqual(len(calls), 2)
        for kw in calls:
            self.assertEqual(kw.get('encoding'), 'utf-8')
            self.assertEqual(kw.get('errors'), 'replace',
                             'errors=replace 是兜底：宁可打成 ? 也不能死在日志上')

    def test_tolerates_streams_without_reconfigure(self) -> None:
        """StringIO 之类的流没有 reconfigure —— 不能因此抛异常。"""
        import io as _io
        from server.stdio_utf8 import force_utf8_stdio
        force_utf8_stdio((_io.StringIO(),))

    def test_main_calls_it_at_import(self) -> None:
        """入口必须真的调用它（否则上面两条只是测了个没人用的函数）。"""
        src = (Path(__file__).resolve().parents[2] / 'server' / 'main.py').read_text(encoding='utf-8')
        self.assertIn('force_utf8_stdio()', src)


if __name__ == '__main__':
    unittest.main()


if __name__ == '__main__':
    unittest.main()

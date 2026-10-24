"""更新状态机生命周期的回归测试（2026-09-26「正在更新：执行中」卡死事故）。

真实事故：更新子进程死在解释器 site 初始化期（管理端控制台的 Ctrl+C/关窗
连坐），update-status.json 从未写出、update.lock 里的 pid 又被 os.kill(pid,0)
误判存活，前端永远停在「正在更新：执行中」、「上次更新」显示「从未」。

这里覆盖四件事：
  1. _pid_alive 在 Windows 上用 OpenProcess+GetExitCodeProcess 判活，不再把
     「已死但内核对象尚可打开」的 pid 误报为存活；POSIX 分支保持
     os.kill(pid,0) 原语义不动。
  2. start_update 的初始状态文件（_write_initial_status）能让 read_status 在
     「子进程瞬间死亡」时合成出 ok=false 的终止态，且与 _update_landed 的
     字段口径兼容（不误判成功）。
  3. worker 的 main() 所有退出路径（异常 / Ctrl+C 中断 / 正常）都落盘终态，
     不给前端留悬空的 running=true。
  4. 下载对偶发网络失败重试一次，4xx 不重试（用本地真实 HTTP 服务验证，
     不 mock 标准库）。

运行：python -m pytest server/tests/test_update_lifecycle.py -q
"""
from __future__ import annotations

import inspect
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import updater  # noqa: E402

# 复用 worker（deploy/update.py 是独立脚本，不在包内）
DEPLOY_DIR = pathlib.Path(__file__).resolve().parents[2] / 'deploy'
sys.path.insert(0, str(DEPLOY_DIR))
import update as worker  # noqa: E402


class PidAlive(unittest.TestCase):
    """_pid_alive 的判活语义（重点：Windows 上死 pid 不能假报存活）。"""

    def test_live_pid_reports_true(self) -> None:
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            self.assertTrue(updater._pid_alive(proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_dead_pid_reports_false_even_with_open_handle(self) -> None:
        """进程被杀后必须判死。

        Windows 分支刻意**不先 wait**：此时内核对象仍被父进程句柄持有，
        os.kill(pid,0) 版在这里 3/3 假报「存活」（事故根因——锁永不失效，
        前端永远「执行中」），新实现必须能判死。POSIX 分支同理：进程退出但
        未被回收时是 zombie，os.kill(pid,0) 同样静默成功，新实现靠 /proc 判死。
        """
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            proc.kill()
            # 不先 wait：POSIX 下此刻是 zombie，必须判死（旧实现会误报存活）
            for _ in range(50):
                if proc.poll() is not None:
                    break
                import time as _t
                _t.sleep(0.02)
            self.assertFalse(updater._pid_alive(proc.pid))
            proc.wait()
            self.assertFalse(updater._pid_alive(proc.pid))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_zombie_pid_reports_false_posix(self) -> None:
        """POSIX 僵尸进程（已退出、未被父进程 reap）必须判死。

        事故场景：更新子进程退出后父进程未及时 wait，update.lock 里记录的 pid
        变成 zombie。os.kill(pid, 0) 对 zombie 返回成功 → _lock_active() 永远为真
        → read_status() 把 running 强制拉回 true → 前端永久显示「正在更新」，
        而状态文件其实早已写入 running=false。
        """
        if os.name == 'nt':
            self.skipTest('POSIX 专用：/proc/<pid>/stat 的 Z 状态')
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            proc.terminate()
            # 等待进入 zombie：进程已退出但父进程尚未 wait
            deadline = __import__('time').time() + 5
            while __import__('time').time() < deadline:
                try:
                    with open(f'/proc/{proc.pid}/stat', 'rb') as f:
                        state = f.read().rsplit(b')', 1)[1].split()[0]
                except (OSError, IndexError):
                    break
                if state == b'Z':
                    break
                __import__('time').sleep(0.02)
            else:
                self.skipTest('进程未能进入 zombie 状态（环境不支持）')
            self.assertFalse(
                updater._pid_alive(proc.pid),
                'zombie 进程必须判死，否则 update.lock 永不失效',
            )
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def test_missing_pid_reports_false(self) -> None:
        """从未存在的 pid：Windows 上 OpenProcess 报 87，POSIX 上 ProcessLookupError。"""
        self.assertFalse(updater._pid_alive(4194304))

    def test_garbage_input_reports_false(self) -> None:
        for bad in (None, '', 'abc', 0, -5):
            self.assertFalse(updater._pid_alive(bad))


class InitialStatusClosedLoop(unittest.TestCase):
    """「子进程瞬间死亡」也能收敛出终止态：初始状态 + read_status 合成。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self._orig = {
            'STATUS_FILE': updater.STATUS_FILE,
            'LOCK_FILE': updater.LOCK_FILE,
            'current_version': updater.current_version,
            'in_container': updater.in_container,
            'can_control_docker': updater.can_control_docker,
        }
        updater.STATUS_FILE = self.dir / 'update-status.json'
        updater.LOCK_FILE = self.dir / 'update.lock'
        self._orig_data = config.DATA_DIR
        config.DATA_DIR = self.dir
        updater.current_version = lambda: 'v1.0.66'  # type: ignore[assignment]
        updater.in_container = lambda: False  # type: ignore[assignment]
        updater.can_control_docker = lambda: False  # type: ignore[assignment]

    def tearDown(self) -> None:
        for name, value in self._orig.items():
            setattr(updater, name, value)
        config.DATA_DIR = self._orig_data
        self._tmp.cleanup()

    def test_initial_status_fields(self) -> None:
        """初始状态字段与 Reporter 的口径一致（前端轮询契约）。"""
        updater._write_initial_status('manager', 424242)
        data = json.loads(updater.STATUS_FILE.read_text(encoding='utf-8'))
        self.assertTrue(data['running'])
        self.assertIsNone(data['ok'])
        self.assertEqual(data['pid'], 424242)
        self.assertEqual(data['target'], 'manager')
        self.assertEqual(data['step'], '已启动更新进程')
        self.assertEqual(data['logs'], [])

    def test_dead_child_converges_to_failure(self) -> None:
        """初始状态 + 死 pid → read_status 合成 ok=false（前端可停止轮询）。"""
        updater._write_initial_status('manager', 4194304)  # 必不存在的 pid
        status = updater.read_status()
        self.assertFalse(status['running'])
        self.assertIs(status['ok'], False)
        self.assertEqual(status['step'], '更新进程异常中断')

    def test_initial_status_is_compatible_with_update_landed(self) -> None:
        """初始状态没有 target_version、logs 为空 → _update_landed 不误判成功。"""
        updater._write_initial_status('manager', 4194304)
        data = json.loads(updater.STATUS_FILE.read_text(encoding='utf-8'))
        self.assertFalse(updater._update_landed(data))


class MainTerminalStatus(unittest.TestCase):
    """worker 的 main() 所有退出路径都必须落盘终态。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self._orig = {
            'DATA_DIR': worker.DATA_DIR,
            'STATUS_FILE': worker.STATUS_FILE,
            'update_upstream': worker.update_upstream,
            'update_manager': worker.update_manager,
        }
        worker.DATA_DIR = self.dir
        worker.STATUS_FILE = self.dir / 'update-status.json'
        self._argv = sys.argv
        sys.argv = ['update.py', '--target', 'manager']

    def tearDown(self) -> None:
        for name, value in self._orig.items():
            setattr(worker, name, value)
        sys.argv = self._argv
        self._tmp.cleanup()

    def _state(self) -> dict:
        return json.loads(worker.STATUS_FILE.read_text(encoding='utf-8'))

    def test_success_writes_ok_true(self) -> None:
        worker.update_manager = lambda rep: rep.finish(True)  # type: ignore[assignment]
        self.assertEqual(worker.main(), 0)
        state = self._state()
        self.assertFalse(state['running'])
        self.assertIs(state['ok'], True)

    def test_exception_writes_ok_false(self) -> None:
        def boom(rep):
            rep.step('更新管理端')
            raise RuntimeError('boom')

        worker.update_manager = boom  # type: ignore[assignment]
        self.assertEqual(worker.main(), 1)
        state = self._state()
        self.assertFalse(state['running'])
        self.assertIs(state['ok'], False)
        texts = [str(e.get('text')) for e in state.get('logs', [])]
        self.assertTrue(any('更新失败：boom' in t for t in texts))

    def test_keyboard_interrupt_writes_ok_false(self) -> None:
        """Ctrl+C（BaseException）不能穿出去留悬空状态——这正是本次事故的形态。"""

        def interrupted(rep):
            raise KeyboardInterrupt

        worker.update_manager = interrupted  # type: ignore[assignment]
        self.assertEqual(worker.main(), 1)  # 必须被捕获，不向外抛
        state = self._state()
        self.assertFalse(state['running'])
        self.assertIs(state['ok'], False)
        texts = [str(e.get('text')) for e in state.get('logs', [])]
        self.assertTrue(any('更新被中断' in t for t in texts))


class _SilentReporter:
    """download() 只用到 rep.log；用无副作用 stub 避免写真实状态文件。"""

    def log(self, message: str, level: str = 'info') -> None:
        pass


class _RetryServer:
    """本地 HTTP 服务：/flaky 第一次 500 之后 200，/down 恒 500，其余 404。

    用真实 HTTP 往返验证重试逻辑，不 mock 标准库（参考 no_mock_pollution 纪律）。
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                with outer._lock:
                    outer.counts[self.path] = outer.counts.get(self.path, 0) + 1
                    n = outer.counts[self.path]
                if self.path == '/flaky' and n == 1:
                    self.send_error(500)
                    return
                if self.path in ('/flaky', '/ok'):
                    body = b'x' * 101_000
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == '/down':
                    self.send_error(500)
                    return
                self.send_error(404)

            def log_message(self, *args) -> None:  # 静默访问日志
                pass

        self._httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f'http://127.0.0.1:{port}'

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class DownloadRetry(unittest.TestCase):
    """下载失败重试一次（网络抖动兜底）；4xx 是请求本身的问题，不重试。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.server = _RetryServer()
        self.server.start()
        self.rep = _SilentReporter()

    def tearDown(self) -> None:
        self.server.stop()
        self._tmp.cleanup()

    def test_retry_once_then_succeed(self) -> None:
        dest = pathlib.Path(self._tmp.name) / 'pkg.tar.gz'
        worker.download(f'{self.server.url}/flaky', dest, self.rep)  # type: ignore[arg-type]
        self.assertEqual(dest.stat().st_size, 101_000)
        self.assertEqual(self.server.counts.get('/flaky'), 2)

    def test_client_error_not_retried(self) -> None:
        dest = pathlib.Path(self._tmp.name) / 'pkg.tar.gz'
        with self.assertRaises(urllib.error.HTTPError):
            worker.download(f'{self.server.url}/gone', dest, self.rep)  # type: ignore[arg-type]
        self.assertEqual(self.server.counts.get('/gone'), 1)

    def test_gives_up_after_second_failure(self) -> None:
        dest = pathlib.Path(self._tmp.name) / 'pkg.tar.gz'
        with self.assertRaises(urllib.error.HTTPError):
            worker.download(f'{self.server.url}/down', dest, self.rep)  # type: ignore[arg-type]
        self.assertEqual(self.server.counts.get('/down'), 2)


class ChildStdioEncoding(unittest.TestCase):
    """更新子进程的 stdio 必须强制 UTF-8（中文 Windows 重定向 stdout 默认 cp936）。

    回归背景（2026-09-26 真实更新事故）：验签通过后在 ✓ 日志行崩溃——
    'gbk' codec can't encode '\\u2713' in position 11（position 11 =
    Reporter.log 时间戳前缀「[HH:MM:SS] 」的长度）。v1.0.66 起
    Reporter.log 走 print()，更新进程被摘掉控制台后，重定向 stdout 的
    ANSI 编码（cp936）编码不了 ✓/⚠️，一打印就死。
    """

    def test_spawner_forces_utf8_env(self) -> None:
        """start_update 传给更新子进程的环境必须显式声明 UTF-8。"""
        src = inspect.getsource(updater)
        self.assertIn("'PYTHONIOENCODING': 'utf-8'", src)
        self.assertIn("'PYTHONUTF8': '1'", src)

    def test_worker_self_reconfigures_stdio(self) -> None:
        """worker 被旧 spawner / 手工启动时也要自我防御。"""
        src = (DEPLOY_DIR / 'update.py').read_text(encoding='utf-8')
        self.assertIn("reconfigure(encoding='utf-8', errors='replace')", src)

    def test_utf8_env_makes_checkmark_printable(self) -> None:
        """功能验证：重定向 stdout + PYTHONIOENCODING=utf-8 下 ✓ 能正常写出。

        （不设该环境变量时，本用例在 cp936 宿主上会以 UnicodeEncodeError
        失败——那正是生产事故的复现路径，故只测正向，保证可移植。）
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / 'out.log'
            env = dict(os.environ)
            env['PYTHONIOENCODING'] = 'utf-8'
            with open(out, 'wb') as fh:
                proc = subprocess.run(
                    [sys.executable, '-c', "print('✓ 签名校验通过')"],
                    stdout=fh, stderr=subprocess.PIPE, env=env, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('✓', out.read_text(encoding='utf-8'))



class RestartStepOnWindows(unittest.TestCase):
    """Windows 原生部署：新代码就位后**不许**去调 systemctl，也不能报失败。

    现场（本批用户反馈的连带问题）：文件替换成功后，收尾去 `systemctl restart` ——
    Windows 上根本没有这个命令，于是整个更新被判失败（用户看到的是一次失败，
    实际新代码已经装好），而报错还叫他去查一个不存在的服务。

    这里用「注入一个会记录的假 run」来验：Windows 分支不许调用它，且不许抛错。
    """

    def _run_restart(self, *, in_container: bool, nt: bool) -> tuple[str, list]:
        calls: list[tuple] = []

        def fake_run(cmd, **kwargs):     # noqa: ANN001
            calls.append(tuple(cmd))
            return 0, ''

        class Rep:
            lines: list[str] = []

            def log(self, msg, level='info'):
                Rep.lines.append(str(msg))

        with mock.patch.object(worker, 'in_container', lambda: in_container), \
             mock.patch.object(worker, 'run', fake_run), \
             mock.patch.object(worker.os, 'name', 'nt' if nt else 'posix'):
            worker.restart_service(Rep())
        return ' '.join(Rep.lines), calls

    def test_windows_does_not_call_systemctl(self) -> None:
        text, calls = self._run_restart(in_container=False, nt=True)
        self.assertEqual(calls, [], 'Windows 上不该去调 systemctl（那里没有这个命令）')
        self.assertIn('重新执行启动脚本', text,
                      '没有告诉 Windows 用户「新代码已就位，重启面板即可」')

    def test_posix_still_restarts_via_systemd(self) -> None:
        text, calls = self._run_restart(in_container=False, nt=False)
        self.assertEqual(calls, [('systemctl', 'restart', worker.SERVICE_NAME)],
                         'POSIX 分支被改坏了：不再走 systemctl')
        self.assertIn('已重启', text)


class ClearStatusTest(unittest.TestCase):
    """清除上次更新的结果与日志（issue #105）。

    现场：一次失败的更新会长期驻留 —— 状态文件只在下次 start_update 时被删、
    update.log 只追加不截断，界面上那条「更新未完成」与日志永远擦不掉，用户只能
    进容器手删 /app/data/update-status.json 与 update.log。而失败记录恰恰是最想
    清掉的东西。这里锁住三件事：能清、运行中拒绝、把两个文件都清掉。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self._orig = {k: getattr(updater, k) for k in
                      ('STATUS_FILE', 'LOCK_FILE', 'LOG_FILE')}
        updater.STATUS_FILE = self.dir / 'update-status.json'
        updater.LOCK_FILE = self.dir / 'update.lock'
        updater.LOG_FILE = self.dir / 'update.log'

    def tearDown(self) -> None:
        for k, v in self._orig.items():
            setattr(updater, k, v)
        self._tmp.cleanup()

    def test_clears_both_status_and_log(self) -> None:
        updater.STATUS_FILE.write_text('{"running": false, "ok": false}', encoding='utf-8')
        updater.LOG_FILE.write_text('更新失败：The read operation timed out\n', encoding='utf-8')
        ok, msg = updater.clear_status()
        self.assertTrue(ok, msg)
        self.assertFalse(updater.STATUS_FILE.exists(), '状态文件没清掉 —— 界面还会显示失败')
        self.assertFalse(updater.LOG_FILE.exists(), 'update.log 没清掉 —— 日志区还会显示历史')
        # 清完再读：没有任何结果，界面因此不显示结果卡片与日志区
        st = updater.read_status()
        self.assertIsNone(st['ok'])
        self.assertFalse(st['running'])
        self.assertEqual(updater.tail_log(80), '')

    def test_refuses_while_an_update_is_running(self) -> None:
        """运行中清除会让前端把「正在更新」看丢 —— 必须拒绝。"""
        updater.LOCK_FILE.write_text(str(os.getpid()), encoding='utf-8')  # 活着的 pid
        updater.STATUS_FILE.write_text('{"running": true}', encoding='utf-8')
        ok, msg = updater.clear_status()
        self.assertFalse(ok, '更新进行中却允许清除')
        self.assertIn('进行中', msg)
        self.assertTrue(updater.STATUS_FILE.exists(), '被拒绝时不该动状态文件')

    def test_stale_lock_does_not_block_clearing(self) -> None:
        """崩溃遗留的死 pid 锁不该拦住清除（那正是最需要清的现场）。"""
        updater.LOCK_FILE.write_text('4194304', encoding='utf-8')  # 必不存在
        updater.STATUS_FILE.write_text('{"running": true, "ok": null}', encoding='utf-8')
        ok, msg = updater.clear_status()
        self.assertTrue(ok, msg)


class UpdateProxyTest(unittest.TestCase):
    """一键更新的下载代理（issue #106）。

    容器能连上 api.github.com（版本检测）却连不上 Release 资产所在的 github.com /
    objects.githubusercontent.com 是常见情形，而「检测成功」会把「下载不可达」
    掩盖成「更新器坏了」。这里验：WB_UPDATE_PROXY 真的被下载路径用上（用一个本地
    HTTP 代理收请求来验，不 mock 标准库），且本机地址会被 no_proxy 放行。
    """

    @staticmethod
    def _load(**env):
        import importlib.util
        keys = list(env)
        old = {k: os.environ.get(k) for k in keys}
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        try:
            spec = importlib.util.spec_from_file_location(
                'upd_proxy', str(pathlib.Path(__file__).resolve().parents[2] / 'deploy' / 'update.py'))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, {k: os.environ.get(k) for k in keys}
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_download_goes_through_the_configured_proxy(self) -> None:
        seen: list[str] = []

        class Proxy(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def do_GET(self):
                # 代理收到的是绝对 URL（http://host/path），不是路径
                seen.append(self.path)
                body = b'x' * 101_000
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            proxy_url = f'http://127.0.0.1:{srv.server_address[1]}'
            mod, _ = self._load(WB_UPDATE_PROXY=proxy_url)
            self.assertEqual(mod.UPDATE_PROXY, proxy_url)
            self.assertIn('127.0.0.1', os.environ.get('no_proxy', ''))
            dest = pathlib.Path(tempfile.mkdtemp()) / 'pkg.tar.gz'
            mod.download('http://release.invalid/workbuddy-manager-v9.tar.gz', dest,
                         _SilentReporter())
            self.assertEqual(dest.stat().st_size, 101_000, '下载没有成功经过代理')
            self.assertTrue(seen, '代理一个请求都没收到 —— 下载没走 WB_UPDATE_PROXY')
            self.assertIn('release.invalid', seen[0])
        finally:
            srv.shutdown()

    def test_wb_http_proxy_is_used_when_update_proxy_absent(self) -> None:
        """用户已经为面板配过出口代理（WB_HTTP_PROXY）时不必再配一遍。"""
        mod, _ = self._load(WB_UPDATE_PROXY=None, WB_HTTP_PROXY='http://panel-proxy:7890')
        self.assertEqual(mod.UPDATE_PROXY, 'http://panel-proxy:7890')


    def test_socks5_panel_proxy_is_not_handed_to_the_updater(self) -> None:
        """面板能吃 socks5，更新器（urllib）吃不了 —— 不能带着它去下载。

        带着它下载只会得到一条看不懂的报错；正确行为是忽略并记一条日志说清怎么办。
        """
        mod, _ = self._load(WB_UPDATE_PROXY=None,
                            WB_HTTP_PROXY='socks5://user:secret@127.0.0.1:1080')
        self.assertEqual(mod.UPDATE_PROXY, '',
                         'socks5 被当成 http 代理交给 urllib 了，下载必然失败')
        self.assertTrue(mod.PROXY_UNSUPPORTED)

    def test_explicit_socks_update_proxy_is_also_ignored(self) -> None:
        mod, _ = self._load(WB_UPDATE_PROXY='socks5://127.0.0.1:1080', WB_HTTP_PROXY=None)
        self.assertEqual(mod.UPDATE_PROXY, '')
        self.assertTrue(mod.PROXY_UNSUPPORTED)

    def test_http_proxy_needs_no_warning(self) -> None:
        mod, _ = self._load(WB_UPDATE_PROXY=None, WB_HTTP_PROXY='http://panel-proxy:7890')
        self.assertEqual(mod.PROXY_UNSUPPORTED, '',
                         'http 代理被误判成不支持 —— 会平白多一条警告日志')

    def test_network_failure_hint_mentions_the_proxy_knob(self) -> None:
        mod, _ = self._load(WB_UPDATE_PROXY=None, WB_HTTP_PROXY=None)
        hint = mod._download_hint(OSError('The read operation timed out'))
        self.assertIn('WB_UPDATE_PROXY', hint,
                      '下载失败时没有告诉用户「可能需要配代理」（issue #106 的第二个诉求）')
        # 与网络无关的失败不贴这条提示（避免误导）
        self.assertEqual(mod._download_hint(ValueError('bad url')), '')

if __name__ == '__main__':
    unittest.main()

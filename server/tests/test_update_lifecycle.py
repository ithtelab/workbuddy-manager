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
        前端永远「执行中」），新实现必须能判死。POSIX 分支 zombie 期
        os.kill(pid,0) 本就成功（语义正确），等 wait/reap 之后再判死。
        """
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            proc.kill()
            if os.name == 'nt':
                self.assertFalse(updater._pid_alive(proc.pid))
                proc.wait()
            else:
                proc.wait()
                self.assertFalse(updater._pid_alive(proc.pid))
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
            'UPSTREAM_REF_FILE': updater.UPSTREAM_REF_FILE,
            'current_version': updater.current_version,
            'in_container': updater.in_container,
            'can_control_docker': updater.can_control_docker,
        }
        updater.STATUS_FILE = self.dir / 'update-status.json'
        updater.LOCK_FILE = self.dir / 'update.lock'
        updater.UPSTREAM_REF_FILE = self.dir / 'upstream-ref.txt'
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


if __name__ == '__main__':
    unittest.main()

"""容器日志降噪：启动失败精简呈现 + access log 开关。

现场（issue #30 的日志导出）：数据库权限故障让服务起不来，Starlette 把完整
异常链刷了 350 行，其中 96 行是 `merged_lifespan` / `__aenter__` 之类的框架内部
帧，带修法的 RuntimeError 被埋在最后。这里锁住「修法原文不丢 + 框架栈不出现」
这两条，避免以后改动又把日志退化成人肉找针。
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, logsetup  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]


def _make_chain() -> BaseException:
    """造一条「外层带修法 + 内层根因」的异常链，形状与 db.connect 的真实故障一致。"""
    try:
        try:
            raise ValueError('unable to open database file')
        except ValueError as cause:
            raise RuntimeError('无法打开数据库文件：/app/data/manager.db\n'
                               '  原因：目录不可写：/app/data\n'
                               '  ① sudo chown -R 10001:10001 ./data') from cause
    except RuntimeError as exc:
        return exc


class FormatStartupFailureTest(unittest.TestCase):
    def test_keeps_full_fix_instructions_from_outer_exception(self) -> None:
        text = logsetup.format_startup_failure(_make_chain())
        self.assertIn('RuntimeError: 无法打开数据库文件：/app/data/manager.db', text)
        self.assertIn('sudo chown -R 10001:10001 ./data', text, '修法原文必须逐字保留')
        self.assertIn('ValueError: unable to open database file', text, '根因也要在')

    def test_headline_makes_failure_unmistakable(self) -> None:
        text = logsetup.format_startup_failure(_make_chain())
        self.assertIn('启动失败：服务无法继续运行。', text)

    def test_project_frames_kept_framework_frames_dropped(self) -> None:
        self.assertTrue(logsetup._is_project_frame('/app/server/db.py'), '容器内路径')
        self.assertTrue(logsetup._is_project_frame('C:\\repo\\server\\db.py'), 'Windows 路径')
        self.assertTrue(logsetup._is_project_frame('/app/server'), 'server 目录本身')
        for noisy in (
            '/usr/local/lib/python3.12/site-packages/starlette/routing.py',
            '/usr/local/lib/python3.12/contextlib.py',
            '/usr/local/lib/python3.12/site-packages/fastapi/routing.py',
        ):
            self.assertFalse(logsetup._is_project_frame(noisy), f'框架帧应被过滤：{noisy}')

    def test_stack_can_be_suppressed_without_losing_the_message(self) -> None:
        with_stack = logsetup.format_startup_failure(_make_chain())
        without_stack = logsetup.format_startup_failure(_make_chain(), include_stack=False)
        self.assertIn('出错位置', with_stack)
        self.assertNotIn('出错位置', without_stack)
        self.assertIn('sudo chown -R 10001:10001 ./data', without_stack,
                      '关掉栈也不能丢修法')

    def test_self_referencing_chain_does_not_loop(self) -> None:
        exc = RuntimeError('自己指自己')
        exc.__cause__ = exc
        text = logsetup.format_startup_failure(exc)
        self.assertEqual(text.count('自己指自己'), 1, '异常链要按 id 去重')

    def test_exception_without_message_still_has_type(self) -> None:
        text = logsetup.format_startup_failure(RuntimeError())
        self.assertIn('RuntimeError', text)


class AccessLogTest(unittest.TestCase):
    def test_default_disables_uvicorn_access_log(self) -> None:
        logger = logging.getLogger('uvicorn.access')
        original = logger.disabled
        try:
            with mock.patch.object(config, 'ACCESS_LOG', False):
                logsetup.configure_access_log()
            self.assertTrue(logger.disabled, '默认应静默逐请求 access log')
        finally:
            logger.disabled = original

    def test_opt_in_keeps_access_log_enabled(self) -> None:
        logger = logging.getLogger('uvicorn.access')
        original = logger.disabled
        try:
            logger.disabled = False
            with mock.patch.object(config, 'ACCESS_LOG', True):
                logsetup.configure_access_log()
            self.assertFalse(logger.disabled, 'WB_ACCESS_LOG=1 时不该动 access log')
        finally:
            logger.disabled = original


class ConfigTest(unittest.TestCase):
    def test_flags_have_expected_defaults_and_types(self) -> None:
        # 不 reload config：它持有 _CLIENTS 客户端池，reload 会清空池、污染同进程
        # 其它测试。这里只钉住默认值语义；_env_bool 的解析由 config 自己的测试覆盖。
        self.assertIsInstance(config.ACCESS_LOG, bool)
        self.assertIsInstance(config.STARTUP_FAILURE_STACK, bool)
        with mock.patch.dict('os.environ', {'WB_ACCESS_LOG': '1'}):
            self.assertTrue(config._env_bool('WB_ACCESS_LOG', False))
        with mock.patch.dict('os.environ', {'WB_STARTUP_FAILURE_STACK': '0'}):
            self.assertFalse(config._env_bool('WB_STARTUP_FAILURE_STACK', True))


class WiringTest(unittest.TestCase):
    def test_lifespan_delegates_startup_and_reports_failure(self) -> None:
        """防止以后有人把 lifespan 改回「异常裸露给框架刷栈」的写法。"""
        src = (_ROOT / 'server' / 'main.py').read_text(encoding='utf-8')
        self.assertIn('logsetup.configure_access_log()', src)
        self.assertIn('logsetup.report_startup_failure(exc)', src)
        self.assertIn('raise SystemExit(1) from None', src,
                      '必须让框架干净退出，否则仍会补一遍框架栈')


if __name__ == '__main__':
    unittest.main()

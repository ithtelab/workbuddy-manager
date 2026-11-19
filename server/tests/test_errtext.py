"""异常原因必须**说得出话**（issue #132）。

报障截图：账号页弹出一条「刷新异常: 」——冒号后面什么都没有。原因不是文案被截断，
而是异常本身的 `str()` 是空的（超时类异常很常见），`f'{exc}'` 就渲染成空串。
用户看到的是「出了个异常，但不知道是什么」，而我们屏幕上那条提示本来就是为了告诉他
原因。

这里钉两件事：
  1. `errtext.err_text` 对空文本异常也要给出人话（超时/取消各有说法，其余退化成类型名）；
  2. 服务端把异常拼进用户可见提示时，不许再裸插 `{exc}`（源码级守卫，防复发）。
"""
from __future__ import annotations

import asyncio
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import tencent  # noqa: E402
from server.services.errtext import err_text  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]


class ErrTextTest(unittest.TestCase):
    def test_空文本异常退化成类型名(self) -> None:
        class WeirdError(Exception):
            def __str__(self) -> str:      # noqa: D105
                return '   '

        self.assertEqual(err_text(WeirdError()), 'WeirdError')

    def test_空文本超时给人话(self) -> None:
        for exc in (TimeoutError(), asyncio.TimeoutError()):
            with self.subTest(exc=type(exc).__name__):
                self.assertIn('超时', err_text(exc))

    def test_取消也说清楚(self) -> None:
        self.assertIn('取消', err_text(asyncio.CancelledError()))

    def test_有文本时保留类型名与原文(self) -> None:
        self.assertEqual(err_text(ConnectionResetError('connection reset')),
                         'ConnectionResetError: connection reset')

    def test_永不返回空串(self) -> None:
        for exc in (TimeoutError(), Exception(), ValueError(''), KeyError()):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(err_text(exc).strip(), '返回了空串，提示会断在冒号上')


class RefreshErrorMessageTest(unittest.TestCase):
    """报障路径本身：token 刷新失败时的那句话。"""

    AUTH = {'uid': 'u1', 'access_token': 'AT', 'refresh_token': 'RT',
            'realm': 'cn', 'enterprise_id': ''}

    def _refresh_with(self, exc: BaseException):
        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                raise exc

        with mock.patch.object(tencent, '_account_http_client', lambda auth: _Client()):
            return asyncio.run(tencent.refresh_token(dict(self.AUTH)))

    def test_空文本超时不再只剩冒号(self) -> None:
        ok, msg, _ = self._refresh_with(TimeoutError())
        self.assertFalse(ok)
        self.assertIn('刷新异常', msg)
        self.assertNotRegex(msg, r'[:：]\s*$',
                            '提示断在冒号上 —— 用户看不到任何原因（issue #132 的截图）')
        self.assertIn('超时', msg)

    def test_普通异常照旧带原文(self) -> None:
        ok, msg, _ = self._refresh_with(ConnectionResetError('connection reset by peer'))
        self.assertFalse(ok)
        self.assertIn('connection reset by peer', msg)


class NoRawExcInUserFacingMessagesTest(unittest.TestCase):
    """源码级守卫：用户可见提示里不许再裸插 `{exc}`。

    这类提示会原样出现在 toast / 任务日志里，裸插空文本异常就是「刷新异常: 」
    那种断句。修法统一走 `err_text()`（它永远不会返回空串）。
    """

    # 这些文件里的「…异常/失败：{exc}」都会显示给用户
    FILES = ('tencent.py', 'renew.py', 'taskrun.py', 'pgsync.py',
             'modelcatalog.py', 'updater.py')

    def test_没有裸插_exc(self) -> None:
        pat = re.compile(r"f'[^']*[:：]\s*\{exc\}'")
        hits: list[str] = []
        for name in self.FILES:
            src = (_ROOT / 'server' / 'services' / name).read_text(encoding='utf-8')
            for m in pat.finditer(src):
                hits.append(f'{name}: {m.group(0)}')
        self.assertEqual(hits, [], '这些提示会断在冒号上：\n  ' + '\n  '.join(hits))

    def test_守卫不是空转(self) -> None:
        """把一处改回裸插，上面的正则必须能看出来。"""
        pat = re.compile(r"f'[^']*[:：]\s*\{exc\}'")
        self.assertIsNotNone(pat.search("return False, f'刷新异常: {exc}', {}"))
        # 正面样例：用 err_text 的不算违规
        self.assertIsNone(pat.search("return False, f'刷新异常: {err_text(exc)}', {}"))


if __name__ == '__main__':
    unittest.main()

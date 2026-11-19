"""网关在连不上上游时，报错要说清「往哪打的、有没有走代理」（issue #167）。

背景：两位用户报同一件事 —— 直接 curl 上游是通的，经面板调用所有模型一律 502。
原来的报错只有异常文本（`上游不可用: ConnectError: ...`），看不出两件最要紧的：

  · 面板这次打的是**哪个地址**（配置里那个，往往不是用户以为的那个）；
  · 面板→上游这一跳**有没有走出口代理**（上游地址若写成公网域名、又配了全局出口
    代理，这一跳就会走代理，代理绕不回用户自己那台机器 → 全 502；内网/本机地址
    是自动直连的）。

这两条信息决定了用户该去改什么，所以按「提示必须指向真正能改的地方」的原则钉住。
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.routers import gateway  # noqa: E402


class UpstreamHintTest(unittest.TestCase):
    def _hint(self, base_url: str, proxy: str = ''):
        with mock.patch.object(config, 'effective_http_proxy', return_value=proxy):
            return gateway._upstream_hint({'base_url': base_url})

    def test_names_the_upstream_address(self) -> None:
        hint = self._hint('http://127.0.0.1:7863')
        self.assertIn('http://127.0.0.1:7863', hint, '必须写出面板这次打的是哪个地址')

    def test_no_proxy_mentions_nothing_extra(self) -> None:
        hint = self._hint('http://up.example.com:7863')
        self.assertNotIn('代理', hint, '没配代理就不该提代理，免得把人带偏')

    def test_public_host_with_proxy_warns_the_hop_goes_through_it(self) -> None:
        """公网域名 + 全局出口代理 = 这一跳会走代理（issue #167 最可能的成因）。"""
        hint = self._hint('http://up.example.com:7863', 'socks5://user:pw@proxy.example:1080')
        self.assertIn('经出口代理', hint)
        self.assertIn('socks5://user:***@proxy.example:1080', hint, '代理凭据要脱敏')
        self.assertNotIn('pw@', hint, '代理密码不能出现在报错里')
        self.assertIn('502', hint)

    def test_internal_host_with_proxy_says_direct(self) -> None:
        for base in ('http://127.0.0.1:7863', 'http://workbuddy2api:7863'):
            with self.subTest(base=base):
                hint = self._hint(base, 'socks5://u:pw@proxy.example:1080')
                self.assertIn('直连', hint, '内网/本机地址是自动直连的')

    def test_empty_base_yields_nothing(self) -> None:
        self.assertEqual(self._hint(''), '')


if __name__ == '__main__':
    unittest.main()

"""出站客户端要**复用**，且不能跨事件循环复用（issue #144）。

报障：Windows 原生部署上切换页面要 1~3 秒，根因是每次出站都新建 httpx.AsyncClient
——在那台机器上「仅构造 + aclose」就要约 1 秒（加载系统证书库建 SSL 上下文），
而账号/状态/模型这些接口每次都要问上游几次。

这里钉住四件事：
  1. 同一个循环 + 同一组超时/代理 → **同一个客户端对象**（复用的全部意义）；
  2. 不同超时组合 → 各自一个（不能把 5 秒超时和 30 秒超时混成一个）；
  3. `async with` 退出时**不关闭**（否则等于每次还是新建）；
  4. 跨事件循环不复用（httpx 的连接池绑在创建它的循环上；同步路由里
     `asyncio.run` 另起循环那条路——见 modelcatalog.fetch_ids_blocking——
     拿到旧循环的客户端就会炸）。
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402


class HttpClientReuseTest(unittest.TestCase):
    def setUp(self) -> None:
        config._CLIENTS.clear()
        self.addCleanup(config._CLIENTS.clear)

    def test_same_config_borrows_the_same_client(self) -> None:
        async def scenario():
            async with config.http_client(10, connect=3) as first:
                pass
            async with config.http_client(10, connect=3) as second:
                pass
            return first, second

        first, second = asyncio.run(scenario())
        self.assertIs(first, second,
                      '两次取到的不是同一个客户端 —— 等于每次还是新建（issue #144）')

    def test_borrowed_client_stays_open(self) -> None:
        async def scenario():
            async with config.http_client(10, connect=3) as client:
                pass
            return client

        client = asyncio.run(scenario())
        self.assertFalse(client.is_closed,
                         '借用期间被关掉了：连接池每次都得重建，复用的意义就没了')

    def test_different_timeouts_get_different_clients(self) -> None:
        async def scenario():
            async with config.http_client(10, connect=3) as slow:
                pass
            async with config.http_client(5, connect=2) as quick:
                pass
            return slow, quick

        slow, quick = asyncio.run(scenario())
        self.assertIsNot(slow, quick,
                         '不同超时配置共用一个客户端会把二者的超时混在一起')
        self.assertEqual(slow.timeout.connect, 3)
        self.assertEqual(quick.timeout.connect, 2)

    def test_different_proxy_gets_its_own_client(self) -> None:
        async def scenario():
            async with config.http_client(10, connect=3, proxy='') as direct:
                pass
            async with config.http_client(10, connect=3, proxy='http://127.0.0.1:7890') as via:
                pass
            return direct, via

        direct, via = asyncio.run(scenario())
        self.assertIsNot(direct, via, '走代理由的账号不能复用直连的客户端')

    def test_never_shares_across_event_loops(self) -> None:
        """跨循环复用会炸（连接池绑在旧循环上）—— 必须各建一个。"""
        async def one():
            async with config.http_client(10, connect=3) as client:
                return client

        first = asyncio.run(one())
        second = asyncio.run(one())
        self.assertIsNot(first, second,
                         '第二个事件循环拿到了前一个循环的客户端 —— 这正是同步路由'
                         '另起循环时最危险的那种复用')
        self.assertFalse(second.is_closed, '第二个循环里的客户端必须能用')

    def test_no_new_client_construction_on_second_borrow(self) -> None:
        """计数式断言：第二次借用时**没有**再构造 httpx.AsyncClient。"""
        import httpx

        real_init = httpx.AsyncClient.__init__
        built: list[int] = []

        def counting_init(self, *a, **kw):  # noqa: ANN001
            built.append(1)
            return real_init(self, *a, **kw)

        async def scenario():
            for _ in range(3):
                async with config.http_client(30, connect=3) as client:
                    pass
            return client

        with mock.patch.object(httpx.AsyncClient, '__init__', counting_init):
            client = asyncio.run(scenario())
        self.assertEqual(len(built), 1,
                         f'三次借用构造了 {len(built)} 个客户端，应当只建一次')
        asyncio.run(_aclose(client))

    def test_close_clients_closes_and_clears(self) -> None:
        async def scenario():
            async with config.http_client(10, connect=3) as client:
                pass
            await config.close_clients()
            return client

        client = asyncio.run(scenario())
        self.assertTrue(client.is_closed, 'close_clients 之后仍开着')
        self.assertEqual(config._CLIENTS, {}, '池没清空')

    def test_outside_event_loop_is_not_cached(self) -> None:
        """同步上下文（没有正在跑的循环）不缓存：不留绑定不明的客户端。"""
        direct = config.http_client(10, connect=3)
        self.assertTrue(hasattr(direct, 'aclose'),
                        '同步上下文应直接返回可用的客户端（而不是借用包装）')
        self.assertEqual(config._CLIENTS, {}, '同步上下文不该往池里塞东西')
        asyncio.run(_aclose(direct))


    def test_closing_a_borrowed_client_does_not_kill_the_shared_one(self) -> None:
        """自己 `client = http_client(...)` 再 `aclose()` 的写法不能真的关掉共享客户端。

        网关 / Anthropic 兼容 / 测试台 / Responses 四处就是这种写法。共享之后，
        那里的 aclose 一旦真关，会把别人正在用的连接池一起关掉 —— 并发下就是
        「偶发请求失败」。借来的客户端只支持「归还」。
        """

        async def scenario():
            async with config.http_client(10, connect=3) as first:
                pass
            borrowed = config.http_client(10, connect=3)   # 不用 async with 的写法
            await borrowed.aclose()                        # 旧写法里的 finally 分支
            async with config.http_client(10, connect=3) as again:
                pass
            return first, again

        first, again = asyncio.run(scenario())
        self.assertFalse(first.is_closed, '借来的客户端被 aclose 真关掉了')
        self.assertIs(first, again, '关过一次之后没从池里复用 —— 退化成每次新建')

    def test_borrowed_wrapper_forwards_client_calls(self) -> None:
        """包装要能当客户端用（post/stream/headers…），否则不用 async with 的写法会崩。"""
        async def scenario():
            borrowed = config.http_client(10, connect=3)
            return borrowed, borrowed.timeout, borrowed.headers

        borrowed, timeout, headers = asyncio.run(scenario())
        self.assertTrue(hasattr(borrowed, 'post') and hasattr(borrowed, 'stream'),
                        '属性转发没生效，`client.post(...)` 会 AttributeError')
        self.assertEqual(timeout.connect, 3)
        self.assertIn('user-agent', {k.lower() for k in headers})
        asyncio.run(config.close_clients())


async def _aclose(client) -> None:  # noqa: ANN001
    await client.aclose()




class ProxySchemeSupportTest(unittest.TestCase):
    """代理线路支持哪些协议：http / https / socks5（socks5 靠 socksio）。

    为什么单独钉：socks5 支持**完全依赖一个可选依赖** —— httpx 没装 socksio 时，
    **构造客户端就抛** `ImportError: Using SOCKS proxy, but the 'socksio' package is
    not installed`。而住宅代理服务商给凭据时常常直接给 socks5://（实测 ProxyLane
    就是），用户照抄就撞上这句「看起来像自己少装了东西」的报错。

    所以这里两条一起钉：依赖必须声明在 requirements 里，且三种协议都能构造出客户端。
    （只构造不发请求 —— 能不能连通由代理服务商决定，不是本用例的事。）
    """

    def setUp(self) -> None:
        config._CLIENTS.clear()
        self.addCleanup(config._CLIENTS.clear)

    def test_socksio_is_declared_in_requirements(self) -> None:
        req = (Path(__file__).resolve().parents[2]
               / 'server' / 'requirements.txt').read_text(encoding='utf-8')
        self.assertIn('socksio', req,
                      'requirements 里没有 socksio —— 填 socks5:// 的用户会看到 '
                      '「socksio 未安装」而不是「代理不通」')

    def test_all_three_schemes_are_accepted(self) -> None:
        async def scenario():
            out = {}
            for scheme in ('http://user:pass@127.0.0.1:7890',
                           'https://user:pass@127.0.0.1:7890',
                           'socks5://user:pass@127.0.0.1:1080'):
                async with config.http_client(10, connect=3, proxy=scheme) as client:
                    out[scheme.split('://', 1)[0]] = client
            return out

        clients = asyncio.run(scenario())
        self.assertEqual(sorted(clients), ['http', 'https', 'socks5'])
        for scheme, client in clients.items():
            with self.subTest(scheme=scheme):
                self.assertFalse(client.is_closed)
        # 三种协议各自一个客户端：连接池不能混用不同代理
        self.assertEqual(len({id(c) for c in clients.values()}), 3)


class InternalHostBypassTest(unittest.TestCase):
    """配了出口代理时，内网 / 本机目标必须**直连**。

    为什么要有：WB_HTTP_PROXY 此前作用于**全部**出站请求，包括「面板 → 上游」那一跳。
    上游在容器内网（`workbuddy2api` / `host.docker.internal`）或本机（`127.0.0.1:7863`）
    时，把这一跳交给外部代理必然连不上 —— 用户按说明配好代理，结果面板读不到上游状态、
    账号页全空，报错还看不出原因（`.env.example` 里那句警告只能靠用户自己躲）。
    """

    def setUp(self) -> None:
        config._CLIENTS.clear()
        self.addCleanup(config._CLIENTS.clear)

    def test_internal_targets_are_recognized(self) -> None:
        for host in ('127.0.0.1', 'localhost', '::1', 'host.docker.internal',
                     'workbuddy2api', 'myhost.lan', '10.1.2.3', '172.16.0.9',
                     '192.168.1.5', '169.254.0.1'):
            with self.subTest(host=host):
                self.assertTrue(config._is_internal_host(host),
                                f'{host} 没被当成内网目标 —— 这一跳会被交给代理')

    def test_public_targets_are_not_internal(self) -> None:
        for host in ('8.8.8.8', '93.184.216.34', 'api.tencent.com', '172.32.0.1',
                     'proxy.example:7890', '', None):
            with self.subTest(host=host):
                self.assertFalse(config._is_internal_host(host),
                                 f'{host} 被误判成内网目标 —— 配了代理却不走代理了')

    def test_internal_target_does_not_touch_the_proxy(self) -> None:
        """真发两次请求，看**谁被连上了**：内网目标连目标，公网目标连代理。

        异常文本靠不住（httpx 会把它包装成「All connection attempts failed」），
        所以起两个本地监听口当靶子：一个是「代理」，一个是「内网目标」。
        路由正确时 —— 内网那次只敲目标口，公网那次只敲代理口。
        """
        hits = {'proxy': 0, 'target': 0}
        servers: dict[str, socket.socket] = {}
        for name in ('proxy', 'target'):
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(('127.0.0.1', 0))
            srv.listen(5)
            servers[name] = srv

        def serve(name: str) -> None:
            while True:
                try:
                    conn, _ = servers[name].accept()
                except OSError:
                    return
                hits[name] += 1
                try:
                    conn.close()
                except OSError:
                    pass

        threads = [threading.Thread(target=serve, args=(n,), daemon=True) for n in servers]
        for t in threads:
            t.start()
        proxy_port = servers['proxy'].getsockname()[1]
        target_port = servers['target'].getsockname()[1]

        async def scenario():
            async with config.http_client(
                    3, proxy=f'http://127.0.0.1:{proxy_port}') as client:
                for url in (f'http://127.0.0.1:{target_port}/x',   # 内网 → 直连
                            'http://93.184.216.34/x'):             # 公网 → 走代理
                    try:
                        await client.get(url)
                    except Exception:  # noqa: BLE001 —— 靶子会立刻关连接，报错是预期的
                        pass

        try:
            asyncio.run(scenario())
        finally:
            for srv in servers.values():
                srv.close()
            time.sleep(0.2)

        self.assertGreaterEqual(hits['target'], 1,
                                '内网目标没被直连 —— 请求跑代理去了（容器部署里就是读不到上游）')
        self.assertGreaterEqual(hits['proxy'], 1,
                                '公网目标没走代理 —— 配了 WB_HTTP_PROXY 却被绕过')
        self.assertNotIn('proxy', [k for k, v in hits.items() if v == 0],
                         f'两个靶子的命中数：{hits}')

if __name__ == '__main__':
    unittest.main()

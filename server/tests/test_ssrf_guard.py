"""SSRF 防护：连通性测试不得把内网/元数据当成可探测目标。

**漏的是什么**：「设置 → Redis 连通性测试」把用户给的地址拿去**服务端发请求**，
再把响应片段回显。地址归一化只抽 host、不判安全性，于是 Upstash REST 那条路可以
指向：

    http://127.0.0.1:7863          → 本机上游客关（探测内网服务）
    169.254.169.254                → 云元数据（常能拿到实例临时凭证）
    metadata.tencentyun.com        → 腾讯云元数据
    10.0.0.5 / 192.168.x.x         → 内网其它主机

**为什么值得修**：它需要管理员权限，看起来"不是洞"。但管理员权限往往是通过
别的漏洞拿到的（本仓库历史上就发生过路径穿越 + 提权后门导致真实入侵），
而「拿到低权限后顺着内网横向移动」正是入侵的第二步。纵深防御应当在能拦的
地方就拦。

**修法**：不做域名白名单（会误伤自建/第三方 Upstash 兼容服务），而是
**排除回环、私有、链路本地、保留段与已知元数据域名**；公网主机照常放行。

**一处有意的例外（issue #125）**：`redis://` / `rediss://`（自建 Redis）走的是
**RESP `PING`**，不是这条 REST 路径 —— 自建 Redis 在私网、甚至回环（宿主机原生
部署就是 `127.0.0.1:6379`）都是正常形态，套用「只放行公网」会把按钮变成必现
误报（上游连得上、面板说不允许）。因此那一支只拒元数据主机名，见下方
`SelfHostedRedisProbeTest`；REST 分支的判定原样保留。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import wb2api  # noqa: E402


class InternalHostRejectionTest(unittest.TestCase):
    """地址判定：内网/元数据一律拒绝，公网一律放行。"""

    BLOCKED = (
        '127.0.0.1', '127.1.2.3', '10.0.0.5', '192.168.1.1', '172.16.0.1',
        '169.254.169.254',          # 云元数据（AWS/腾讯云等通用）
        '0.0.0.0', '::1', 'fc00::1', 'fe80::1',
        'metadata.tencentyun.com',  # 腾讯云内网元数据
        'metadata.google.internal',
        'something.internal', 'db.local',
    )
    ALLOWED = (
        'us1-xxx.upstash.io', 'upstash.io', 'my-redis.example.com',
        '1.2.3.4', '2606:4700::1111',   # 公网 IP（IPv6 也要放行）
    )

    def test_blocked_hosts(self) -> None:
        for h in self.BLOCKED:
            self.assertIsNotNone(
                wb2api._reject_internal_host(h),
                f'{h} 应被拒绝 —— 它能被用来探测内网或云元数据',
            )

    def test_allowed_hosts(self) -> None:
        for h in self.ALLOWED:
            self.assertIsNone(
                wb2api._reject_internal_host(h),
                f'{h} 是合法目标，不应被拦（否则自建/第三方 Redis 会被误伤）',
            )

    def test_empty_host_rejected(self) -> None:
        self.assertIsNotNone(wb2api._reject_internal_host(''))
        self.assertIsNotNone(wb2api._reject_internal_host('   '))


class TestUpstashEndpointTest(unittest.TestCase):
    """端到端：test_upstash 对内网地址必须在**发请求之前**就返回失败。

    关键断言是「没有发出任何 HTTP 请求」——只检查返回值不够：返回失败但请求
    已经打出去了，SSRF 已经成立（对方服务已经收到探测）。
    """

    def _call(self, url: str):
        sent: list[str] = []

        class _Client:
            def __init__(self, *a, **k) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, u, **kw):
                sent.append(u)
                raise AssertionError('不应发出请求')

        with mock.patch.object(config, 'http_client', _Client):
            ok, msg = asyncio.run(wb2api.test_upstash(url, 'tok'))
        return ok, msg, sent

    def test_internal_targets_never_requested(self) -> None:
        # `redis://` 不在这张表里（issue #125）：它走 RESP 分支，内网/回环是自建
        # Redis 的正常形态 → 那一支的断言在 SelfHostedRedisProbeTest。
        for url in ('http://127.0.0.1:7863/ping', '169.254.169.254',
                    'http://metadata.tencentyun.com'):
            ok, msg, sent = self._call(url)
            self.assertFalse(ok, f'{url} 应被拒绝')
            self.assertEqual(sent, [], f'{url} 竟然发出了请求：{sent}')
            self.assertIn('不允许探测', msg)

    def test_public_target_is_actually_probed(self) -> None:
        """公网地址要照常探测 —— 修复不能把正常功能一起关掉。"""
        sent: list[str] = []

        class _Resp:
            status_code = 200
            text = 'PONG'

        class _Client:
            def __init__(self, *a, **k) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, u, **kw):
                sent.append(u)
                return _Resp()

        with mock.patch.object(config, 'http_client', _Client):
            ok, msg = asyncio.run(wb2api.test_upstash('https://us1-abc.upstash.io', 'tok'))
        self.assertTrue(ok, msg)
        self.assertEqual(len(sent), 1, '公网地址应正常探测')
        self.assertIn('upstash.io/ping', sent[0])


class _RespStub:
    """最小 RESP 服务端：认 AUTH 与 PING，其余返回错误。

    `require_auth` 打开时先要求 AUTH；`record` 收集收到的完整命令，便于断言
    「我们真的按 RESP 发了什么」——只断言返回值会漏掉「发错命令但也有个像样的
    返回」这种假通过。
    """

    def __init__(self, *, require_auth: bool = False,
                 username: str = '', password: str = '',
                 hang: bool = False) -> None:
        self.require_auth = require_auth
        self.username = username
        self.password = password
        self.hang = hang
        self.record: list[list[str]] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self._authed = False

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                if self.hang:
                    await asyncio.sleep(3600)      # 只接受连接、永不回应
                line = await reader.readline()
                if not line:
                    return
                if not line.startswith(b'*'):      # 内联命令（我们不该发这种）
                    self.record.append([line.decode('utf-8', 'replace').strip()])
                    writer.write(b'-ERR expected array\r\n')
                    await writer.drain()
                    continue
                count = int(line[1:].strip())
                args: list[str] = []
                for _ in range(count):
                    await reader.readline()        # $len
                    args.append((await reader.readline()).decode('utf-8').rstrip('\r\n'))
                self.record.append(args)
                cmd = args[0].upper()
                if cmd == 'AUTH':
                    ok = (not self.username and args[1:] == [self.password]) \
                        or (self.username and args[1:] == [self.username, self.password])
                    self._authed = ok
                    writer.write(b'+OK\r\n' if ok else
                                 b'-WRONGPASS invalid username-password pair\r\n')
                elif cmd == 'PING':
                    if self.require_auth and not self._authed:
                        writer.write(b'-NOAUTH Authentication required.\r\n')
                    else:
                        writer.write(b'+PONG\r\n')
                else:
                    writer.write(b'-ERR unknown command\r\n')
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        if self.server is not None:
            self.server.close()
        return False

    @property
    def url(self) -> str:
        return f'redis://127.0.0.1:{self.port}'


class SelfHostedRedisProbeTest(unittest.TestCase):
    """自建 Redis 的连通性测试要真发 RESP PING（issue #125）。

    报障：填 `redis://<内网IP>:9736`（上游用得好好的）点「测试连接」必报
    「不允许探测的内部地址」——旧实现把它当 Upstash REST，先归一化成
    `https://<host>` 丢掉端口，再按私网拒掉。现在这一支走 RESP，且不套「只放行
    公网」的判定。
    """

    def _probe(self, url: str):
        return asyncio.run(wb2api.test_upstash(url))

    def test_loopback_自建_redis_能通过(self) -> None:
        """报障场景：回环/私网上的自建 Redis 必须真连、真 PING。"""
        async def run():
            async with _RespStub() as stub:
                ok, msg = await wb2api.test_upstash(stub.url)
                return ok, msg, stub.record
        ok, msg, record = asyncio.run(run())
        self.assertTrue(ok, msg)
        self.assertIn('PONG', msg)
        self.assertEqual([r[0].upper() for r in record], ['PING'],
                         f'应只发一条 RESP PING，实际：{record}')

    def test_私网地址不再被拦而是如实报连接结果(self) -> None:
        """不可达的私网地址：消息里不能再出现「不允许探测」。"""
        ok, msg = self._probe('redis://10.255.255.1:6379')
        self.assertFalse(ok)
        self.assertNotIn('不允许探测', msg, '私网自建 Redis 不该再被判定为非法目标')
        self.assertIn('无法连接', msg)

    def test_密码走_AUTH_且错误密码如实报(self) -> None:
        async def run():
            async with _RespStub(require_auth=True, password='s3cret') as stub:
                good = await wb2api.test_upstash(f'redis://:{stub.password}@{stub.url.split("//")[1]}')
                bad = await wb2api.test_upstash(f'redis://:wrong@{stub.url.split("//")[1]}')
                return good, bad, stub.record
        good, bad, record = asyncio.run(run())
        self.assertTrue(good[0], good[1])
        self.assertFalse(bad[0])
        self.assertIn('认证失败', bad[1])
        self.assertEqual(record[0][0].upper(), 'AUTH')
        self.assertEqual(record[0][1:], ['s3cret'], '无用户名时用两参数 AUTH（老版本 Redis 也认）')

    def test_带用户名时用三参数_AUTH(self) -> None:
        async def run():
            async with _RespStub(require_auth=True, username='wb', password='pw') as stub:
                ok, msg = await wb2api.test_upstash(
                    f'redis://wb:pw@127.0.0.1:{stub.port}')
                return ok, msg, stub.record
        ok, msg, record = asyncio.run(run())
        self.assertTrue(ok, msg)
        self.assertEqual(record[0], ['AUTH', 'wb', 'pw'], 'Redis 6+ 的 ACL 写法')

    def test_元数据主机名仍然拒绝(self) -> None:
        for host in ('metadata.tencentyun.com', 'metadata.google.internal'):
            ok, msg = self._probe(f'redis://{host}:6379')
            self.assertFalse(ok, host)
            self.assertIn('不允许探测', msg)

    def test_不是_redis_的回应要如实报错(self) -> None:
        """对端不说 RESP（比如连到了 HTTP 服务）：不能报「连接正常」。"""
        async def run():
            async with _RespStub(hang=True) as stub:
                return await wb2api.test_upstash(stub.url)
        ok, msg = asyncio.run(run())
        self.assertFalse(ok, '对端不应答却报成功，等于又一条假反馈')
        self.assertIn('无法连接', msg)

    def test_地址解析(self) -> None:
        cases = {
            'redis://h:6380': ('h', 6380, '', ''),
            'redis://h': ('h', 6379, '', ''),
            'redis://:pw@h:6379': ('h', 6379, '', 'pw'),
            'redis://u:pw@h:6379': ('h', 6379, 'u', 'pw'),
            'redis://u@h:6379': ('h', 6379, 'u', ''),
            'rediss://h:6380/2': ('h', 6380, '', ''),
        }
        for url, (host, port, user, password) in cases.items():
            with self.subTest(url=url):
                got = wb2api._redis_target(url)
                self.assertIsNotNone(got, url)
                self.assertEqual((got['host'], got['port'], got['username'],
                                  got['password']), (host, port, user, password))
        for url in ('https://x.upstash.io', 'x.upstash.io', '', 'redis:/bad'):
            with self.subTest(url=url):
                self.assertIsNone(wb2api._redis_target(url),
                                  '非 redis/rediss 不该走 RESP 分支')


class DbPermissionTest(unittest.TestCase):
    """数据库文件必须收紧到 0600（含 WAL/SHM 伴生文件）。

    库里存着 **API 密钥哈希与前缀、全部请求日志（来源 IP / UA）、审计日志**。
    SQLite 默认建出的文件是 0644，同主机的其他用户/进程可读——密钥前缀能用
    于针对性爆破，日志暴露调用方与内部拓扑。WAL 模式的伴生文件同样含数据。

    在 Windows 上 chmod 只影响只读位、无法验证实际模式，因此这里断言
    「以 0600 调用了 chmod，且三个文件都被覆盖」——那是 Linux 上生效的依据。
    """

    def test_chmod_0600_called_for_all_db_files(self) -> None:
        import os as _os
        import tempfile
        from unittest import mock
        from pathlib import Path as _P

        tmp = tempfile.mkdtemp()
        from server import config as _cfg
        old_dir, old_db = _cfg.DATA_DIR, _cfg.DB_PATH
        _cfg.DATA_DIR = _P(tmp)
        _cfg.DB_PATH = _P(tmp) / 'perm.db'
        from server import db as _db
        old_conn = _db._conn
        _db._conn = None
        try:
            calls: list[tuple[str, int]] = []
            real_chmod = _os.chmod

            def spy(path, mode):
                calls.append((str(path), mode))
                return real_chmod(path, mode)

            with mock.patch.object(_os, 'chmod', spy):
                _db.connect()
        finally:
            _db._conn = old_conn
            _cfg.DATA_DIR, _cfg.DB_PATH = old_dir, old_db

        modes = {m for _, m in calls}
        self.assertTrue(calls, '建库时没有收紧权限')
        self.assertEqual(modes, {0o600}, f'权限值应为 0600，实际 {[oct(m) for m in modes]}')
        names = ' '.join(p for p, _ in calls)
        for suffix in ('-wal', '-shm'):
            self.assertIn(suffix, names, f'{suffix} 伴生文件没有被收紧')


if __name__ == '__main__':
    unittest.main()

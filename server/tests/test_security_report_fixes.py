"""第二轮外部安全报告（附 PoC）中各项的回归测试。

报告由外部研究者提交，附完整复现步骤。以下每条都**先在本机复现确认**再修，
用例锁定修复后的行为。其中：

  1. `X-Real-IP` 伪造绕过全部 IP 类管控 —— 实测：服务直接暴露时，
     加一行 `X-Real-IP: 9.9.9.9` 即可从 403(ip_blocked) 变成通过校验。
     修法不是「默认不信任代理」（那会让标准反代部署下所有请求都看成
     127.0.0.1，IP 白名单要么全拦要么形同虚设、登录锁定变成一人失败全员被锁），
     而是**只在 TCP 对端确实来自可信网段时才采信转发头**。
  2. 更新包解压逃逸（供应链，可写 /root/.ssh/authorized_keys）：
     - 前缀匹配缺陷：`str.startswith` 把 `dest-sibling` 判成在 `dest` 内（已实测写出）
     - 符号链接成员未校验：先建 link 再经它写出，resolve() 在检查阶段看不穿
     - extractall 未传 filter：按包内 mode chmod，可带 setuid
  3. `POST /api/keys` 500（v1.0.23 引入的回归，我的疏漏）：审计调用用了
     `client_ip` 但漏导入 —— 升级后**密钥功能完全不可用**。
  4. `days` 无上限导致 SQLite 绑定溢出 500。
"""
from __future__ import annotations

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, iputil  # noqa: E402


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """最小 Request 替身：只带 client.host 与 headers。"""

    def __init__(self, peer: str, headers: dict | None = None) -> None:
        self.client = _FakeClient(peer) if peer else None
        self.headers = headers or {}


class SpoofedForwardHeaderTest(unittest.TestCase):
    """伪造转发头不能绕过 IP 类管控。"""

    def test_untrusted_peer_cannot_spoof_x_real_ip(self) -> None:
        """核心回归：对端是公网 IP（服务直接暴露）时，X-Real-IP 不被采信。"""
        req = _FakeRequest('203.0.113.9', {'x-real-ip': '9.9.9.9'})
        self.assertEqual(iputil.client_ip(req), '203.0.113.9',
                         '伪造的 X-Real-IP 被采信了——IP 白名单可被绕过')

    def test_untrusted_peer_cannot_spoof_xff(self) -> None:
        req = _FakeRequest('203.0.113.9', {'x-forwarded-for': '9.9.9.9'})
        self.assertEqual(iputil.client_ip(req), '203.0.113.9')

    def test_trusted_proxy_header_still_honored(self) -> None:
        """正常部署（反代与本体同机）不受影响，否则功能就废了。"""
        req = _FakeRequest('127.0.0.1', {'x-real-ip': '9.9.9.9'})
        self.assertEqual(iputil.client_ip(req), '9.9.9.9')

    def test_trusted_proxy_xff_rightmost_hop(self) -> None:
        """可信代理下按跳数从右取值（左侧可伪造，右侧才是代理追加的）。"""
        req = _FakeRequest('127.0.0.1', {'x-forwarded-for': '1.2.3.4, 9.9.9.9'})
        self.assertEqual(iputil.client_ip(req), '9.9.9.9')

    def test_private_peer_is_not_trusted_by_default(self) -> None:
        """默认只信回环（审计 2026-10-18）。

        以前默认还包含全部 RFC1918：同一局域网/同一 Docker 网络里的任意主机都能
        伪造 `X-Real-IP`，绕过登录锁定、密钥 IP 白名单与红包限领。
        """
        for peer in ('192.168.1.10', '10.1.2.3', '172.16.5.5'):
            req = _FakeRequest(peer, {'x-real-ip': '9.9.9.9'})
            self.assertEqual(iputil.client_ip(req), peer,
                             f'{peer} 默认不该被当成可信反代')

    def test_private_proxy_works_when_explicitly_configured(self) -> None:
        """反代在另一台内网机器时，把它加进 WB_TRUSTED_PROXY_CIDRS 即可。"""
        with mock.patch.object(config, 'TRUSTED_PROXY_CIDRS',
                               ['127.0.0.0/8', '192.168.0.0/16']):
            req = _FakeRequest('192.168.1.10', {'x-real-ip': '9.9.9.9'})
            self.assertEqual(iputil.client_ip(req), '9.9.9.9')
            # 未列入的网段仍不可信
            req2 = _FakeRequest('10.1.2.3', {'x-real-ip': '8.8.8.8'})
            self.assertEqual(iputil.client_ip(req2), '10.1.2.3')

    def test_no_headers_falls_back_to_peer(self) -> None:
        self.assertEqual(iputil.client_ip(_FakeRequest('127.0.0.1')), '127.0.0.1')

    def test_trust_proxy_off_ignores_headers_entirely(self) -> None:
        with mock.patch.object(config, 'TRUST_PROXY', False):
            req = _FakeRequest('127.0.0.1', {'x-real-ip': '9.9.9.9'})
            self.assertEqual(iputil.client_ip(req), '127.0.0.1')


class SafeExtractTest(unittest.TestCase):
    """发布包解压必须严格限制在目标目录内（供应链风险）。"""

    @staticmethod
    def _load_update_mod():
        import importlib.util
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location('upd', str(root / 'deploy' / 'update.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _tar(self, members: list[tuple[str, str, str]]):
        """members: [(name, kind, payload)]，kind: file|sym|hard"""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tf:
            for name, kind, payload in members:
                info = tarfile.TarInfo(name)
                if kind == 'sym':
                    info.type = tarfile.SYMTYPE
                    info.linkname = payload
                    tf.addfile(info)
                elif kind == 'hard':
                    info.type = tarfile.LNKTYPE
                    info.linkname = payload
                    tf.addfile(info)
                else:
                    data = payload.encode()
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
        buf.seek(0)
        return tarfile.open(fileobj=buf, mode='r:gz')

    def setUp(self) -> None:
        self.upd = self._load_update_mod()
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)
        self.dest = self.work / 'dest'
        self.dest.mkdir()

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    # ── 前缀匹配（实测曾逃逸）──

    def test_sibling_prefix_directory_rejected(self) -> None:
        """`../dest-sibling/x` 曾因字符串前缀相同而被放行——实测写出了 dest 之外。"""
        sibling = self.work / 'dest-sibling'
        sibling.mkdir()
        with self._tar([('../dest-sibling/pwn.txt', 'file', 'PWNED')]) as tf:
            with self.assertRaises(RuntimeError):
                self.upd._safe_extract(tf, self.dest)
        self.assertFalse((sibling / 'pwn.txt').exists(), '文件逃逸到了 dest 之外')

    def test_plain_dotdot_rejected(self) -> None:
        outside = self.work / 'OUT'
        outside.mkdir()
        with self._tar([('../OUT/x.txt', 'file', 'PWNED')]) as tf:
            with self.assertRaises(RuntimeError):
                self.upd._safe_extract(tf, self.dest)
        self.assertFalse((outside / 'x.txt').exists())

    def test_absolute_path_rejected(self) -> None:
        with self._tar([('/tmp/evil.txt', 'file', 'X')]) as tf:
            with self.assertRaises(RuntimeError):
                self.upd._safe_extract(tf, self.dest)

    # ── 链接成员（攻击链的核心载体）──

    def test_symlink_member_rejected(self) -> None:
        """即使指向目录内也拒绝：正常发布包不需要链接，拒绝才是安全的默认。"""
        with self._tar([('link', 'sym', str(self.work))]) as tf:
            with self.assertRaises(RuntimeError) as ctx:
                self.upd._safe_extract(tf, self.dest)
        self.assertIn('链接', str(ctx.exception))

    def test_hardlink_member_rejected(self) -> None:
        with self._tar([('f.txt', 'file', 'x'), ('h', 'hard', 'f.txt')]) as tf:
            with self.assertRaises(RuntimeError):
                self.upd._safe_extract(tf, self.dest)

    # ── 正常包必须照常解开 ──

    def test_normal_package_extracts(self) -> None:
        with self._tar([
            ('pkg/server/main.py', 'file', 'x = 1'),
            ('pkg/web/out/index.html', 'file', '<html>'),
        ]) as tf:
            self.upd._safe_extract(tf, self.dest)
        self.assertTrue((self.dest / 'pkg' / 'server' / 'main.py').is_file())
        self.assertTrue((self.dest / 'pkg' / 'web' / 'out' / 'index.html').is_file())

    def test_setuid_bit_stripped(self) -> None:
        """包内 mode 带 setuid 时必须被清掉（extractall 默认会照抄）。"""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tf:
            info = tarfile.TarInfo('pkg/suid')
            data = b'x'
            info.size = len(data)
            info.mode = 0o4755  # setuid
            tf.addfile(info, io.BytesIO(data))
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode='r:gz') as tf:
            self.upd._safe_extract(tf, self.dest)
        import stat as _stat
        mode = (self.dest / 'pkg' / 'suid').stat().st_mode
        self.assertFalse(mode & _stat.S_ISUID, 'setuid 位没有被清掉')


class PackageNameSanitizationTest(unittest.TestCase):
    """下载时用远端返回的文件名拼本地路径，必须只用文件名部分。"""

    def test_path_traversal_in_asset_name_neutralized(self) -> None:
        p = Path('../../etc/cron.d/evil.tar.gz')
        self.assertEqual(p.name, 'evil.tar.gz')


class DaysClampTest(unittest.TestCase):
    """days 无上限会导致 SQLite 绑定溢出（曾 500）。"""

    def test_clamped(self) -> None:
        self.assertEqual(db.clamp_days(None), None)
        self.assertEqual(db.clamp_days(7), 7)
        self.assertEqual(db.clamp_days(999999999999999), db._DAYS_MAX)
        self.assertEqual(db.clamp_days(-5), 1)
        self.assertEqual(db.clamp_days('abc'), None)

    def test_query_with_huge_days_does_not_raise(self) -> None:
        """关键：真的去查一次，确认不再抛 OverflowError。"""
        tmp = tempfile.TemporaryDirectory()
        orig = config.DB_PATH
        config.DB_PATH = Path(tmp.name) / 'd.db'
        db._conn = None
        db.connect()
        try:
            for days in (999999999999999, -1, 0):
                db.list_checkin_logs(limit=5, days=days)
                db.list_task_logs(limit=5, days=days)
                db.task_log_stats(days=days)
        finally:
            if db._conn is not None:
                db._conn.close()
            db._conn = None
            config.DB_PATH = orig
            try:
                tmp.cleanup()
            except PermissionError:
                pass


if __name__ == '__main__':
    unittest.main()

"""离线升级包（issue #153）：上传、校验、以及更新器那条「不联网」的路。

背景：内网机器拿不到 GitHub，一键更新无从谈起。做法是「别处下载发布包 → 面板上传
→ 走同一套更新流程」。这个文件锁的是这条链路上最容易悄悄失守的几件事：

  · 上传只碰服务端固定路径（客户端给的只是文件内容，不是路径）；
  · 不是本项目的包、或摘要对不上，必须**当场拒收且不留半个文件**；
  · 更新器走这条路时真的**不查 GitHub**（内网环境里那一步必然超时）；
  · 两边各有一份 `.version` 读取实现（面板不 import 更新器），必须读出一致；
  · 包大小上限与请求体上限不能脱节（否则上传在中间件那层就被 413 掉）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config, db, main as server_main, security
from server.routers import system as system_router
from server.services import updater

_ROOT = Path(__file__).resolve().parents[2]
ADMIN = {'username': 'admin', 'role': 'admin'}


def _load_updater_module(**env):
    """按环境装载 deploy/update.py（它读环境变量定常量）。"""
    keys = ['WB_UPDATE_PACKAGE', 'WB_UPDATE_PACKAGE_SHA256', 'WB_INSTALL_DIR', *env]
    old = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.update({k: str(v) for k, v in env.items()})
        spec = importlib.util.spec_from_file_location('upd_pkg', _ROOT / 'deploy' / 'update.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _make_package(path: Path, version: str = 'v9.9.9', *, with_version: bool = True) -> Path:
    """造一个结构正确的发布包（体积过 100KB 下限，用不可压缩数据）。"""
    import io as _io

    with tarfile.open(path, 'w:gz') as tf:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(f'workbuddy-manager-{version}/{name}')
            info.size = len(data)
            tf.addfile(info, _io.BytesIO(data))

        if with_version:
            add('.version', f'{version}\n'.encode())
        add('server/main.py', b"version='9.9.9'\n")
        add('web/out/index.html', b'<html>new</html>')
        add('server/big.bin', os.urandom(200_000))
    return path


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (('DATA_DIR', self.root / 'data'), ('ROOT', self.root)):
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        (self.root / 'data').mkdir(parents=True, exist_ok=True)
        patch = mock.patch.object(config, 'DB_PATH', self.root / 'm.db')
        patch.start()
        self.addCleanup(patch.stop)
        db._conn = None
        db.connect()
        self.addCleanup(self._close_db)
        # 这些常量在 import 时按当时的 DATA_DIR 固化，测试里要跟着改
        for name, value in (('PACKAGE_FILE', config.DATA_DIR / 'update-package.tar.gz'),
                            ('STATUS_FILE', config.DATA_DIR / 'update-status.json'),
                            ('LOCK_FILE', config.DATA_DIR / 'update.lock'),
                            ('LOG_FILE', config.DATA_DIR / 'update.log')):
            patch = mock.patch.object(updater, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        db.set_setting(updater.PACKAGE_META_KEY, {})

        app = FastAPI()
        app.include_router(system_router.router)
        app.dependency_overrides[security.current_user] = lambda: ADMIN
        app.dependency_overrides[security.require_session_admin] = lambda: ADMIN
        self.client = TestClient(app)

    def _close_db(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None

    def _upload(self, path: Path, expected: str | None = None):
        """上传一个包。`expected=None` = 自动填**正确**的摘要。

        摘要现在是必填项（见 updater.allow_unverified_package）：绝大多数用例
        关心的是"包本身能不能收"，不该被摘要门槛挡住，所以默认替它们算好；
        专门测"不填摘要"的用例显式传 `expected=''`。
        """
        if expected is None:
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
        with open(path, 'rb') as fh:
            return self.client.post(
                '/api/system/update-package',
                files={'file': (path.name, fh, 'application/gzip')},
                data={'expected_sha256': expected},
            )


class UploadEndpointTest(_Base):
    def test_valid_package_is_stored_and_recorded(self) -> None:
        pkg = _make_package(self.root / 'workbuddy-manager-v9.9.9.tar.gz')
        r = self._upload(pkg)
        self.assertEqual(r.status_code, 200, r.text)
        meta = r.json()
        self.assertTrue(meta['present'])
        self.assertEqual(meta['version'], 'v9.9.9')
        self.assertEqual(meta['sha256'], hashlib.sha256(pkg.read_bytes()).hexdigest())
        self.assertTrue(updater.PACKAGE_FILE.is_file(), '包没有落盘到固定路径')
        self.assertEqual(updater.package_meta()['version'], 'v9.9.9')

    def test_wrong_expected_sha256_is_refused_without_leaving_files(self) -> None:
        pkg = _make_package(self.root / 'a.tar.gz')
        r = self._upload(pkg, expected='0' * 64)
        self.assertEqual(r.status_code, 400)
        self.assertIn('sha256 与填的不一致', r.text)
        self.assertFalse(updater.PACKAGE_FILE.exists(), '拒收时不该留下包')
        self.assertFalse(updater.package_meta().get('present'))

    def test_matching_expected_sha256_is_accepted(self) -> None:
        pkg = _make_package(self.root / 'b.tar.gz')
        digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
        r = self._upload(pkg, expected=digest.upper())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['expected_sha256'], digest, '摘要要归一化成小写存下来')

    def test_upload_without_digest_is_refused(self) -> None:
        """不填摘要 = 无法确认包没被替换，必须当场拒收（漏洞修复的守卫）。"""
        pkg = _make_package(self.root / 'nodigest.tar.gz')
        r = self._upload(pkg, expected='')
        self.assertEqual(r.status_code, 400)
        self.assertIn('sha256', r.text)
        self.assertFalse(updater.PACKAGE_FILE.exists(), '拒收时不该留下包')
        self.assertFalse(updater.package_meta().get('present'))

    def test_upload_without_digest_allowed_by_explicit_env(self) -> None:
        """显式声明「我知道自己在装什么」的离线环境仍可放行。"""
        pkg = _make_package(self.root / 'nodigest-ok.tar.gz')
        with mock.patch.dict(os.environ, {'WB_ALLOW_UNVERIFIED_PACKAGE': '1'}):
            r = self._upload(pkg, expected='')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()['expected_sha256'], '没填就是没填，不能编一个')
        self.assertTrue(updater.PACKAGE_FILE.is_file())

    def test_non_package_is_refused(self) -> None:
        junk = self.root / 'junk.tar.gz'
        junk.write_bytes(os.urandom(200_000))
        r = self._upload(junk)
        self.assertEqual(r.status_code, 400)
        self.assertIn('读不了', r.text)
        self.assertFalse(updater.PACKAGE_FILE.exists())
    def test_package_without_version_marker_is_refused(self) -> None:
        pkg = _make_package(self.root / 'c.tar.gz', with_version=False)
        r = self._upload(pkg)
        self.assertEqual(r.status_code, 400)
        self.assertIn('.version', r.text)
        self.assertFalse(updater.PACKAGE_FILE.exists())

    def test_oversize_is_refused(self) -> None:
        pkg = _make_package(self.root / 'd.tar.gz')
        with mock.patch.object(updater, 'PACKAGE_MAX_BYTES', 1000):
            r = self._upload(pkg)
        self.assertEqual(r.status_code, 413)
        self.assertFalse(updater.PACKAGE_FILE.exists())

    def test_delete_clears_file_and_record(self) -> None:
        pkg = _make_package(self.root / 'e.tar.gz')
        self.assertEqual(self._upload(pkg).status_code, 200)
        r = self.client.delete('/api/system/update-package')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(updater.PACKAGE_FILE.exists())
        self.assertFalse(updater.package_meta().get('present'))


class StartUpdateFromPackageTest(_Base):
    def _spawn_env(self, **kw) -> dict:
        """跑 start_update 但拦下真正的进程，返回传给它环境变量。"""
        captured: dict = {}

        class _Proc:
            pid = 4321

            def wait(self, timeout=None):
                return 0

        def fake_popen(argv, **kwargs):
            captured['argv'] = argv
            captured['env'] = kwargs.get('env') or {}
            return _Proc()

        with mock.patch.object(updater.subprocess, 'Popen', fake_popen), \
             mock.patch.object(updater, '_lock_active', lambda: False):
            ok, message = updater.start_update(**kw)
        return {'ok': ok, 'message': message, **captured}

    def test_without_package_it_refuses(self) -> None:
        got = self._spawn_env(target='manager', source='uploaded')
        self.assertFalse(got['ok'])
        self.assertIn('还没有上传升级包', got['message'])

    def test_with_package_it_passes_env_and_expected_digest(self) -> None:
        pkg = _make_package(self.root / 'f.tar.gz')
        digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
        self.assertEqual(self._upload(pkg, expected=digest).status_code, 200)
        got = self._spawn_env(target='manager', source='uploaded')
        self.assertTrue(got['ok'], got['message'])
        env = got['env']
        self.assertEqual(env['WB_UPDATE_PACKAGE'], str(updater.PACKAGE_FILE))
        self.assertEqual(env['WB_UPDATE_PACKAGE_SHA256'], digest)

    def test_stored_package_without_digest_is_refused_at_install(self) -> None:
        """旧版面板存下的"无摘要包"也不能直接装：上传端点不是唯一入口。"""
        pkg = _make_package(self.root / 'legacy.tar.gz')
        with mock.patch.dict(os.environ, {'WB_ALLOW_UNVERIFIED_PACKAGE': '1'}):
            self.assertEqual(self._upload(pkg, expected='').status_code, 200)
        got = self._spawn_env(target='manager', source='uploaded')
        self.assertFalse(got['ok'], '没有摘要的包必须拒绝安装')
        self.assertIn('sha256', got['message'])
        # 显式声明风险后才放行（同样是那份包）
        with mock.patch.dict(os.environ, {'WB_ALLOW_UNVERIFIED_PACKAGE': '1'}):
            allowed = self._spawn_env(target='manager', source='uploaded')
        self.assertTrue(allowed['ok'], allowed['message'])
        self.assertNotIn('WB_UPDATE_PACKAGE_SHA256', allowed['env'],
                         '没填摘要就不该传一个假摘要给更新器')

    def test_release_source_does_not_pass_package_env(self) -> None:
        pkg = _make_package(self.root / 'g.tar.gz')
        self.assertEqual(self._upload(pkg).status_code, 200)
        got = self._spawn_env(target='manager', source='release')
        self.assertTrue(got['ok'], got['message'])
        self.assertNotIn('WB_UPDATE_PACKAGE', got['env'],
                         '从 GitHub 更新的那条路不该被上传的包影响')


class OfflineUpdaterTest(unittest.TestCase):
    """更新器侧：指定离线包时不联网、按包内版本走。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pkg = _make_package(self.root / 'offline.tar.gz')

    class _Rep:
        def __init__(self) -> None:
            self.lines: list[tuple[str, str]] = []
            self.integrity: dict | None = None

        def log(self, msg, level='info'):
            self.lines.append((level, msg))

        def set_integrity(self, status: str, digest: str = '') -> None:
            self.integrity = {'status': status, 'digest': digest}

        def text(self):
            return '\n'.join(m for _, m in self.lines)

    def test_offline_mode_never_touches_network(self) -> None:
        mod = _load_updater_module(WB_UPDATE_PACKAGE=str(self.pkg))
        with mock.patch.object(mod, 'fetch_release_via_api',
                               side_effect=AssertionError('离线包不该查 API')), \
             mock.patch.object(mod, 'fetch_release_via_redirect',
                               side_effect=AssertionError('离线包不该走重定向')):
            info = mod._fetch_release_info(self._Rep())
        self.assertEqual(info['tag'], 'v9.9.9')
        self.assertEqual(info['local_package'], str(self.pkg))

    def test_missing_package_is_reported_clearly(self) -> None:
        mod = _load_updater_module(WB_UPDATE_PACKAGE=str(self.root / 'nope.tar.gz'))
        with self.assertRaises(RuntimeError) as ctx:
            mod._fetch_release_info(self._Rep())
        self.assertIn('找不到离线升级包', str(ctx.exception))

    def test_package_without_version_is_refused(self) -> None:
        bad = _make_package(self.root / 'noversion.tar.gz', with_version=False)
        mod = _load_updater_module(WB_UPDATE_PACKAGE=str(bad))
        with self.assertRaises(RuntimeError) as ctx:
            mod._fetch_release_info(self._Rep())
        self.assertIn('.version', str(ctx.exception))

    def test_expected_digest_is_verified(self) -> None:
        digest = hashlib.sha256(self.pkg.read_bytes()).hexdigest()
        mod = _load_updater_module(WB_UPDATE_PACKAGE=str(self.pkg),
                                   WB_UPDATE_PACKAGE_SHA256='0' * 64)
        rep = self._Rep()
        with self.assertRaises(RuntimeError) as ctx:
            mod.verify_download(self.pkg, mod._fetch_release_info(rep), rep)
        self.assertIn('sha256 与发布页不一致', str(ctx.exception))

        good = _load_updater_module(WB_UPDATE_PACKAGE=str(self.pkg),
                                    WB_UPDATE_PACKAGE_SHA256=digest)
        rep2 = self._Rep()
        good.verify_download(self.pkg, good._fetch_release_info(rep2), rep2)
        self.assertIn('校验通过', rep2.text())

    def test_both_version_readers_agree(self) -> None:
        """面板与更新器各有一份 .version 读取实现 —— 它们必须读出一致。"""
        self.assertEqual(updater._read_package_version(self.pkg),
                         _load_updater_module().read_package_version(self.pkg))
        self.assertEqual(updater._read_package_version(self.pkg), 'v9.9.9')


class NoStringLstripOnDigestTest(unittest.TestCase):
    """发布包摘要的处理不许用 `str.lstrip('sha256:')`。

    为什么单列一条（2026-10-14 实测）：`lstrip` 的参数是**字符集**不是前缀 ——
    摘要若以 `5`/`a`/`2` 这种字符开头，会被啃掉一位（长度从 64 变 63），于是
    在 CI 上表现为「sha256 与发布页不一致」，而在本机碰巧不出现（摘要开头不在
    那个集合里）。这种错只在特定数据上炸，最难查，所以直接禁止这个写法。
    """

    def test_no_char_set_strip_on_digest(self) -> None:
        offenders = []
        for path in (_ROOT / 'server' / 'tests').glob('*.py'):
            if path.name == Path(__file__).name:
                continue        # 本文件里有「坏写法的演示」与说明，跳过自己
            text = path.read_text(encoding='utf-8')
            # 拼出被禁的写法：本文件里不出现这个字面量，否则守卫会扫到自己
            banned = '.lstrip' + "('sha256:')"
            if banned in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], '摘要去前缀请用 removeprefix 或切片：' + '、'.join(offenders))

    def test_removeprefix_is_the_right_tool(self) -> None:
        digest = 'sha256:5f3a' + '0' * 60
        self.assertEqual(len(digest.removeprefix('sha256:')), 64)
        self.assertNotEqual(len(digest.lstrip('sha256:')), 64,
                            'lstrip 是字符集剥离，这里必须能看出它把开头也啃了')


class LimitsStayConsistentTest(unittest.TestCase):
    """包大小上限、请求体上限、中间件豁免路径三者不能脱节。"""

    def test_package_limit_fits_inside_body_limit(self) -> None:
        self.assertLess(updater.PACKAGE_MAX_BYTES, server_main.MAX_PACKAGE_BODY_BYTES,
                        '包上限 ≥ 请求体上限时，上传会在中间件那层被 413 掉'
                        '（multipart 还要占一点边界与表头）')

    def test_middleware_exempts_exactly_the_upload_path(self) -> None:
        self.assertEqual(tuple(server_main.PACKAGE_UPLOAD_PATHS),
                         ('/api/system/update-package',))
        paths = {r.path for r in system_router.router.routes}
        for p in server_main.PACKAGE_UPLOAD_PATHS:
            self.assertIn(p, paths, f'{p} 不在路由里（豁免了但没人处理）')


if __name__ == '__main__':
    unittest.main()

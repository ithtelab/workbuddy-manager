"""端到端：一键更新的完整链路（下载 → 校验 → 解压 → 安装）。

与 test_release_integrity.py 的分工：
  * 那边是**单元**测试，只锁「校验这个函数」和「接线是否接上」；
  * 这边跑**真实的 update_manager**，只有 GitHub API 的返回是伪造的
    （它指向 file:// 的假 Release），其余全程真跑：下载、校验、解压、
    替换 server/ 与 web/out/、同步 .version 与文档。

为什么值得单独写：单元测试全绿但主流程装错了包/装到一半的情况是存在的，
而这正是供应链攻击最想要的结果——**包校验过了，然后在安装阶段被换掉**。
只有把整条链路跑一遍才能锁住「校验的包 == 安装的包」。

同时验证失败时的**不落盘**性质：校验不过，安装目录必须一个字节都没变。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]


def _ssh_keygen_available() -> bool:
    return shutil.which('ssh-keygen') is not None


class _Rep:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.signature: dict | None = None
        self.state: dict = {}

    def log(self, msg: str, level: str = 'info') -> None:
        self.lines.append((level, msg))

    def step(self, name: str) -> None:
        self.lines.append(('info', f'== {name} =='))

    def set_target_version(self, tag: str) -> None:
        self.state['target_version'] = str(tag or '').strip().lstrip('vV')

    def set_integrity(self, status: str, digest: str = '') -> None:
        self.integrity = {'status': status, 'digest': digest}

    def finish(self, ok: bool) -> None:
        self.state['ok'] = ok

    def text(self) -> str:
        return '\n'.join(m for _, m in self.lines)

    def warned(self) -> bool:
        return any(lvl == 'warn' for lvl, _ in self.lines)


@unittest.skipUnless(_ssh_keygen_available(), '需要 ssh-keygen（OpenSSH 8.0+）')
def _digest_of(path: Path) -> str:
    """发布页会给的那种摘要串（GitHub 的 assets[].digest 就是 `sha256:…`）。"""
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


class UpdateEndToEndTest(unittest.TestCase):
    """真实跑 update_manager，只伪造 GitHub API 的返回。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    # ── 夹具 ──

    def _make_install(self, name: str) -> Path:
        """造一个"已部署"的安装目录（旧版本）。"""
        inst = Path(tempfile.mkdtemp(dir=self.dir)) / name
        (inst / 'server').mkdir(parents=True)
        (inst / 'server' / 'main.py').write_text("version='1.0.0'\n# OLD\n", encoding='utf-8')
        (inst / 'server' / 'requirements.txt').write_text('', encoding='utf-8')
        (inst / 'web' / 'out').mkdir(parents=True)
        (inst / 'web' / 'out' / 'index.html').write_text('<html>OLD</html>', encoding='utf-8')
        (inst / 'deploy').mkdir()
        # 安装目录里放一份"正在使用的" update.py（就是 repo 里这份）
        shutil.copyfile(_ROOT / 'deploy' / 'update.py', inst / 'deploy' / 'update.py')
        (inst / '.version').write_text('v1.0.0\n', encoding='utf-8')
        return inst

    def _make_release(self, name: str, *, version: str = '9.9.9',
                      deploy_diff: bool = True,
                      upstream: bool = False) -> tuple[Path, str]:
        """打一个真实发布包，返回 (tar.gz, 'sha256:…') —— 摘要是发布页会给的那份。"""
        stage = Path(tempfile.mkdtemp(dir=self.dir)) / f'workbuddy-manager-v{version}'
        (stage / 'server').mkdir(parents=True)
        (stage / 'server' / 'main.py').write_text(
            f"version='{version}'\n# NEW\n", encoding='utf-8')
        (stage / 'server' / 'requirements.txt').write_text('', encoding='utf-8')
        (stage / 'web' / 'out').mkdir(parents=True)
        (stage / 'web' / 'out' / 'index.html').write_text('<html>NEW</html>', encoding='utf-8')
        (stage / '.version').write_text(f'v{version}\n', encoding='utf-8')
        (stage / 'CHANGELOG.md').write_text('# 更新日志\n\n## [9.9.9]\n- 新\n', encoding='utf-8')
        # 包体积下限（download() 会拒绝 <100KB，避免把错误页当包）。
        # 必须用**不可压缩**的数据：全零在 gzip 下会缩到几百字节，
        # 那样测的就不是"正常下载"而是"下载异常偏小"了。
        (stage / 'server' / 'big.bin').write_bytes(os.urandom(200_000))
        if deploy_diff:
            (stage / 'deploy').mkdir()
            (stage / 'deploy' / 'update.py').write_text('# 包内的（随包更新）\n', encoding='utf-8')
        if upstream:
            # 上游源码随包分发（原仓库已不可用）。没有 docker-compose.yml 的话
            # _sync_bundled_upstream 会当成「本包未含上游源码」直接跳过。
            (stage / 'upstream').mkdir()
            (stage / 'upstream' / 'docker-compose.yml').write_text(
                'services:\n  wb2api:\n    build: .\n    ports:\n      - "7863:7863"\n',
                encoding='utf-8')
            (stage / 'upstream' / 'VERSION.txt').write_text('from-package\n', encoding='utf-8')

        pkg = Path(tempfile.mkdtemp(dir=self.dir)) / f'workbuddy-manager-v{version}.tar.gz'
        subprocess.run(['tar', 'czf', str(pkg), '-C', str(stage.parent), stage.name],
                       check=True, capture_output=True)
        return pkg, _digest_of(pkg)

    def _load(self, install: Path, **env):
        keys = ['WB_INSTALL_DIR', 'WB_DATA_DIR', 'WB_UPDATE_STATUS',
                'WB_SERVICE_NAME', *env.keys()]
        old = {k: os.environ.get(k) for k in keys}
        try:
            os.environ.update({
                'WB_INSTALL_DIR': str(install),
                'WB_DATA_DIR': str(install / 'data'),
                'WB_UPDATE_STATUS': str(install / 'data' / 'update-status.json'),
                'WB_SERVICE_NAME': 'workbuddy-web-does-not-exist',
                **{k: str(v) for k, v in env.items()},
            })
            spec = importlib.util.spec_from_file_location(
                'upd_e2e', str(install / 'deploy' / 'update.py'))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return mod

    def _wire_release(self, mod, pkg: Path, digest: str | None,
                      version: str = '9.9.9', *, size: int | None = None) -> None:
        """把假 Release 接到模块上：assets 用 file:// URL（urlopen 原生支持），
        所以下载与校验都是真跑的；只伪造 GitHub API 这一步。

        `digest` 传 None 模拟「免 API 的回退路径」（拿不到发布页摘要）。
        """
        asset = {
            'name': pkg.name,
            'browser_download_url': pkg.as_uri(),
            'size': pkg.stat().st_size if size is None else size,
        }
        if digest is not None:
            asset['digest'] = digest
        rel = {'tag_name': f'v{version}', 'assets': [asset]}

        mod.http_json = lambda url, timeout=20: rel  # type: ignore[assignment]
        # systemctl restart 在测试环境不存在，替换为空操作
        mod.run = lambda *a, **kw: (0, '')  # type: ignore[assignment]

    def _make_upstream_dir(self, name: str) -> Path:
        """造一个"随包分发"的上游目录（非 git，带 compose）。"""
        up = Path(tempfile.mkdtemp(dir=self.dir)) / name
        up.mkdir(parents=True)
        (up / 'docker-compose.yml').write_text(
            'services:\n  wb2api:\n    build: .\n    ports:\n      - "7863:7863"\n',
            encoding='utf-8')
        (up / 'VERSION.txt').write_text('old\n', encoding='utf-8')
        return up

    def _run_update(self, mod, pkg: Path, digest: str | None,
                    version: str = '9.9.9', *, size: int | None = None):
        """用假 Release 元数据驱动真实的 update_manager。"""
        self._wire_release(mod, pkg, digest, version, size=size)
        rep = _Rep()
        try:
            mod.update_manager(rep)
            return True, rep
        except Exception as exc:  # noqa: BLE001
            rep.log(f'更新失败：{exc}', 'error')
            return False, rep

    # ── 正常路径：装上去的必须就是验过的那一份 ──

    def test_full_update_installs_verified_package(self) -> None:
        inst = self._make_install('ok')
        pkg, digest = self._make_release('ok')
        mod = self._load(inst)
        ok, rep = self._run_update(mod, pkg, digest)
        self.assertTrue(ok, rep.text())

        # 代码真的换了
        main = (inst / 'server' / 'main.py').read_text(encoding='utf-8')
        self.assertIn("version='9.9.9'", main)
        self.assertIn('# NEW', main)
        self.assertIn('NEW', (inst / 'web' / 'out' / 'index.html').read_text(encoding='utf-8'))
        # 版本标记与文档同步
        self.assertEqual((inst / '.version').read_text(encoding='utf-8').strip(), 'v9.9.9')
        self.assertIn('9.9.9', (inst / 'CHANGELOG.md').read_text(encoding='utf-8'))
        # 校验结果落到状态（界面据此显示「已校验」）
        self.assertEqual(rep.integrity['status'], 'digest')

        # deploy/ 现在**随包更新**（维护者决定，见 issue #55）：更新器本身就在
        # 这个目录里，不更新它意味着以后每次都用旧逻辑，它修过的毛病到不了用户手上
        # （#28 修过的 compose 探测就是这么丢的）。
        #
        # 依据：这一步在**校验之后**才执行（`verify_download` 在解压前比过大小与
        # 摘要），所以覆盖 deploy/ 与覆盖 server/ 是同一性质 —— 都是同一份下载
        # 来的内容。真正的不变量由下面那条攻击路径用例守着：
        # 摘要不符 → 整个更新中止、什么都不落盘。
        installed_deploy = (inst / 'deploy' / 'update.py').read_text(encoding='utf-8')
        self.assertIn('包内的（随包更新）', installed_deploy,
                      'deploy/ 没随包更新 —— 更新器会永远停在旧版本')
        # 覆盖前必须备份（出问题能回退）
        self.assertTrue(list((inst).glob('backup-*/deploy/update.py')),
                        '覆盖 deploy/ 前没有备份')
        self.assertIn('已同步到包内版本', rep.text())

    def test_deploy_sync_can_be_disabled(self) -> None:
        """`WB_SYNC_DEPLOY=0` 时保持本地 deploy/ 不动（手工维护者的退路）。

        关掉时必须**告警**并说清代价（更新器会一直是旧的），不能静默跳过。
        """
        inst = self._make_install('ok')
        pkg, digest = self._make_release('ok')
        mod = self._load(inst)
        with mock.patch.dict(os.environ, {'WB_SYNC_DEPLOY': '0'}):
            ok, rep = self._run_update(mod, pkg, digest)
        self.assertTrue(ok, rep.text())
        installed_deploy = (inst / 'deploy' / 'update.py').read_text(encoding='utf-8')
        self.assertNotIn('包内的（随包更新）', installed_deploy,
                         '关了同步却还是被覆盖')
        self.assertIn('verify_download', installed_deploy)
        self.assertTrue(rep.warned(), '跳过同步却没告警')
        self.assertIn('WB_SYNC_DEPLOY=0', rep.text())

    # ── 攻击路径：包被换过，必须中止且不落盘 ──

    def test_tampered_package_aborts_without_touching_install(self) -> None:
        inst = self._make_install('tampered')
        pkg, digest = self._make_release('tampered')
        data = bytearray(pkg.read_bytes())
        data[len(data) // 2] ^= 0xFF
        pkg.write_bytes(bytes(data))  # 摘要不跟着改 = 包在传输途中被换掉的处境

        mod = self._load(inst)
        ok, rep = self._run_update(mod, pkg, digest)
        self.assertFalse(ok, '被换过的包竟然装上了')
        self.assertIn('sha256 与发布页不一致', rep.text())

        # 关键：一个字节都不能动
        self.assertIn('# OLD', (inst / 'server' / 'main.py').read_text(encoding='utf-8'))
        self.assertIn('OLD', (inst / 'web' / 'out' / 'index.html').read_text(encoding='utf-8'))
        self.assertEqual((inst / '.version').read_text(encoding='utf-8').strip(), 'v1.0.0')
        self.assertFalse((inst / 'CHANGELOG.md').exists())

    def test_failed_dependency_install_aborts_without_touching_install(self) -> None:
        """pip 失败（网络、PEP 668、磁盘满…）必须判定更新失败，且不落盘（issue #157）。

        早先是换完代码再 `check=False` 装依赖：pip 失败也标记成功并重启，
        新代码缺依赖起不来，容器无限重启。
        """
        inst = self._make_install('pipfail')
        pkg, digest = self._make_release('pipfail')
        mod = self._load(inst)
        pip_calls: list[list[str]] = []

        def fake_run(cmd, *a, **kw):
            if cmd[1:3] == ['-m', 'pip']:
                pip_calls.append(cmd)
                return 1, 'ERROR: Could not install packages'
            return 0, ''

        orig_update = mod.update_manager

        def update_with_failing_pip(rep):
            mod.run = fake_run
            return orig_update(rep)

        mod.update_manager = update_with_failing_pip
        ok, rep = self._run_update(mod, pkg, digest)
        self.assertFalse(ok, 'pip 失败竟然判定为更新成功')
        self.assertNotIn('ok', rep.state, '失败的更新不应写终态 ok')
        self.assertIn('依赖安装失败', rep.text())
        # 与 install.sh 一致：先带 --break-system-packages，失败再不带重试
        self.assertEqual(len(pip_calls), 2)
        self.assertIn('--break-system-packages', pip_calls[0])
        self.assertNotIn('--break-system-packages', pip_calls[1])

        self.assertIn('# OLD', (inst / 'server' / 'main.py').read_text(encoding='utf-8'))
        self.assertIn('OLD', (inst / 'web' / 'out' / 'index.html').read_text(encoding='utf-8'))
        self.assertEqual((inst / '.version').read_text(encoding='utf-8').strip(), 'v1.0.0')
        self.assertFalse((inst / 'CHANGELOG.md').exists())

    # ── 依赖装到哪：容器里以非 root 跑，系统 site-packages 归 root ──
    #
    # 容器形态下 `pip install` 默认写 /usr/local/lib/python3.12/site-packages，
    # 而进程是 uid 10001 → EACCES。光"失败要响"（#160）不够：更新会干净地失败，
    # 但**永远升不上去**。要装到用户站点（/app/.local，见 Dockerfile 的
    # PYTHONUSERBASE），它排在全站 site-packages 之前，所以能压住旧版本。

    def _run_with_pip(self, mod, pkg, digest, *, writable: bool,
                      results: list[tuple[int, str]]):
        """驱动真实 update_manager，只接管 pip 的调用与"系统站点是否可写"的判定。

        results: 按调用序号取第 n 次 pip 的 (code, out)，越界则重复最后一项。
        """
        pip_calls: list[list[str]] = []

        def fake_run(cmd, *a, **kw):
            if cmd[1:3] == ['-m', 'pip']:
                pip_calls.append(cmd)
                return results[min(len(pip_calls) - 1, len(results) - 1)]
            return 0, ''

        orig_update = mod.update_manager

        def update_with_fake_pip(rep):
            mod.run = fake_run
            mod._system_site_writable = lambda: writable
            return orig_update(rep)

        mod.update_manager = update_with_fake_pip
        ok, rep = self._run_update(mod, pkg, digest)
        return ok, rep, pip_calls

    def test_pip_uses_user_install_when_system_site_readonly(self) -> None:
        """系统 site-packages 不可写时必须装到用户站点，否则容器更新永远装不上。"""
        inst = self._make_install('pipuser')
        pkg, digest = self._make_release('pipuser')
        mod = self._load(inst)
        ok, rep, calls = self._run_with_pip(
            mod, pkg, digest, writable=False, results=[(0, '')])
        self.assertTrue(ok, rep.text())
        self.assertTrue(calls, '压根没调用 pip')
        self.assertIn('--user', calls[0],
                      '系统 site-packages 不可写却没加 --user —— 容器里会 EACCES')
        self.assertIn('--break-system-packages', calls[0],
                      '--user 不在 PEP 668 豁免名单里，必须同时带这个参数')

    def test_pip_prefers_system_install_when_writable(self) -> None:
        """可写时不该引入 --user：会悄悄改变既有部署的安装位置。"""
        inst = self._make_install('pipsys')
        pkg, digest = self._make_release('pipsys')
        mod = self._load(inst)
        ok, rep, calls = self._run_with_pip(
            mod, pkg, digest, writable=True, results=[(0, '')])
        self.assertTrue(ok, rep.text())
        self.assertTrue(calls, '压根没调用 pip')
        self.assertTrue(all('--user' not in c for c in calls),
                        f'可写时不该用 --user：{calls}')

    def test_pip_falls_back_to_user_on_permission_error(self) -> None:
        """可写性判定可能误判（ACL / 只读挂载）：pip 报权限问题时补一次 --user。"""
        inst = self._make_install('pipperm')
        pkg, digest = self._make_release('pipperm')
        mod = self._load(inst)
        ok, rep, calls = self._run_with_pip(
            mod, pkg, digest, writable=True,
            results=[(1, 'ERROR: Could not install packages due to an OSError: '
                        '[Errno 13] Permission denied: /usr/local/lib/python3.12/site-packages'),
                     (1, 'ERROR: Permission denied'),
                     (0, '')])
        self.assertTrue(ok, rep.text())
        self.assertTrue(any('--user' in c for c in calls),
                        f'权限失败后没有回退到 --user：{calls}')

    # ── venv 里不能退回 --user（issue #382） ──
    #
    # venv 的 pip 明确拒绝 `--user`（"User site-packages are not visible in
    # this virtualenv"）；原先的可写性判定只看目录 ACL，判成不可写就加
    # `--user`，于是本地 venv 部署的更新永远失败。

    def _run_with_pip_venv(self, mod, pkg, digest, *, writable: bool,
                           purelib: str, results: list[tuple[int, str]]):
        pip_calls: list[list[str]] = []

        def fake_run(cmd, *a, **kw):
            if cmd[1:3] == ['-m', 'pip']:
                pip_calls.append(cmd)
                return results[min(len(pip_calls) - 1, len(results) - 1)]
            return 0, ''

        orig_update = mod.update_manager

        def update_with_fake_pip(rep):
            mod.run = fake_run
            mod._in_venv = lambda: True
            mod._venv_purelib_writable = lambda: (writable, purelib)
            return orig_update(rep)

        mod.update_manager = update_with_fake_pip
        ok, rep = self._run_update(mod, pkg, digest)
        return ok, rep, pip_calls

    def test_venv_never_passes_user(self) -> None:
        """venv 中即使目录被判不可写，也不能传 --user（pip 会直接拒绝）。"""
        inst = self._make_install('venvok')
        pkg, digest = self._make_release('venvok')
        mod = self._load(inst)
        ok, rep, calls = self._run_with_pip_venv(
            mod, pkg, digest, writable=True,
            purelib='/opt/workbuddy-manager/venv/lib/python3.14/site-packages',
            results=[(0, '')])
        self.assertTrue(ok, rep.text())
        self.assertTrue(calls, '压根没调用 pip')
        self.assertTrue(all('--user' not in c for c in calls),
                        f'venv 里不该出现 --user：{calls}')
        self.assertTrue(all('--break-system-packages' not in c for c in calls),
                        f'venv 里不该传 PEP 668 的开关：{calls}')

    def test_venv_unwritable_fails_before_pip_with_path(self) -> None:
        """venv 目标不可写时提前报出具体路径，不要连着跑两次无意义的 pip。"""
        inst = self._make_install('venvro')
        pkg, digest = self._make_release('venvro')
        mod = self._load(inst)
        purelib = '/opt/workbuddy-manager/venv/lib/python3.14/site-packages'
        ok, rep, calls = self._run_with_pip_venv(
            mod, pkg, digest, writable=False, purelib=purelib, results=[(0, '')])
        self.assertFalse(ok, 'venv 不可写竟然更新成功')
        self.assertEqual(calls, [], '预检失败后仍调用了 pip')
        self.assertIn(purelib, rep.text(), '错误里没有给出不可写的目录')
        self.assertIn('虚拟环境', rep.text())

    def test_venv_permission_error_does_not_retry_with_user(self) -> None:
        """venv 里 pip 报权限问题时也不补 --user：那条路本来就走不通。"""
        inst = self._make_install('venvperm')
        pkg, digest = self._make_release('venvperm')
        mod = self._load(inst)
        ok, rep, calls = self._run_with_pip_venv(
            mod, pkg, digest, writable=True, purelib='/venv/lib/python3.14/site-packages',
            results=[(1, 'ERROR: [Errno 13] Permission denied'), (1, 'ERROR: Permission denied')])
        self.assertFalse(ok, 'pip 失败竟然更新成功')
        self.assertTrue(calls, '压根没调用 pip')
        self.assertTrue(all('--user' not in c for c in calls),
                        f'venv 的权限回退不该加 --user：{calls}')

    # ── 管理端没变时，上游仍要跟着包走 ──
    #
    # 上游源码只随发布包分发（原仓库已不可用），所以"管理端版本没变"不等于
    # "上游没变"。早先 update_manager 在**下载之前**就 return，于是「全部更新」
    # 在管理端已最新时什么都不做 —— 包里带着更新的上游源码也同步不进去。

    def test_current_version_still_syncs_bundled_upstream(self) -> None:
        """管理端已是最新时，「全部更新」仍要把包内上游同步进去。"""
        inst = self._make_install('bothsame')
        pkg, digest = self._make_release('bothsame', version='1.0.0', upstream=True)
        up = self._make_upstream_dir('bothsame')
        mod = self._load(inst, WB_UPSTREAM_DIR=up)
        self._wire_release(mod, pkg, digest, '1.0.0')
        rebuilt: list[bool] = []
        mod.rebuild_upstream = lambda rep: rebuilt.append(True)  # type: ignore[assignment]

        rep = _Rep()
        mod.update_upstream(rep)   # main() 里上游在前
        mod.update_manager(rep)

        self.assertEqual((up / 'VERSION.txt').read_text(encoding='utf-8'), 'from-package\n',
                         '包内上游源码没同步进去')
        self.assertTrue(rebuilt, '上游源码变了却没触发重建')
        # 管理端代码一个字节都没动（版本相同 → 短路）
        self.assertIn('# OLD', (inst / 'server' / 'main.py').read_text(encoding='utf-8'))
        self.assertEqual((inst / '.version').read_text(encoding='utf-8').strip(), 'v1.0.0')

    def test_upstream_only_target_syncs_bundled_upstream(self) -> None:
        """--target upstream 对随包分发的上游必须真的同步（早先是空操作）。"""
        inst = self._make_install('uponly')
        pkg, digest = self._make_release('uponly', upstream=True)
        up = self._make_upstream_dir('uponly')
        mod = self._load(inst, WB_UPSTREAM_DIR=up)
        self._wire_release(mod, pkg, digest)
        mod.rebuild_upstream = lambda rep: None  # type: ignore[assignment]

        mod.update_upstream(_Rep())

        self.assertEqual((up / 'VERSION.txt').read_text(encoding='utf-8'), 'from-package\n')

    def test_release_downloaded_once_for_both_targets(self) -> None:
        """both 时发布包只下一次 —— 两条路径共用同一次下载与解压。"""
        inst = self._make_install('once')
        pkg, digest = self._make_release('once', upstream=True)
        up = self._make_upstream_dir('once')
        mod = self._load(inst, WB_UPSTREAM_DIR=up)
        self._wire_release(mod, pkg, digest)
        mod.rebuild_upstream = lambda rep: None  # type: ignore[assignment]

        urls: list[str] = []
        real_download = mod.download

        def counting_download(url, dest, rep):
            urls.append(url)
            return real_download(url, dest, rep)

        mod.download = counting_download  # type: ignore[assignment]

        rep = _Rep()
        mod.update_upstream(rep)
        mod.update_manager(rep)

        self.assertEqual(len(urls), 1, f'发布包被下载了 {len(urls)} 次：{urls}')

    def test_current_version_manager_only_does_not_download(self) -> None:
        """--target manager 且已最新时连包都不该下（省流量，也省 GitHub 配额）。"""
        inst = self._make_install('mgrsame')
        pkg, digest = self._make_release('mgrsame', version='1.0.0')
        mod = self._load(inst)
        self._wire_release(mod, pkg, digest, '1.0.0')

        def boom(*a, **kw):
            raise AssertionError('已是最新版本，不该下载发布包')

        mod.download = boom  # type: ignore[assignment]
        rep = _Rep()
        mod.update_manager(rep)   # 不应抛
        self.assertIn('当前已是最新版本', rep.text())

    def test_fallback_path_installs_but_says_it_could_not_compare(self) -> None:
        """拿不到发布页摘要（免 API 的回退路径）时：照装，但**如实说没比对**。

        这条是刻意的取舍：回退路径存在的意义就是「GitHub API 被限流时还能更新」，
        所以不能因为拿不到摘要就拒绝更新；但也不能假装校验过 —— 状态记成 none，
        日志里打出本地 sha256 让人能自己比对。
        """
        inst = self._make_install('nodigest')
        pkg, _ = self._make_release('nodigest')
        mod = self._load(inst)
        ok, rep = self._run_update(mod, pkg, None)
        self.assertTrue(ok, rep.text())
        self.assertIn('# NEW', (inst / 'server' / 'main.py').read_text(encoding='utf-8'))
        self.assertEqual(rep.integrity['status'], 'none')
        self.assertIn('未比对发布页摘要', rep.text())
        self.assertRegex(rep.text(), r'发布包 sha256：[0-9a-f]{64}')

    def test_size_mismatch_aborts_before_unpacking(self) -> None:
        """下载被截断（大小不符）必须当场拒绝 —— 这种包解压到一半才炸，报错还看不出原因。"""
        inst = self._make_install('truncated')
        pkg, digest = self._make_release('truncated')
        mod = self._load(inst)
        ok, rep = self._run_update(mod, pkg, digest, size=pkg.stat().st_size - 1024)
        self.assertFalse(ok, '大小对不上竟然也装了')
        self.assertIn('大小不符', rep.text())
        self.assertIn('# OLD', (inst / 'server' / 'main.py').read_text(encoding='utf-8'))

    def test_bootstrap_from_old_updater_installs_new_deploy(self) -> None:
        """鸡生蛋：旧版更新器（会把 deploy/ 整体拷过去）装上新版后，
        新的校验逻辑就位 —— 老部署要换掉更新器只能靠它自己，确保这条路成立。"""
        inst = self._make_install('boot')
        # 模拟旧版 update.py：没有 verify_download，且会同步 deploy/
        old_updater = "import shutil\nfrom pathlib import Path\n"
        (inst / 'deploy' / 'update.py').write_text(old_updater, encoding='utf-8')
        # 新包里的 deploy/update.py 就是仓库里这份（含摘要校验）
        stage = Path(tempfile.mkdtemp(dir=self.dir)) / 'workbuddy-manager-v9.9.9'
        shutil.copytree(_ROOT / 'deploy', stage / 'deploy')
        (stage / 'server').mkdir()
        (stage / 'server' / 'main.py').write_text("version='9.9.9'\n", encoding='utf-8')
        (stage / 'web' / 'out').mkdir(parents=True)
        (stage / 'web' / 'out' / 'index.html').write_text('NEW', encoding='utf-8')
        (stage / '.version').write_text('v9.9.9\n', encoding='utf-8')
        # 旧版更新器的行为：deploy/ 会被复制过去
        shutil.copytree(stage / 'deploy', inst / 'deploy', dirs_exist_ok=True)
        new_updater = (inst / 'deploy' / 'update.py').read_text(encoding='utf-8')
        self.assertIn('verify_download', new_updater, '新更新器没有就位，过渡会断')
        self.assertIn('_sha256_of', new_updater)


    # ── 离线升级包（issue #153）：不联网也要能装 ──

    def test_offline_package_installs_without_touching_github(self) -> None:
        """指定离线包时：不查 GitHub、按包内容装上去，且摘要对不上就中止。

        这条走的是 `WB_UPDATE_PACKAGE` 那条路 —— 内网机器唯一的更新方式。
        单元测试只覆盖到「元信息怎么来的」与「摘要比对」，**安装本身**（解压、
        替换 server/、同步版本号）只有整条链路跑一遍才算数。
        """
        inst = self._make_install('offline')
        pkg, digest = self._make_release('offline')
        mod = self._load(inst, WB_UPDATE_PACKAGE=str(pkg),
                         WB_UPDATE_PACKAGE_SHA256=digest.removeprefix('sha256:'))
        mod.run = lambda *a, **kw: (0, '')      # pip / systemctl 不真跑（与 _wire_release 同）
        # 故意不接 GitHub 元信息：只要代码去查网络，这一步就会炸
        rep = _Rep()
        mod.update_manager(rep)
        self.assertIn("version='9.9.9'", (inst / 'server' / 'main.py').read_text(encoding='utf-8'))
        self.assertEqual((inst / '.version').read_text(encoding='utf-8').strip(), 'v9.9.9')
        self.assertEqual(rep.integrity['status'], 'digest')
        self.assertIn('离线升级包', rep.text())

    def test_offline_package_with_wrong_digest_aborts(self) -> None:
        inst = self._make_install('offline-bad')
        pkg, _digest = self._make_release('offline-bad')
        mod = self._load(inst, WB_UPDATE_PACKAGE=str(pkg),
                         WB_UPDATE_PACKAGE_SHA256='0' * 64)
        mod.run = lambda *a, **kw: (0, '')
        rep = _Rep()
        with self.assertRaises(RuntimeError) as ctx:
            mod.update_manager(rep)
        self.assertIn('sha256 与发布页不一致', str(ctx.exception))
        self.assertIn('# OLD', (inst / 'server' / 'main.py').read_text(encoding='utf-8'),
                      '摘要不符时安装目录一个字节都不该动')

    def test_offline_package_reinstall_same_version_is_allowed(self) -> None:
        """版本号与当前相同时也要装：离线包最常见的用法就是「重装/修复」那一份。"""
        inst = self._make_install('offline-same')
        pkg, digest = self._make_release('offline-same', version='1.0.0')
        mod = self._load(inst, WB_UPDATE_PACKAGE=str(pkg),
                         WB_UPDATE_PACKAGE_SHA256=digest.removeprefix('sha256:'))
        mod.run = lambda *a, **kw: (0, '')
        rep = _Rep()
        mod.update_manager(rep)          # 不应被「已是最新」短路
        self.assertIn('# NEW', (inst / 'server' / 'main.py').read_text(encoding='utf-8'))
        self.assertIn('不比较版本新旧', rep.text())

if __name__ == '__main__':
    unittest.main()

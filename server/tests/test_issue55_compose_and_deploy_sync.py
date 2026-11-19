"""compose 命令探测与 deploy/ 同步（issue #55、#28）。

## 报告者遇到的两件事

1. **更新上游时重建失败**：日志里 `$ docker-compose up -d --build` →
   `命令不存在: [Errno 2] ... 'docker-compose'`（exit 127）。他只在 1Panel 上
   跑过面板自更新，容器里没有 `docker-compose` 这个二进制。
2. **上游更新覆盖了 docker-compose.yml**：他给上游 compose 加的
   `networks: 1panel-network` 与端口收敛被抹掉。

两件事的根因是同一个：**他跑的是旧版更新器**。面板自更新为了保住信任锚，
**故意不替换 `deploy/`**（见 update_manager 里的说明），而 `deploy/update.py`
就是更新器本身 —— 于是「面板升到 1.0.62」并不会让更新器变新，#28 修过的
compose 探测与 compose 定制恢复两处修复都到不了他机器上。

判断依据（他贴的日志里**缺少**这两行，而当前代码必然打印）：

  · `compose 探测：docker compose=…，docker-compose=…`
  · `检测到 docker-compose.yml 有本地定制，更新后会原样恢复`

## 这里钉住的

  · **三种 compose 环境的选择**：容器（镜像自带 v2 插件）/ 宿主机只有 v1 /
    两者都没有 —— 后者必须返回 None，由调用方**直接报错**，绝不去执行一个
    明知不存在的命令（那正是「让他对着 No such file 发呆」的形态）；
  · **探测结果要打日志**：这行是将来判断「更新器是不是太旧」的唯一证据；
  · **不同步 deploy/ 的代价要写出来**：更新器就在 deploy/ 里，不换它意味着
    以后每次都用旧逻辑，而原来的提示只讲「替换的风险」、不讲「不替换的代价」，
    管理员自然一律选择不动手；
  · **显式同步要真的可用**（`WB_SYNC_DEPLOY=1`）：包在解压前已验签，因此包内
    deploy/ 与 server/ 同属维护者签过名的内容，覆盖它不降级信任链；同步前必须
    备份。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]


def _load_update_mod(**env):
    keys = ['WB_RUN_MODE', *env.keys()]
    old = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.update({k: str(v) for k, v in env.items()})
        spec = importlib.util.spec_from_file_location(
            'upd_issue55', str(_ROOT / 'deploy' / 'update.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


class _Rep:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def log(self, msg: str, level: str = 'info') -> None:
        self.lines.append((level, msg))

    def step(self, msg: str) -> None:
        self.lines.append(('step', msg))

    def set_integrity(self, *a, **k) -> None:
        pass

    def set_target_version(self, *a, **k) -> None:
        pass

    def text(self) -> str:
        return '\n'.join(m for _, m in self.lines)


class ComposeCommandTest(unittest.TestCase):
    """三种环境下选出的 compose 命令。这套判据被改坏过两次（#28、#55）。"""

    def setUp(self) -> None:
        self.mod = _load_update_mod()

    def _pick(self, *, v2: bool, v1: bool):
        with mock.patch.object(self.mod, '_has_compose_v2', lambda: v2), \
             mock.patch.object(self.mod, '_has_compose_v1', lambda: v1):
            return self.mod._compose_cmd()

    def test_container_with_bundled_plugin_uses_v2(self) -> None:
        """容器形态：镜像自带 compose 插件（Dockerfile 装到 cli-plugins）。"""
        self.assertEqual(self._pick(v2=True, v1=False), ['docker', 'compose'])

    def test_host_with_only_legacy_v1(self) -> None:
        self.assertEqual(self._pick(v2=False, v1=True), ['docker-compose'])

    def test_neither_available_returns_none(self) -> None:
        """两者都没有时必须返回 None —— 绝不去跑一个明知不存在的命令。

        这正是 #55 的形态：旧更新器「v2 不可用就退回 v1」，于是必然执行
        `docker-compose` 并报 exit 127，用户只看到一行「命令不存在」。
        """
        self.assertIsNone(self._pick(v2=False, v1=False))

    def test_probe_result_is_logged(self) -> None:
        """探测结果必须进日志：这是判断「更新器是否太旧」的证据。"""
        rep = _Rep()
        with mock.patch.object(self.mod, '_has_compose_v2', lambda: True), \
             mock.patch.object(self.mod, '_has_compose_v1', lambda: False):
            self.mod._compose_cmd(rep)
        self.assertIn('compose 探测', rep.text())
        self.assertIn('docker compose=可用', rep.text())

    def test_probe_lists_plugin_paths(self) -> None:
        rep = _Rep()
        with mock.patch.object(self.mod, '_has_compose_v2', lambda: False), \
             mock.patch.object(self.mod, '_has_compose_v1', lambda: False), \
             mock.patch.object(self.mod, '_compose_plugin_paths',
                               lambda: ['/usr/local/lib/docker/cli-plugins/docker-compose']):
            self.mod._compose_cmd(rep)
        self.assertIn('/usr/local/lib/docker/cli-plugins/docker-compose', rep.text())

    def test_plugin_paths_survive_hostile_environment(self) -> None:
        """诊断辅助函数**不能自己把更新带崩**（审核补漏）。

        `Path.home()` 在 uid 没有 passwd 条目时会抛 RuntimeError（某些编排器以
        任意 uid 跑容器），`is_file()` 也可能因权限抛 OSError。这个函数只是用来
        **打印诊断信息**的，它抛错等于「想看日志的人反而把更新搞挂」。
        """
        with mock.patch.object(self.mod.Path, 'home',
                               side_effect=RuntimeError('no home')):
            paths = self.mod._compose_plugin_paths()
        self.assertIsInstance(paths, list)

    def test_plugin_paths_shape(self) -> None:
        """插件路径探测返回字符串列表（本机没有则空列表，不能抛）。"""
        paths = self.mod._compose_plugin_paths()
        self.assertIsInstance(paths, list)
        for p in paths:
            self.assertIsInstance(p, str)


class DeployRiskMessageTest(unittest.TestCase):
    """「不同步的代价」必须写出来，并给出可执行的下一步。"""

    def setUp(self) -> None:
        self.mod = _load_update_mod()

    def test_warns_that_updater_itself_changed(self) -> None:
        rep = _Rep()
        here = Path('/app/deploy/update.py')
        self.mod._explain_deploy_risk(rep, ['update.py'], [], here, Path('/app/data/backup-x'))
        text = rep.text()
        self.assertIn('更新器本身', text)
        self.assertIn('不同步', text)
        self.assertIn('WB_SYNC_DEPLOY', text, '没给出可执行的下一步')
        # 备份路径按本机分隔符渲染（Windows 上是反斜杠），只断言末段
        self.assertIn('backup-x', text, '没告诉用户备份在哪')

    def test_non_updater_change_does_not_cry_wolf(self) -> None:
        """只改了普通脚本时，不要拿「更新器会变旧」吓人（误报会被无视）。"""
        rep = _Rep()
        self.mod._explain_deploy_risk(rep, ['install.sh'], [], Path('/app/deploy/update.py'))
        self.assertNotIn('更新器本身', rep.text())
        self.assertIn('WB_SYNC_DEPLOY', rep.text(), '任何差异都该给出同步办法')

    def test_added_only_says_no_action_needed(self) -> None:
        rep = _Rep()
        self.mod._explain_deploy_risk(rep, [], ['check-upstream.sh'], Path('/app/deploy/update.py'))
        self.assertIn('不影响本次更新', rep.text())


class DeploySyncTest(unittest.TestCase):
    """`WB_SYNC_DEPLOY=1` 的同步行为（包已验签，覆盖不降级信任链）。"""

    def setUp(self) -> None:
        self.mod = _load_update_mod()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.pkg = self.tmp / 'pkg' / 'deploy'
        self.inst = self.tmp / 'install'
        (self.inst / 'deploy').mkdir(parents=True)
        self.pkg.mkdir(parents=True)
        self.backup = self.tmp / 'backup'
        self.backup.mkdir()

    def _write(self, base: Path, name: str, text: str) -> None:
        (base / name).parent.mkdir(parents=True, exist_ok=True)
        (base / name).write_text(text, encoding='utf-8')

    def test_syncs_modified_and_added_files_with_backup(self) -> None:
        self._write(self.pkg, 'update.py', 'NEW')
        self._write(self.pkg, 'check-upstream.sh', 'ADDED')
        self._write(self.inst / 'deploy', 'update.py', 'OLD')

        rep = _Rep()
        added, modified = self.mod._sync_deploy(self.pkg, self.inst, self.backup, rep)

        self.assertEqual(modified, ['update.py'])
        self.assertEqual(added, ['check-upstream.sh'])
        self.assertEqual((self.inst / 'deploy' / 'update.py').read_text(encoding='utf-8'), 'NEW')
        self.assertTrue((self.inst / 'deploy' / 'check-upstream.sh').is_file())
        # 备份必须留下旧内容（出问题要能回退）
        self.assertEqual((self.backup / 'deploy' / 'update.py').read_text(encoding='utf-8'), 'OLD')
        self.assertIn('WB_SYNC_DEPLOY=0', rep.text(), '没告诉用户怎么保持不变')

    def test_identical_files_are_a_no_op(self) -> None:
        self._write(self.pkg, 'x.sh', 'SAME')
        self._write(self.inst / 'deploy', 'x.sh', 'SAME')
        added, modified = self.mod._sync_deploy(self.pkg, self.inst, self.backup, _Rep())
        self.assertEqual((added, modified), ([], []))

    def _enabled_with(self, val: str | None, mod) -> bool:
        """在**调用时**设好环境变量再问一次。

        为什么不能像别的用例那样靠 `_load_update_mod(WB_SYNC_DEPLOY=...)`：那个
        加载器会在 exec_module 之后把 env 还原，而 `_deploy_sync_enabled()` 是
        **调用时**读 env 的 —— 加载期设的值到调用时早就没了（第一版就是这么写的，
        于是「设 0 也不生效」看起来像代码 bug）。
        """
        env = {} if val is None else {'WB_SYNC_DEPLOY': val}
        with mock.patch.dict(os.environ, env, clear=False) as _:
            if val is None:
                os.environ.pop('WB_SYNC_DEPLOY', None)
            return mod._deploy_sync_enabled()

    def test_enabled_by_default(self) -> None:
        """**默认同步**（维护者拍定）：发布包在解压前已验签，更新 deploy/ 与更新
        server/ 同一性质；不更新的代价是「更新器永远是旧的」（#28 → #55 的根因）。"""
        mod = _load_update_mod()
        self.assertTrue(self._enabled_with(None, mod), '默认没开同步')

    def test_can_be_disabled(self) -> None:
        """手工维护 deploy/ 的部署可以关掉（给它们留退路）。"""
        mod = _load_update_mod()
        for val in ('0', ' 0 '):
            with self.subTest(value=val):
                self.assertFalse(self._enabled_with(val, mod))

    def test_other_values_mean_enabled(self) -> None:
        """只有 0 表示关闭：`1` / 其它值都按默认（同步）处理。

        这样「用户没设」与「用户照旧设了 1」行为一致，不会因为一个历史值突然
        变成「不同步」。
        """
        mod = _load_update_mod()
        for val in ('1', 'true', 'yes'):
            with self.subTest(value=val):
                self.assertTrue(self._enabled_with(val, mod))


if __name__ == '__main__':
    unittest.main()

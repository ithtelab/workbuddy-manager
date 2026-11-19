"""强制重建上游容器（issue #334）。

现场：v2.11.2 更新日志里每一步都成功（同步源码、上游标识已同步、等待就绪通过），
但界面一直提示「随包上游与本机不一致」，DSH 反代也照旧报错。日志里有一行
`Container workbuddy2api  Running` —— compose 判定「配置没变」，**没有换容器**，
构建出来的新镜像躺在一边，跑着的还是旧代码。

两件事必须同时成立：
  1. 重建命令带 `--force-recreate`，不让 compose 有机会沿用旧容器；
  2. 重建后核对容器 ID，变了才算真的换过（读不到 ID 时跳过，不误报）。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[2]
_UPDATE = _ROOT / 'deploy' / 'update.py'


def _load():
    spec = importlib.util.spec_from_file_location('wb_update_force', _UPDATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _Rep:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def log(self, text: str, level: str = 'info') -> None:
        self.lines.append((level, text))

    def step(self, text: str) -> None:
        self.lines.append(('step', text))

    def text(self) -> str:
        return '\n'.join(t for _l, t in self.lines)


class ForceRecreateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()
        self.rep = _Rep()
        self.run_calls: list[list[str]] = []

    def _patch(self, ids=('old-container-id', 'new-container-id'), *, persisted=True):
        seq = list(ids)

        def fake_run(cmd, **kw):
            self.run_calls.append(list(cmd))
            return 0, ''

        return [
            mock.patch.object(self.mod, '_docker_unusable_hint', return_value=''),
            mock.patch.object(self.mod, '_missing_copy_sources', return_value=[]),
            mock.patch.object(self.mod, '_compose_cmd', return_value=['docker', 'compose']),
            mock.patch.object(self.mod, 'run', side_effect=fake_run),
            mock.patch.object(self.mod, 'wait_upstream_ready'),
            mock.patch.object(self.mod, '_container_id',
                              side_effect=lambda name: seq.pop(0) if seq else ''),
            mock.patch.object(self.mod, '_auths_persisted', return_value=persisted),
        ]

    def test_skips_rebuild_when_auths_not_persisted(self) -> None:
        """auths 没有挂载/卷时跳过重建（issue #400）：force-recreate 会删掉容器层里
        的账号文件，而面板 data 还在——用户会看到「能登录但没有号」。"""
        patches = self._patch(persisted=False)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.mod.rebuild_upstream(self.rep)
        up = [c for c in self.run_calls if 'up' in c]
        self.assertEqual(up, [], f'没有持久化 auths 时仍执行了重建：{up}')
        self.assertIn('auths', self.rep.text())

    def test_unknown_auths_state_does_not_block(self) -> None:
        """容器还不存在/读不到挂载信息时不拦（首次创建依赖 compose 配置）。"""
        patches = self._patch(persisted=None)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.mod.rebuild_upstream(self.rep)
        self.assertTrue(any('up' in c for c in self.run_calls))

    def test_rebuild_uses_force_recreate(self) -> None:
        patches = self._patch()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.mod.rebuild_upstream(self.rep)
        up = [c for c in self.run_calls if 'up' in c]
        self.assertEqual(len(up), 1, self.run_calls)
        self.assertIn('--force-recreate', up[0],
                      '不带 --force-recreate 时 compose 可能打出「Container ... Running」'
                      '而根本不换容器（#334 的现场）')

    def test_same_container_id_warns(self) -> None:
        """容器 ID 没变时必须告警 —— 这正是「提示消不掉」的直接原因。"""
        patches = self._patch(ids=('same-id-123456789', 'same-id-123456789'))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.mod.rebuild_upstream(self.rep)
        self.assertIn('未变化', self.rep.text())

    def test_changed_container_id_does_not_warn(self) -> None:
        patches = self._patch()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.mod.rebuild_upstream(self.rep)
        self.assertNotIn('未变化', self.rep.text())

    def test_unknown_container_id_is_not_an_error(self) -> None:
        """读不到 ID（docker 不可用 / 容器名不对）时不能把成功的更新报成失败。"""
        patches = self._patch(ids=('', ''))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.mod.rebuild_upstream(self.rep)  # 不应抛
        self.assertNotIn('未变化', self.rep.text())


if __name__ == '__main__':
    unittest.main()

"""账号页「强制退出冷却与模型限流」的回归测试。"""
from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from server import config
from server.services import wb2api


class ForceClearCooling(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self._old_config = config.UPSTREAM_CONFIG
        self._old_dir = config.UPSTREAM_DIR
        config.UPSTREAM_DIR = root
        config.UPSTREAM_CONFIG = root / 'config.json'
        config.UPSTREAM_CONFIG.write_text(
            json.dumps({'state_file': './data/state.json'}), encoding='utf-8',
        )
        self.state_path = root / 'data' / 'state.json'
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text(json.dumps({
            'accounts': {
                'target': {
                    'credits': 123,
                    'disabled': False,
                    'reason': '429 rate limit',
                    'until': '2099-01-01T04:00:00+08:00',
                    'cool_kind': 1,
                    'soft_streak': 7,
                    'model_cooldowns': {
                        'glm-5.3': {
                            'until': '2099-01-01T05:00:00+08:00',
                            'reason': '6004 model rate limit',
                        },
                    },
                    'breaker_until': '2099-01-01T06:00:00+08:00',
                    'retry_count': 3,
                    'degrade_until': '2099-01-01T07:00:00+08:00',
                    'consecutive_fails': 5,
                },
                'other': {'credits': 9, 'until': '2099-02-01T04:00:00+08:00'},
            },
        }, ensure_ascii=False), encoding='utf-8')

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._old_config
        config.UPSTREAM_DIR = self._old_dir
        self._tmp.cleanup()

    def test_clears_only_target_cooling_domains(self) -> None:
        result = wb2api.clear_account_cooling_state('target')
        data = json.loads(self.state_path.read_text(encoding='utf-8'))
        target = data['accounts']['target']

        self.assertEqual(result['uid'], 'target')
        self.assertEqual(target['until'], '0001-01-01T00:00:00Z')
        # `cool_kind` 是**删掉**而不是写 0（评审修正）：它在 state.json 里是 int
        # 枚举（0 = hard_credit），而「没有冷却」的规范表示是不写这个键 ——
        # 上游自己的落盘逻辑就这么做，其注释写明是为避免「until 零值 + cool_kind」
        # 的不一致快照。
        self.assertNotIn('cool_kind', target)
        self.assertEqual(target['soft_streak'], 0)
        self.assertNotIn('reason', target)
        self.assertNotIn('model_cooldowns', target)
        self.assertNotIn('breaker_until', target)
        self.assertEqual(target['retry_count'], 0)
        self.assertNotIn('degrade_until', target)
        self.assertEqual(target['consecutive_fails'], 0)
        self.assertEqual(target['credits'], 123)
        self.assertEqual(data['accounts']['other']['credits'], 9)
        self.assertTrue(pathlib.Path(result['backup']).is_file())

    def test_preserves_disabled_reason(self) -> None:
        data = json.loads(self.state_path.read_text(encoding='utf-8'))
        data['accounts']['target']['disabled'] = True
        data['accounts']['target']['reason'] = 'request illegal'
        self.state_path.write_text(json.dumps(data), encoding='utf-8')

        wb2api.clear_account_cooling_state('target')
        target = json.loads(self.state_path.read_text(encoding='utf-8'))['accounts']['target']
        self.assertEqual(target['reason'], 'request illegal')

    def test_force_clear_stops_edits_and_starts(self) -> None:
        initial = {'connected': True, 'accounts': [{'uid': 'target', 'cooling': True}]}
        after = {'connected': True, 'accounts': [{
            'uid': 'target', 'cooling': False, 'rate_limited_models': [],
        }]}
        with (
            mock.patch.object(wb2api, 'get_status', new=mock.AsyncMock(side_effect=[initial, after])),
            mock.patch.object(
                wb2api,
                '_set_upstream_running',
                new=mock.AsyncMock(side_effect=[(True, 'stopped'), (True, 'started')]),
            ) as control,
        ):
            ok, message, detail = asyncio.run(wb2api.force_clear_account_cooling('target'))

        self.assertTrue(ok, message)
        self.assertIn('已强制清除', message)
        self.assertEqual(detail['uid'], 'target')
        self.assertEqual(control.await_args_list[0].args, (False,))
        self.assertEqual(control.await_args_list[1].args, (True,))
        target = json.loads(self.state_path.read_text(encoding='utf-8'))['accounts']['target']
        self.assertNotIn('model_cooldowns', target)

    def test_noop_when_account_is_already_clear(self) -> None:
        status = {'connected': True, 'accounts': [{
            'uid': 'target', 'cooling': False, 'rate_limited_models': [],
        }]}
        with (
            mock.patch.object(wb2api, 'get_status', new=mock.AsyncMock(return_value=status)),
            mock.patch.object(wb2api, '_set_upstream_running', new=mock.AsyncMock()) as control,
        ):
            ok, message, _ = asyncio.run(wb2api.force_clear_account_cooling('target'))

        self.assertTrue(ok)
        self.assertIn('无需清除', message)
        control.assert_not_awaited()


class AtomicStateWriteTest(unittest.TestCase):
    """state.json 的写入必须是**原子**的，且不依赖平台特有的 API。

    这是评审补的两条，各对应一个真实缺陷：

      · **Windows 上没有 `os.chown`**（该 API 仅 Unix 提供）。原实现在 `try` 里直接
        调用、只捕获 `PermissionError`，于是原生模式部署在 Windows 上会抛
        AttributeError，功能直接不可用（本 PR 自带的四条测试在 Windows 上三条红）。
      · **写失败必须留下完好的原文件**：这是上游启动时要读的文件，半个文件会让它
        起不来（与账号文件的原子写同一条理由，见 test_account_disable 的
        AtomicAuthWriteTest）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self._old_config = config.UPSTREAM_CONFIG
        self._old_dir = config.UPSTREAM_DIR
        config.UPSTREAM_DIR = root
        config.UPSTREAM_CONFIG = root / 'config.json'
        config.UPSTREAM_CONFIG.write_text(
            json.dumps({'state_file': './data/state.json'}), encoding='utf-8')
        self.state_path = root / 'data' / 'state.json'
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text(json.dumps({
            'accounts': {'target': {'credits': 1, 'until': '2099-01-01T04:00:00+08:00',
                                    'cool_kind': 1}},
        }), encoding='utf-8')

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._old_config
        config.UPSTREAM_DIR = self._old_dir
        self._tmp.cleanup()

    def test_works_without_os_chown(self) -> None:
        """没有 `os.chown` 的平台（Windows）也必须能写成功。

        用它来在**任意平台**上钉住这个缺陷：把属性删掉模拟 Windows，Linux/CI 上
        同样会红 —— 否则这条只在 Windows 上才暴露，等于没守。
        """
        with mock.patch.object(wb2api.os, 'chown', None, create=True):
            wb2api.clear_account_cooling_state('target')
        data = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.assertEqual(data['accounts']['target']['until'], '0001-01-01T00:00:00Z')

    def test_chown_denied_does_not_block_write(self) -> None:
        """chown 被拒（非 root 且属主不同）时不该阻断写入 —— 权限模式已保留。"""
        def _deny(*_a, **_k):
            raise PermissionError('operation not permitted')
        with mock.patch.object(wb2api.os, 'chown', _deny, create=True):
            wb2api.clear_account_cooling_state('target')
        data = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.assertNotIn('cool_kind', data['accounts']['target'])

    def test_failed_write_keeps_original_and_leaves_no_temp(self) -> None:
        """替换失败时：原文件逐字节不动，且不留下临时文件。"""
        before = self.state_path.read_bytes()
        with mock.patch.object(wb2api.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                wb2api.clear_account_cooling_state('target')
        self.assertEqual(self.state_path.read_bytes(), before, '写失败把原状态文件弄坏了')
        leftovers = [p.name for p in self.state_path.parent.iterdir()
                     if p.name != 'state.json' and not p.name.endswith('.force-clear.bak')]
        self.assertEqual(leftovers, [], f'留下了临时文件：{leftovers}')


if __name__ == '__main__':
    unittest.main()

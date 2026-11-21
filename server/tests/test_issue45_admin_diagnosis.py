"""状态位走不通时的回退提示，必须把**为什么**说到可操作（issue #45 追问）。

## 为什么要有这条

用户手改了上游 config.json 里的 `admin.enabled: true`（截图里确实是 `true`、
`api_key` 也非空），点「临时停用」却仍拿到回退提示，只能来问「明明开了还是不行」。
界面当时只说「该上游未启用管理接口」——那句话对**两种完全不同的成因**都成立：

  1. 配置里没开（去设置页打开即可）；
  2. 配置里已开，但运行中的上游没加载到它（上游只在启动时读这个开关，
     改完必须重启容器；或镜像早于 2026-09-19，那版没有这组接口）。

而且面板读的配置文件**可能不是用户手改的那个**——本项目的部署里两者都常见
（面板挂载一份、上游容器另一份）。所以文案里要带上面板实际读到的路径，
用户一比就知道是不是同一个文件。

## 这里钉住的

  · 配置里已开启 → 说清「重启容器才生效」并给出路径；
  · 配置里没开 → 给出开关位置（可操作）；
  · 配置读不到 → 如实说读不到，并带上路径与原因；
  · 非 `no_route`（状态位成功 / 真失败）→ 不加这段，别把回退文案贴到成功路径上。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.routers import accounts  # noqa: E402
from server.services import wb2api  # noqa: E402


class AdminEnabledInConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self.path
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        self._tmp.cleanup()

    def _write(self, doc: object) -> None:
        self.path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')

    def test_enabled_true(self) -> None:
        self._write({'admin': {'enabled': True}})
        enabled, where = wb2api.admin_enabled_in_config()
        self.assertTrue(enabled)
        self.assertEqual(where, str(self.path))

    def test_enabled_absent_or_false(self) -> None:
        self._write({'schedule': {}})
        self.assertFalse(wb2api.admin_enabled_in_config()[0])
        self._write({'admin': {'enabled': False}})
        self.assertFalse(wb2api.admin_enabled_in_config()[0])

    def test_enabled_wrong_shape_is_false_not_crash(self) -> None:
        """`admin` 段写成别的形态（字符串等）不能抛。"""
        for doc in ({'admin': True}, {'admin': 'on'}, {'admin': None}, {'admin': []}):
            with self.subTest(doc=doc):
                self._write(doc)
                self.assertFalse(wb2api.admin_enabled_in_config()[0])

    def test_missing_file_reports_path(self) -> None:
        enabled, msg = wb2api.admin_enabled_in_config()
        self.assertIsNone(enabled)
        self.assertIn(str(self.path), msg)

    def test_broken_json_reports_reason(self) -> None:
        self.path.write_text('{not json', encoding='utf-8')
        enabled, msg = wb2api.admin_enabled_in_config()
        self.assertIsNone(enabled)
        self.assertIn(str(self.path), msg)

    def test_non_object_reports(self) -> None:
        self._write([1, 2, 3])
        enabled, msg = wb2api.admin_enabled_in_config()
        self.assertIsNone(enabled)
        self.assertIn(str(self.path), msg)


class FallbackMessageTest(unittest.TestCase):
    """回退提示本身（`_fallback_why`）。"""

    def test_no_route_with_enabled_config_tells_restart(self) -> None:
        with mock.patch.object(wb2api, 'admin_enabled_in_config',
                              lambda: (True, '/srv/upstream/config.json')):
            msg = accounts._fallback_why('no_route', True)
        self.assertIn('重启账号池服务容器', msg)
        self.assertIn('/srv/upstream/config.json', msg)
        self.assertIn('2026-09-19', msg)
        # 回退的代价仍要如实说清（这条不能因为加了诊断就丢掉）
        self.assertIn('完全退出账号池', msg)
        self.assertIn('签到与保活', msg)

    def test_no_route_with_disabled_config_points_at_the_switch(self) -> None:
        with mock.patch.object(wb2api, 'admin_enabled_in_config',
                              lambda: (False, '/srv/upstream/config.json')):
            msg = accounts._fallback_why('no_route', True)
        self.assertIn('设置 → 账号管理接口', msg)
        self.assertNotIn('重启账号池服务容器才生效', msg)

    def test_no_route_with_unreadable_config_says_so(self) -> None:
        with mock.patch.object(wb2api, 'admin_enabled_in_config',
                              lambda: (None, '未找到上游配置文件 /srv/x/config.json')):
            msg = accounts._fallback_why('no_route', True)
        self.assertIn('/srv/x/config.json', msg)
        self.assertIn('设置 → 账号管理接口', msg)

    def test_other_codes_get_no_fallback_text(self) -> None:
        """状态位成功或真失败时不该贴回退文案（那会误导）。"""
        for code in ('ok', 'not_found', 'error', 'skipped'):
            with self.subTest(code=code):
                self.assertEqual(accounts._fallback_why(code, True), '')

    def test_enable_path_does_not_tell_user_to_stop_again(self) -> None:
        """用户点的是**启用**：文案不能让他「再重新停用」——那是停用路径的下一步。

        启用时状态位本来就没东西可清（账号是改名禁用的），账号已经恢复；此时
        「开启开关后再重新停用」是答非所问。只提示「以后想保留签到与保活去哪儿开」。
        """
        with mock.patch.object(wb2api, 'admin_enabled_in_config',
                               lambda: (False, '/p/config.json')):
            msg = accounts._fallback_why('no_route', False)
        self.assertNotIn('再重新停用', msg)
        self.assertIn('设置 → 账号管理接口', msg)
        self.assertIn('已改用改名方式启用', msg)

    def test_disable_path_still_gives_the_re_stop_step(self) -> None:
        """停用路径保留那一步：改名的账号要转到状态位，必须重新停用一次。"""
        with mock.patch.object(wb2api, 'admin_enabled_in_config',
                               lambda: (False, '/p/config.json')):
            msg = accounts._fallback_why('no_route', True)
        self.assertIn('再重新停用', msg)
        self.assertIn('完全退出账号池', msg)

    def test_message_is_plain_text(self) -> None:
        """提示会直接显示在 toast 里——不要出现 markdown 记号。"""
        with mock.patch.object(wb2api, 'admin_enabled_in_config',
                              lambda: (True, '/p/config.json')):
            msg = accounts._fallback_why('no_route', True)
        for token in ('**', '`', '##'):
            self.assertNotIn(token, msg)


if __name__ == '__main__':
    unittest.main()

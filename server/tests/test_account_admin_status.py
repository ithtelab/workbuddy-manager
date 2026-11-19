"""解禁队列的 admin.enabled 探测（PR #287 的后续修正）。

默认分组读本地 config.json；非默认分组通常跑在另一套容器/主机上，面板读不到
它的配置——必须回「未知」，不能拿默认分组的开关去误导。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import accounts as accounts_router  # noqa: E402


class AdminApiStatusTest(unittest.TestCase):
    def test_default_group_reads_local_config(self) -> None:
        with mock.patch.object(accounts_router, '_group',
                               lambda uid: {'is_default': True, 'name': '默认上游'}), \
             mock.patch.object(accounts_router.wb2api, 'admin_enabled_in_config',
                               lambda: (True, '/tmp/config.json')):
            out = accounts_router.account_admin_api_status(
                upstream_id=None, user={'role': 'admin'})
        self.assertEqual(out, {'enabled': True})

    def test_other_group_is_unknown(self) -> None:
        with mock.patch.object(accounts_router, '_group',
                               lambda uid: {'is_default': False, 'name': 'B'}):
            out = accounts_router.account_admin_api_status(
                upstream_id=7, user={'role': 'admin'})
        self.assertEqual(out, {'enabled': None})


if __name__ == '__main__':
    unittest.main()

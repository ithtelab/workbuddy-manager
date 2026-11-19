"""issue #256：签到 code 10001 的两层语义与失败分类。

上游把 10001 同时用于「今天已签到」（幂等成功）和「签到失败，请稍后重试」
（瞬时限流）。只按业务码判断会把失败记成成功，批量签到也因此从不重试。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import tasklog, tencent  # noqa: E402

DOCKER = '2026-10-22T09:00:05.123456789Z '


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        return _Resp(self._payload)


class _Ctx:
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *exc):
        return False


def _checkin(payload):
    client = _Client(payload)
    with mock.patch.object(tencent, '_account_http_client',
                           lambda *a, **k: _Ctx(client)):
        code, message = asyncio.run(tencent.checkin('tok', 'cn'))
    return code, message, client


class CheckinCode10001Test(unittest.TestCase):
    def test_success(self) -> None:
        code, message, _ = _checkin({'code': 0, 'msg': 'ok'})
        self.assertEqual((code, message), (0, '签到成功'))

    def test_already_checked_in_zh(self) -> None:
        code, message, client = _checkin({'code': 10001, 'msg': '今天已签到'})
        self.assertEqual(code, 10001)
        self.assertIn('已签到', message)
        self.assertEqual(client.calls, 1)

    def test_already_checked_in_en(self) -> None:
        code, message, _ = _checkin({'code': 10001, 'msg': 'Already checked in today'})
        self.assertEqual(code, 10001)
        self.assertIn('已签到', message)

    def test_transient_failure_is_not_success(self) -> None:
        """实测报文：同一个 10001 表达「稍后重试」，必须与已签到区分。"""
        code, message, _ = _checkin(
            {'code': 10001, 'msg': '签到失败，请稍后重试', 'requestId': 'x'}
        )
        self.assertEqual(code, -3, '瞬时失败不能被当成 10001 已签到')
        self.assertEqual(message, '签到失败，请稍后重试')

    def test_empty_message_keeps_legacy_semantics(self) -> None:
        """上游省略 msg 时保持旧行为（按已签到处理），不误记失败。"""
        code, message, _ = _checkin({'code': 10001})
        self.assertEqual(code, 10001)
        self.assertIn('已签到', message)

    def test_other_business_code_unchanged(self) -> None:
        code, message, _ = _checkin({'code': 10000, 'msg': '参数错误'})
        self.assertEqual(code, 10000)
        self.assertIn('10000', message)

    def test_unparsable_body_falls_back(self) -> None:
        code, message, _ = _checkin(ValueError('not json'))
        self.assertEqual(code, -1)
        self.assertIn('签到异常', message)


class AlreadyCheckinClassifierTest(unittest.TestCase):
    def test_markers(self) -> None:
        self.assertTrue(tencent.is_already_checkin_message(10001, '今天已签到'))
        self.assertTrue(tencent.is_already_checkin_message(10001, '今天已经签到'))
        self.assertTrue(tencent.is_already_checkin_message(10001, 'Already checked in'))
        self.assertTrue(tencent.is_already_checkin_message(10001, ''))
        self.assertFalse(tencent.is_already_checkin_message(
            10001, '签到失败，请稍后重试'))
        self.assertFalse(tencent.is_already_checkin_message(0, '签到成功'))
        self.assertFalse(tencent.is_already_checkin_message(10000, '今天已签到'))


class CheckinSummarySplitTest(unittest.TestCase):
    def test_split_counters_shown(self) -> None:
        ev = tasklog.parse_line(
            f'{DOCKER}checkin done: total=12 ok=4 already=0 fail=3 skipped=5 '
            'fail_checkin=2 fail_resource=1'
        )
        assert ev is not None
        self.assertEqual(ev['level'], 'warn')
        self.assertIn('共 12 个', ev['message'])
        self.assertIn('签到失败 2', ev['message'])
        self.assertIn('余额查询失败 1', ev['message'])

    def test_old_summary_still_parses(self) -> None:
        ev = tasklog.parse_line(
            f'{DOCKER}checkin done: total=3 ok=1 already=1 fail=1 skipped=0'
        )
        assert ev is not None
        self.assertEqual(ev['level'], 'warn')
        self.assertNotIn('签到失败', ev['message'])


if __name__ == '__main__':
    unittest.main()

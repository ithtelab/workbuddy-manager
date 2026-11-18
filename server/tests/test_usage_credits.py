"""`/v1/usage` 的额度三元组 —— 积分是小数，不能按整数截断（issue #450）。

现象：密钥设了 `quota_credit: 0.5` 且实际消耗 0.04，面板显示正常，
但 `/v1/usage` 的 `credits` 段恒为 `{used: 0, limit: null, remaining: null}`。

根因：`allowance()` 对三个段一律 `int()`：

    int(0.5) = 0   -> limit 变 0 -> `limit_n or None` 变成 None（被当成「不限」）
    int(0.04) = 0  -> used 恒为 0

`daily_tokens.limit` 同时变 null 是同一处逻辑的连带现象（那个用例里
`daily_quota` 为 0，本来就不限，所以要把两条分开断言才不会互相掩盖）。

这类 bug 不报错、只是数字变成 0/None —— 用户会以为额度没生效，
所以用一组「小数 + 整数 + 边界」的输入把口径钉住。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import db, keysvc  # noqa: E402


class UsageAllowanceTest(unittest.TestCase):
    def setUp(self) -> None:
        db.connect()
        self.client = TestClient(self._app())

    @staticmethod
    def _app():
        from server.main import app
        return app

    def _key(self, **kw) -> tuple[int, str]:
        created = keysvc.create_key('probe-' + str(kw.get('quota_credit', 0)), **kw)
        return int(created['id']), str(created['key'])

    def _usage(self, token: str) -> dict:
        r = self.client.get('/v1/usage', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_credit_keeps_decimals(self) -> None:
        """核心回归：0.5 / 0.04 必须原样返回，而不是被截成 0 与 None。"""
        key_id, token = self._key(quota_credit=0.5, models=['x'], realm='cn')
        db.execute('UPDATE api_keys SET used_credit = ? WHERE id = ?', (0.04, key_id))
        credits = self._usage(token)['credits']
        self.assertAlmostEqual(credits['used'], 0.04, places=6)
        self.assertAlmostEqual(credits['limit'], 0.5, places=6)
        self.assertAlmostEqual(credits['remaining'], 0.46, places=6)

    def test_credit_remaining_never_negative(self) -> None:
        """超额消耗时 remaining 夹到 0，不出现负数。"""
        key_id, token = self._key(quota_credit=0.1, models=['x'], realm='cn')
        db.execute('UPDATE api_keys SET used_credit = ? WHERE id = ?', (0.35, key_id))
        self.assertEqual(self._usage(token)['credits']['remaining'], 0)

    def test_credit_unset_reports_null(self) -> None:
        """没设积分额度时仍是 null（= 不限），不能变成 0。"""
        _, token = self._key(models=['x'], realm='cn')
        credits = self._usage(token)['credits']
        self.assertEqual(credits['used'], 0)
        self.assertIsNone(credits['limit'])
        self.assertIsNone(credits['remaining'])

    def test_tokens_stay_integers(self) -> None:
        """token 段必须仍是整数 —— 改成浮点会让前端显示 `343.0`。"""
        key_id, token = self._key(models=['x'], realm='cn', quota=1000)
        db.execute('UPDATE api_keys SET used_tokens = ? WHERE id = ?', (343, key_id))
        tokens = self._usage(token)['tokens']
        self.assertEqual(tokens['used'], 343)
        self.assertIsInstance(tokens['used'], int)
        self.assertEqual(tokens['limit'], 1000)
        self.assertEqual(tokens['remaining'], 657)

    def test_daily_tokens_are_independent(self) -> None:
        """每日 token 限额与积分互不影响（issue #436 引入的第三段）。

        `daily_used_tokens` 只在 `daily_used_day` 是**今天**时才算（次日 0 点复位），
        所以这里两者都要写：只写用量不带日期会被当成「昨天的」，读回来恒为 0。
        """
        key_id, token = self._key(models=['x'], realm='cn',
                                  quota_credit=0.5, daily_quota=1000)
        db.execute('UPDATE api_keys SET used_credit = ?, daily_used_tokens = ?, '
                   'daily_used_day = ? WHERE id = ?',
                   (0.04, 343, db.day_of(), key_id))
        got = self._usage(token)
        self.assertAlmostEqual(got['credits']['limit'], 0.5, places=6)
        self.assertEqual(got['daily_tokens']['limit'], 1000)
        self.assertEqual(got['daily_tokens']['used'], 343)
        self.assertEqual(got['daily_tokens']['remaining'], 657)


if __name__ == '__main__':
    unittest.main()

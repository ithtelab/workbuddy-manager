"""/v1/credits：客户端默认余额路径。

RikkaHub 的 OpenAI 兼容服务商默认打 `{{baseUrl}}/credits`，
读 `data.total_usage` 当余额。这里钉住：同一份数值在两个键上都给，
且鉴权行为与 `/api/usage/token/` 完全一致。
"""
from __future__ import annotations

import unittest

from server.tests.test_balance_api import _Base


class CreditsRouteTest(_Base):
    def test_matches_token_usage(self):
        token, created = self._make_key()
        self._spend(created['id'], credit=12, tokens=0)
        base = self._usage(token)['body']['data']
        r = self._client().get('/v1/credits',
                               headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        data = body['data']
        # RikkaHub 读 total_usage；two keys 必须同值
        self.assertEqual(data['total_usage'], data['total_available'])
        self.assertEqual(data['total_usage'], base['total_available'])
        self.assertEqual(data['total_credits'], base['total_granted'])
        self.assertEqual(data['total_used'], base['total_used'])

    def test_requires_key(self):
        r = self._client().get('/v1/credits')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['error']['code'], 'invalid_api_key')

    def test_bad_key(self):
        r = self._client().get('/v1/credits',
                               headers={'Authorization': 'Bearer wbk_nope'})
        self.assertEqual(r.status_code, 401)


if __name__ == '__main__':
    unittest.main()

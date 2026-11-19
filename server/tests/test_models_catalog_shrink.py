"""可用模型列表按模型中心目录裁剪（issue #407）。

上游 `/v1/models` 仍保留腾讯早已下架的模型（如 `deepseek-v3-0324-lkeap`），
而模型中心直连腾讯拿到的是当前可用清单。这里钉住两处口径：

  · 面板「可用模型」接口 `/api/models`；
  · 网关 `/v1/models`，包括**未限定版本**的密钥（以前只裁了限定版本的）。

取不到权威目录（来源不是 tencent）时一律不裁，宁可多列也不能把列表清空。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import accounts as A  # noqa: E402
from server.routers import gateway as G  # noqa: E402


def _cat(models: list[str], source: str = 'tencent') -> dict:
    return {'source': source, 'models': [{'id': m} for m in models]}


class PanelAvailableModelsTest(unittest.TestCase):
    def _run(self, payload, realm, cats):
        async def fake_catalog(r, force=False):
            return cats[r]
        with mock.patch.object(A.wb2api, 'get_models',
                               mock.AsyncMock(return_value=(True, {'data': payload}))), \
                mock.patch.object(A.modelcatalog, 'catalog', side_effect=fake_catalog):
            return asyncio.run(A.models(realm=realm, user={'role': 'admin'}))

    def test_drops_stale_models_for_one_realm(self) -> None:
        out = self._run([{'id': 'deepseek-v4.1-flash'},
                         {'id': 'deepseek-v3-0324-lkeap'}],
                        'cn', {'cn': _cat(['deepseek-v4.1-flash'])})
        self.assertEqual([m['id'] for m in out['models']], ['deepseek-v4.1-flash'])

    def test_unbound_realm_uses_union(self) -> None:
        out = self._run([{'id': 'glm-5.2'}, {'id': 'global:gpt-5.6-sol'},
                         {'id': 'old-model'}],
                        None, {'cn': _cat(['glm-5.2']),
                               'global': _cat(['gpt-5.6-sol'])})
        self.assertEqual([m['id'] for m in out['models']],
                         ['glm-5.2', 'global:gpt-5.6-sol'])

    def test_non_tencent_catalog_keeps_list(self) -> None:
        out = self._run([{'id': 'old-model'}], 'cn',
                        {'cn': _cat(['glm-5.2'], source='upstream')})
        self.assertEqual([m['id'] for m in out['models']], ['old-model'])


class GatewayUnboundKeyShrinkTest(unittest.TestCase):
    def test_unbound_key_filters_against_both_catalogs(self) -> None:
        payload = {'data': [{'id': 'glm-5.2'}, {'id': 'global:gpt-5.6-sol'},
                            {'id': 'deepseek-v3-0324-lkeap'}]}

        async def fake_catalog(r):
            return {'cn': _cat(['glm-5.2']),
                    'global': _cat(['gpt-5.6-sol'])}[r]

        with mock.patch.object(G.modelcatalog, 'catalog', side_effect=fake_catalog):
            out = asyncio.run(G._drop_models_outside_catalog(payload, {'realm': '', 'models': []}))
        self.assertEqual([m['id'] for m in out['data']], ['glm-5.2', 'global:gpt-5.6-sol'])

    def test_one_realm_not_authoritative_keeps_list(self) -> None:
        payload = {'data': [{'id': 'old-model'}]}

        async def fake_catalog(r):
            return _cat(['glm-5.2']) if r == 'cn' else _cat(['gpt-5.6-sol'], source='upstream')

        with mock.patch.object(G.modelcatalog, 'catalog', side_effect=fake_catalog):
            out = asyncio.run(G._drop_models_outside_catalog(payload, {'realm': '', 'models': []}))
        self.assertEqual([m['id'] for m in out['data']], ['old-model'])


if __name__ == '__main__':
    unittest.main()

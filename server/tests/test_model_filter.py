"""模型清单的「非对话模型」过滤与新字段透出。

背景：上游 2026-09-14 起在它自己的模型解析里加了 `nonChatModel` 过滤——把
嵌入 / 补全 / 代码专用（`nes-` / `completion-` / `codewise-` 前缀）、
输出上限过小（`maxOutputTokens <= 256`）、以及图片生成（tags 含
`text-to-image`）这三类从可选列表剔除，理由是「选了会报 code=11102」。

管理端**直连腾讯**同一接口（比上游多拿显示名与推理档位），所以上游的过滤
不会自动惠及我们——必须自己同步，否则模型中心会列出选不了的东西，
用户点进去必然失败一次。

同时上游新增解析两个字段并透出到 /v1/models：
  * `reasoning.defaultEffort` —— thinking 决策用；空 = 未声明（上游回退硬编码）
  * `supportsImages` —— 多模态能力
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import modelcatalog, tencent  # noqa: E402


class _Resp:
    def __init__(self, payload, status: int = 200) -> None:
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


class _Client:
    """返回预置的模型接口响应。"""

    payload: dict = {}

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        return _Resp(_Client.payload)

    async def aclose(self):
        return None


def _models_payload(models: list[dict], cli_ids: list[str] | None = None) -> dict:
    return {
        'code': 0,
        'data': {
            'models': models,
            'agents': [{'name': 'cli', 'models': cli_ids or [m['id'] for m in models]}],
        },
    }


class NonChatFilterTest(unittest.TestCase):
    """非对话模型不得进入可选清单（对齐上游 nonChatModel）。"""

    AUTH = {'access_token': 'T', 'realm': 'cn', 'uid': 'u'}

    def setUp(self) -> None:
        p = mock.patch.object(config, 'http_client', _Client)
        p.start()
        self.addCleanup(p.stop)

    def _fetch(self, models, cli_ids=None):
        _Client.payload = _models_payload(models, cli_ids)
        return asyncio.run(tencent.fetch_models(self.AUTH))

    def test_embedding_prefix_filtered(self) -> None:
        ok, out = self._fetch([
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
            {'id': 'nes-embedding-3', 'maxInputTokens': 8192, 'maxOutputTokens': 4096},
        ])
        self.assertTrue(ok, out)
        ids = [m['id'] for m in out]
        self.assertIn('glm-5.2', ids)
        self.assertNotIn('nes-embedding-3', ids, 'nes- 前缀是嵌入模型，选了报 11102')

    def test_completion_and_codewise_filtered(self) -> None:
        ok, out = self._fetch([
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
            {'id': 'completion-basic', 'maxInputTokens': 4096, 'maxOutputTokens': 2048},
            {'id': 'codewise-7b', 'maxInputTokens': 4096, 'maxOutputTokens': 2048},
        ])
        ids = [m['id'] for m in out]
        self.assertEqual(ids, ['glm-5.2'], f'补全/代码模型应被过滤，实际 {ids}')

    def test_tiny_output_filtered(self) -> None:
        """输出上限 ≤256 视为 tiny 非对话模型（上游同此判定）。"""
        ok, out = self._fetch([
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
            {'id': 'tiny-model', 'maxInputTokens': 4096, 'maxOutputTokens': 256},
            {'id': 'tiny-model-2', 'maxInputTokens': 4096, 'maxOutputTokens': 128},
        ])
        ids = [m['id'] for m in out]
        self.assertEqual(ids, ['glm-5.2'], f'输出过小的应被过滤，实际 {ids}')

    def test_text_to_image_filtered(self) -> None:
        ok, out = self._fetch([
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
            {'id': 'img-gen', 'maxInputTokens': 4096, 'maxOutputTokens': 4096,
             'tags': ['text-to-image']},
        ])
        ids = [m['id'] for m in out]
        self.assertEqual(ids, ['glm-5.2'], f'图片生成模型应被过滤，实际 {ids}')

    def test_boundary_values_kept(self) -> None:
        """边界：257 输出不算 tiny（上游是 <= 256）；无关 tag 不过滤。"""
        ok, out = self._fetch([
            {'id': 'ok-257', 'maxInputTokens': 8192, 'maxOutputTokens': 257},
            {'id': 'ok-tag', 'maxInputTokens': 8192, 'maxOutputTokens': 4096,
             'tags': ['vision', 'chat']},
        ])
        ids = sorted(m['id'] for m in out)
        self.assertEqual(ids, ['ok-257', 'ok-tag'])

    def test_internal_marker_not_leaked(self) -> None:
        """`_non_chat` 是内部标记，不能出现在返回给前端的字段里。"""
        ok, out = self._fetch([
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
        ])
        for m in out:
            self.assertNotIn('_non_chat', m, '内部标记漏到响应里了')

    def test_all_filtered_reports_failure(self) -> None:
        """全被过滤时不能返回空清单装作成功。"""
        ok, out = self._fetch([
            {'id': 'nes-embed', 'maxInputTokens': 8192, 'maxOutputTokens': 4096},
        ])
        self.assertFalse(ok)
        self.assertIn('未返回任何可用模型', str(out))

    def test_new_fields_extracted(self) -> None:
        """defaultEffort / supportsImages 要解析出来（上游新增）。"""
        ok, out = self._fetch([
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768,
             'supportsImages': True,
             'reasoning': {'supportedEfforts': ['low', 'high'],
                           'defaultEffort': 'high'}},
            {'id': 'plain', 'maxInputTokens': 8192, 'maxOutputTokens': 4096},
        ])
        by_id = {m['id']: m for m in out}
        self.assertEqual(by_id['glm-5.2']['default_effort'], 'high')
        self.assertTrue(by_id['glm-5.2']['supports_images'])
        # 未声明时是空值，不能瞎猜一个默认
        self.assertEqual(by_id['plain']['default_effort'], '')
        self.assertFalse(by_id['plain']['supports_images'])


class GlobalModelScopeTest(unittest.TestCase):
    """国际版取全量模型，不套国内版的 `cli` 白名单。

    上游两个域各走各的解析：国内版 FetchModels 按 agents 的 `cli` 列表过滤，
    国际版 parseGlobalModelNames 直接取 `data.models` 全量、完全不看 agents。
    我们曾把国内版口径套到国际版上，导致国际版实际可用的模型（如
    deepseek-v4.1-flash，上游 issue #84 专为它在国际版的档位做过处理）
    若不在 `cli` 列表里就从模型中心消失。
    """

    def setUp(self) -> None:
        p = mock.patch.object(config, 'http_client', _Client)
        p.start()
        self.addCleanup(p.stop)

    def _fetch(self, realm, models, cli_ids):
        _Client.payload = _models_payload(models, cli_ids)
        return asyncio.run(tencent.fetch_models(
            {'access_token': 'T', 'realm': realm, 'uid': 'u'}))

    MODELS = [
        {'id': 'gpt-5.6-sol', 'maxInputTokens': 200000, 'maxOutputTokens': 32000},
        {'id': 'deepseek-v4.1-flash', 'maxInputTokens': 131072, 'maxOutputTokens': 32000,
         'reasoning': {'supportedEfforts': ['high']}},
    ]

    def test_global_ignores_cli_whitelist(self) -> None:
        ok, out = self._fetch('global', self.MODELS, ['gpt-5.6-sol'])
        self.assertTrue(ok, out)
        ids = [m['id'] for m in out]
        self.assertIn('deepseek-v4.1-flash', ids,
                      f'国际版不该被国内版的 cli 白名单截断，实际 {ids}')

    def test_cn_still_uses_cli_whitelist(self) -> None:
        ok, out = self._fetch('cn', self.MODELS, ['gpt-5.6-sol'])
        self.assertTrue(ok, out)
        self.assertEqual([m['id'] for m in out], ['gpt-5.6-sol'],
                         '国内版必须保持 cli 白名单口径')

    def test_global_narrow_list_form(self) -> None:
        """国际版探测端点可能返回字符串数组（上游 parseGlobalModelNames 兼容该形态）。"""
        _Client.payload = {'code': 0, 'data': ['gpt-5.6-sol', 'deepseek-v4.1-flash']}
        ok, out = asyncio.run(tencent.fetch_models(
            {'access_token': 'T', 'realm': 'global', 'uid': 'u'}))
        self.assertTrue(ok, out)
        self.assertEqual([m['id'] for m in out], ['gpt-5.6-sol', 'deepseek-v4.1-flash'])

    def test_global_keeps_models_matching_cn_non_chat_rules(self) -> None:
        """国内版的非对话判定不作用于国际版（那套规则来自国内的 harness）。

        同一个 id 在国内版被滤掉、在国际版保留——只按 id 判断而不管 realm，
        就等于用国内版的口径裁剪国际版的模型清单。
        """
        models = [
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
            {'id': 'nes-something', 'maxInputTokens': 131072, 'maxOutputTokens': 4096},
        ]
        ok_cn, out_cn = self._fetch('cn', models, None)
        self.assertTrue(ok_cn, out_cn)
        self.assertEqual([m['id'] for m in out_cn], ['glm-5.2'],
                         '国内版应滤掉 nes- 前缀模型')

        ok_gl, out_gl = self._fetch('global', models, None)
        self.assertTrue(ok_gl, out_gl)
        self.assertEqual([m['id'] for m in out_gl], ['glm-5.2', 'nes-something'],
                         '国际版不该套用国内版的非对话规则')


class TwoTierCatalogTest(unittest.TestCase):
    """模型目录是**两级取数**：企业端点 + `/v3/config`。

    官方客户端取模型目录走两级；我们此前只探测企业端点，于是 `/v3/config`
    独有的模型全丢——实测国际版少了 deepseek-v4.1-flash、gpt-6-astra、
    hy4-preview-f、kimi-k2.8-preview（用户报的「国际版没有 DeepSeek」即此，
    上游 commit 0adc345 修的是同一件事）。

    这一组测试按**路径**分派响应，因为两路的内容本就不同——用同一个 body
    应答两个端点的话，即使实现只探测一路也照样通过（那种测试挡不住回归）。
    """

    ENT = [
        {'id': 'gpt-5.6-sol', 'maxInputTokens': 977000, 'maxOutputTokens': 125000, 'credits': 'x3.47'},
        {'id': 'gpt-5.3-codex', 'maxInputTokens': 200000, 'maxOutputTokens': 32000, 'credits': 'x0.20'},
    ]
    V3 = [
        {'id': 'gpt-5.6-sol', 'maxInputTokens': 977000, 'maxOutputTokens': 125000, 'credits': 'x3.47'},
        {'id': 'deepseek-v4.1-flash', 'maxInputTokens': 172000, 'maxOutputTokens': 23000,
         'credits': 'x0.00', 'reasoning': {'supportedEfforts': ['high']}},
        {'id': 'hy4-preview-f', 'maxInputTokens': 977000, 'maxOutputTokens': 63000, 'credits': 'x0.00'},
    ]

    def setUp(self) -> None:
        self.seen: list[str] = []

    def _patch(self, ent, v3):
        """两路各自预置响应；ent/v3 可以是 payload 或 (status, payload) 或异常。"""
        seen = self.seen

        def make(spec):
            if isinstance(spec, Exception):
                def raiser(_url):
                    raise spec
                return raiser

            def fixed(_url):
                status, payload = spec if isinstance(spec, tuple) else (200, spec)
                return _Resp(payload, status)
            return fixed

        ent_f, v3_f = make(ent), make(v3)

        class _PathClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kw):
                seen.append(url)
                return v3_f(url) if url.endswith('/v3/config') else ent_f(url)

        return mock.patch.object(config, 'http_client', lambda *a, **k: _PathClient())

    def _fetch(self, realm, ent, v3):
        self.seen = []
        with self._patch(ent, v3):
            return asyncio.run(tencent.fetch_models(
                {'access_token': 'T', 'realm': realm, 'uid': 'u', 'domain': ''}))

    def test_v3_only_models_are_included(self) -> None:
        """核心回归：只在 /v3/config 下发的模型必须出现在清单里。"""
        ok, out = self._fetch('global', _models_payload(self.ENT), {'code': 0, 'data': {'models': self.V3}})
        self.assertTrue(ok, out)
        ids = [m['id'] for m in out]
        self.assertIn('deepseek-v4.1-flash', ids, f'国际版丢了 v3 独有模型：{ids}')
        self.assertIn('hy4-preview-f', ids, f'国际版丢了 v3 独有模型：{ids}')
        self.assertIn('gpt-5.3-codex', ids, '企业端点独有模型也应保留（补缺）')

    def test_both_paths_are_requested(self) -> None:
        """两路都要探测——少探一路就是本 bug 的成因。"""
        self._fetch('global', _models_payload(self.ENT), {'code': 0, 'data': {'models': self.V3}})
        paths = {u.split('workbuddy.ai')[-1] for u in self.seen}
        self.assertIn('/v3/config', paths, '没探测 /v3/config——v3 独有模型会全部丢失')
        self.assertIn('/v2/enterprises/personal/models', paths, '没探测企业端点')

    def test_dedup_and_v3_priority(self) -> None:
        """同 id 只出现一次，且字段以 v3 为准（上游合并口径）。"""
        ent = _models_payload([{'id': 'gpt-5.6-sol', 'maxInputTokens': 1, 'maxOutputTokens': 2,
                                'credits': 'x9.99'}])
        v3 = {'code': 0, 'data': {'models': [
            {'id': 'gpt-5.6-sol', 'maxInputTokens': 977000, 'maxOutputTokens': 125000,
             'credits': 'x3.47'}]}}
        ok, out = self._fetch('global', ent, v3)
        self.assertTrue(ok, out)
        self.assertEqual(len(out), 1, f'重复条目未去重：{[m["id"] for m in out]}')
        self.assertEqual(out[0]['credits'], 'x3.47', 'v3 条目应为权威（credits 以它为准）')

    def test_v3_failure_degrades_to_enterprise(self) -> None:
        """v3 失败不拖累企业端点（降级为单路，而不是整体报错）。"""
        ok, out = self._fetch('global', _models_payload(self.ENT), (400, {'code': 12403}))
        self.assertTrue(ok, f'v3 失败不该让整次取数失败：{out}')
        self.assertEqual([m['id'] for m in out], ['gpt-5.6-sol', 'gpt-5.3-codex'])

    def test_enterprise_failure_degrades_to_v3(self) -> None:
        ok, out = self._fetch('global', (500, {'code': 500}), {'code': 0, 'data': {'models': self.V3}})
        self.assertTrue(ok, f'企业端点失败不该让整次取数失败：{out}')
        self.assertIn('deepseek-v4.1-flash', [m['id'] for m in out])

    def test_v3_network_error_degrades(self) -> None:
        """网络异常（而非业务错误码）同样只降级、不整体失败。"""
        ok, out = self._fetch('global', _models_payload(self.ENT),
                              RuntimeError('connection reset'))
        self.assertTrue(ok, f'v3 网络异常不该让整次取数失败：{out}')

    def test_both_fail_reports_failure(self) -> None:
        ok, out = self._fetch('global', (500, {'code': 500}), (500, {'code': 500}))
        self.assertFalse(ok, '两路全失败时应报失败，而不是返回空清单')
        self.assertIsInstance(out, str, '失败时应给出原因字符串')

    def test_cn_keeps_cli_filter_with_v3_supplement(self) -> None:
        """国内版：console 的 cli 过滤与 v3 补缺同时生效。"""
        ent = _models_payload(
            [{'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
             {'id': 'nes-embed', 'maxInputTokens': 8192, 'maxOutputTokens': 4096}],
            cli_ids=['glm-5.2'],
        )
        v3 = {'code': 0, 'data': {'models': [
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
            {'id': 'hy4-preview-f', 'maxInputTokens': 256000, 'maxOutputTokens': 32000}]}}
        ok, out = self._fetch('cn', ent, v3)
        self.assertTrue(ok, out)
        ids = [m['id'] for m in out]
        self.assertIn('glm-5.2', ids)
        self.assertIn('hy4-preview-f', ids, '国内版也该拿到 v3 补缺')
        self.assertNotIn('nes-embed', ids, '非对话过滤仍生效')

    def test_v3_null_models_shape_degrades(self) -> None:
        """`/v3/config` 返回 `code:0` 但 `models:null` 时按「这路没数据」处理。

        这是**真实观测到的形状**（用无凭据请求打 /v3/config：data 里有 agent/mcp/
        codebase 等键，但 models 是 null）。token 失效等情况下也会走到这里。
        必须降级而不是当成「成功但空」把整页清空——企业端点的结果要留住。
        """
        v3 = {'code': 0, 'msg': 'ok', 'data': {'agent': {'agents': None}, 'models': None}}
        ok, out = self._fetch('global', _models_payload(self.ENT), v3)
        self.assertTrue(ok, f'该形态应降级为企业端点结果：{out}')
        self.assertEqual([m['id'] for m in out], ['gpt-5.6-sol', 'gpt-5.3-codex'])

    def test_both_null_models_reports_failure(self) -> None:
        """两路都是空 models 时如实报失败，不返回空清单冒充成功。"""
        empty = {'code': 0, 'data': {'models': None}}
        ok, out = self._fetch('global', empty, empty)
        self.assertFalse(ok, '两路都没数据时应报失败')
        self.assertIsInstance(out, str)

    def test_v3_only_models_are_kept_for_global(self) -> None:
        """国际版与国内版对 v3 条目的口径一致（都取全量、都不做 CN 的非对话过滤）。"""
        v3 = {'code': 0, 'data': {'models': [
            {'id': 'nes-thing', 'maxInputTokens': 131072, 'maxOutputTokens': 4096}]}}
        ok_gl, out_gl = self._fetch('global', _models_payload([]), v3)
        self.assertTrue(ok_gl, out_gl)
        self.assertIn('nes-thing', [m['id'] for m in out_gl],
                      '国际版不该套用国内版的非对话规则')


class CatalogFieldPassthroughTest(unittest.TestCase):
    """模型目录要把新字段带到前端（否则 UI 拿不到）。"""

    def test_decorate_keeps_new_fields(self) -> None:
        out = modelcatalog._decorate([
            {'id': 'glm-5.2', 'name': 'GLM', 'context_length': 131072,
             'max_output_tokens': 32768, 'efforts': ['high'],
             'default_effort': 'high', 'supports_images': True},
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['default_effort'], 'high')
        self.assertTrue(out[0]['supports_images'])

    def test_decorate_defaults_for_upstream_fallback(self) -> None:
        """回退来源（上游 /v1/models）没有这些字段 → 给安全默认值。"""
        out = modelcatalog._decorate([{'id': 'x', 'context_length': 100}])
        self.assertEqual(out[0]['default_effort'], '')
        self.assertFalse(out[0]['supports_images'])


if __name__ == '__main__':
    unittest.main()


class EffortFallbackTest(unittest.TestCase):
    """推理档位的三级解析（镜像上游 EffortListing）。

    用户报的 issue #8：模型中心的推理栏对 deepseek-v4.1-flash 显示「不支持」，
    而上游已支持三档强度。根因是**只依赖远端 supportedEfforts**，而腾讯接口
    对部分模型不返回该字段。

    上游 2026-09-15（PR #92）的解法是三级：远端权威 → 产品级静态兜底表 →
    都没有则省略。我们镜像同一套（_EFFORT_FALLBACK 逐条照抄其 effort_catalog.go）。

    另外上游把字段透出为 `reasoning_supported_efforts`（此前 /v1/models 里
    根本没有档位字段）——回退路径必须按新字段名读，否则永远拿不到。
    """

    def test_issue8_model_gets_cn_efforts(self) -> None:
        """issue #8 的那个模型：远端没给档位时，国内版应补上三档。"""
        out = modelcatalog._decorate(
            [{'id': 'deepseek-v4.1-flash', 'efforts': [], 'default_effort': ''}], 'cn')
        self.assertEqual(out[0]['efforts'], ['low', 'high', 'max'])
        self.assertEqual(out[0]['default_effort'], 'high')

    def test_realm_tables_are_not_mixed(self) -> None:
        """同一模型在两个版本的档位不同，不能混用（上游注释明确警告过）。"""
        cn = modelcatalog._decorate([{'id': 'deepseek-v4.1-flash', 'efforts': []}], 'cn')[0]
        gl = modelcatalog._decorate([{'id': 'deepseek-v4.1-flash', 'efforts': []}], 'global')[0]
        self.assertEqual(cn['efforts'], ['low', 'high', 'max'])
        self.assertEqual(gl['efforts'], ['high', 'max'], '国际版档位表不正确')

    def test_issue238_global_remote_lag_adds_max(self) -> None:
        """issue #238：官方客户端已可选 Max，但接口元数据仍只回 high。"""
        out = modelcatalog._decorate(
            [{'id': 'deepseek-v4.1-flash', 'efforts': ['high'], 'default_effort': 'high'}],
            'global')
        self.assertEqual(out[0]['efforts'], ['high', 'max'])
        self.assertEqual(out[0]['default_effort'], 'high')

    def test_remote_wins_over_fallback(self) -> None:
        """远端给了档位就是权威 —— 兜底表不覆盖。"""
        out = modelcatalog._decorate(
            [{'id': 'deepseek-v4.1-flash', 'efforts': ['low'], 'default_effort': 'low'}], 'cn')
        self.assertEqual(out[0]['efforts'], ['low'])
        self.assertEqual(out[0]['default_effort'], 'low')

    def test_unknown_model_gets_nothing(self) -> None:
        """两边都没有 → 空数组，不编造。"""
        out = modelcatalog._decorate([{'id': 'totally-unknown', 'efforts': []}], 'cn')
        self.assertEqual(out[0]['efforts'], [])
        self.assertEqual(out[0]['default_effort'], '')

    def test_default_must_be_in_efforts(self) -> None:
        """默认档不在支持列表内 → 清空（镜像上游 containsEffort 校验）。"""
        out = modelcatalog._decorate(
            [{'id': 'x', 'efforts': ['low'], 'default_effort': 'max'}], 'cn')
        self.assertEqual(out[0]['default_effort'], '')

    def test_upstream_new_field_names_mapped(self) -> None:
        """上游 /v1/models 的 reasoning_supported_efforts 要能被识别。"""
        mapped = modelcatalog._map_upstream_model_fields({
            'id': 'cn:deepseek-v4.1-flash',
            'reasoning_supported_efforts': ['low', 'high', 'max'],
            'reasoning_default_effort': 'high',
            'supports_images': True,
        })
        self.assertEqual(mapped['efforts'], ['low', 'high', 'max'])
        self.assertEqual(mapped['default_effort'], 'high')
        d = modelcatalog._decorate([mapped], 'cn')[0]
        self.assertEqual(d['efforts'], ['low', 'high', 'max'])
        self.assertTrue(d['supports_images'])

    def test_fallback_table_matches_upstream_shape(self) -> None:
        """兜底表的基本形态：两个版本都在、档位非空、默认档合法。"""
        for realm in ('cn', 'global'):
            table = modelcatalog._EFFORT_FALLBACK.get(realm)
            self.assertTrue(table, f'{realm} 兜底表缺失')
            for mid, cap in table.items():
                self.assertTrue(cap['efforts'], f'{realm}/{mid} 档位为空')
                d = cap.get('default')
                if d:
                    self.assertIn(d, cap['efforts'],
                                  f'{realm}/{mid} 的默认档 {d!r} 不在支持列表内')


class UpstreamFallbackPathTest(unittest.TestCase):
    """回退路径（读上游 /v1/models）必须走字段映射。

    上游 2026-09-15 起才在 /v1/models 里透出档位，字段名是
    `reasoning_supported_efforts` —— 我们的内部名字是 `efforts`。
    若回退路径忘了映射，上游明明给了档位我们也读不到
    （这一点是被反证试出来的：直接测 _map_upstream_model_fields 覆盖不到
    「调用方是否真的用了它」）。
    """

    AUTH = {'file': 'a.json', 'uid': '1', 'nickname': '甲', 'realm': 'cn',
            'is_expired': False, 'remain_seconds': 1000}

    def setUp(self) -> None:
        modelcatalog.invalidate()
        # **`addCleanup` 要传 patcher 本身，不能传 `pat.start()` 的返回值**：
        # patch 的 start() 返回的是**被装上去的 mock 对象**，它的 `.stop` 是个
        # 自动生成的 MagicMock 属性——调用它什么都不做。写成
        # `p = pat.start(); self.addCleanup(p.stop)` 会让 patch **永不撤销**。
        #
        # 后果不止本类：`wb2api` 是全局单例模块，那个 mock 会一直留着，后面所有
        # 依赖 `list_auth_accounts` 的用例都静默拿到 mock 值而走偏（实测：
        # test_token_renew 单独跑全绿、全量跑 6 红；二分才定位到这里）。
        for pat in (
            mock.patch.object(modelcatalog.wb2api, 'list_auth_accounts',
                              return_value=[self.AUTH]),
            mock.patch.object(modelcatalog, '_load_token', return_value='tok'),
        ):
            pat.start()
            self.addCleanup(pat.stop)

    def tearDown(self) -> None:
        modelcatalog.invalidate()

    def test_fallback_path_reads_new_field_names(self) -> None:
        # 刻意用**兜底表里没有**的模型名：否则「映射生效」与「兜底表兜住了」
        # 会产生相同结果，测不出映射是否真的在工作。
        # （第一版用的是 deepseek-v4.1-flash——它在兜底表里，去掉映射后测试
        #  仍然全绿；这是反证时发现的。）
        mid = 'brand-new-model-not-in-fallback-table'
        self.assertNotIn(mid, modelcatalog._EFFORT_FALLBACK['cn'],
                         '这个模型不能出现在兜底表里，否则测不出字段映射')

        async def tencent_fail(auth):
            return False, 'token 过期'

        async def upstream():
            # 上游透出的形态：带 cn: 前缀 + reasoning_supported_efforts
            return True, [{'id': f'cn:{mid}',
                           'context_length': 131072, 'max_output_tokens': 8192,
                           'reasoning_supported_efforts': ['low', 'high', 'max'],
                           'reasoning_default_effort': 'high',
                           'supports_images': True}]

        with mock.patch.object(modelcatalog.tencent, 'fetch_models', tencent_fail), \
                mock.patch.object(modelcatalog.wb2api, 'get_models', upstream):
            out = asyncio.run(modelcatalog.catalog('cn', force=True))

        self.assertEqual(out['source'], 'upstream')
        m = out['models'][0]
        self.assertEqual(m['id'], mid, '前缀应被剥掉')
        self.assertEqual(m['efforts'], ['low', 'high', 'max'],
                         '回退路径没映射新字段名 —— 上游给了档位也读不到')
        self.assertEqual(m['default_effort'], 'high')
        self.assertTrue(m['supports_images'])


class ModelCatalogFullFieldsTest(unittest.TestCase):
    """模型目录的完整字段解析（上游 2026-09-15 补齐）。

    背景：上游这次把 /v1/models 的字段大幅补齐（name / description / credits
    / tags / vendor / 能力标志），并顺手给出了它从腾讯接口解析这些字段时用的
    JSON 名（descriptionZh / credits / tags / vendor …）。我们直连腾讯，本就
    能取到这些字段——只是此前没解析。

    其中最有价值的是 **credits（积分倍率）**：同一 prompt 在不同模型上的扣费
    倍率不同，用户挑「省积分」的模型时靠它。此前界面上完全看不到。

    字段名照上游的实测解析结果，不是猜的（详见 tencent.fetch_models 的注释）。
    """

    def test_tencent_fields_parsed(self) -> None:
        payload = {
            'code': 0,
            'data': {
                'models': [{
                    'id': 'glm-5.2',
                    'name': 'GLM-5.2',
                    'descriptionZh': '通用对话模型',
                    'credits': 'x0.05',
                    'tags': ['badge:限时免费'],
                    'vendor': 'zhipu',
                    'isDefault': True,
                    'maxInputTokens': 131072,
                    'maxOutputTokens': 32768,
                    'supportsImages': True,
                    'supportsReasoning': True,
                    'supportsToolCall': True,
                    'onlyReasoning': False,
                    'reasoning': {'supportedEfforts': ['high'], 'defaultEffort': 'high',
                                  'summary': 'auto'},
                }],
                'agents': [{'name': 'cli', 'models': ['glm-5.2']}],
            },
        }

        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *exc): return False
            async def get(self, url, **kw):
                class R:
                    status_code = 200
                    def json(self): return payload
                return R()

        with mock.patch.object(config, 'http_client', _Client):
            ok, out = asyncio.run(tencent.fetch_models(
                {'access_token': 'T', 'realm': 'cn', 'uid': 'u'}))
        self.assertTrue(ok, out)
        m = out[0]
        self.assertEqual(m['description'], '通用对话模型')
        self.assertEqual(m['credits'], 'x0.05', '积分倍率没解析出来 —— 用户挑不了省积分的模型')
        self.assertEqual(m['vendor'], 'zhipu')
        self.assertEqual(m['tags'], ['badge:限时免费'])
        self.assertTrue(m['is_default'])
        self.assertTrue(m['supports_reasoning'])
        self.assertTrue(m['supports_tool_call'])
        self.assertFalse(m['only_reasoning'])
        self.assertEqual(m['reasoning_summary'], 'auto')

    def test_catalog_decorate_passes_fields_through(self) -> None:
        out = modelcatalog._decorate([{
            'id': 'glm-5.2', 'efforts': ['high'], 'credits': 'x0.05',
            'description': '通用对话模型', 'vendor': 'zhipu', 'tags': ['t'],
            'is_default': True, 'supports_reasoning': True,
            'supports_tool_call': True, 'only_reasoning': False,
            'reasoning_summary': 'auto',
        }], 'cn')[0]
        for key, want in (('credits', 'x0.05'), ('description', '通用对话模型'),
                          ('vendor', 'zhipu'), ('tags', ['t']),
                          ('is_default', True), ('supports_reasoning', True),
                          ('supports_tool_call', True), ('reasoning_summary', 'auto')):
            self.assertEqual(out[key], want, f'{key} 没透传到目录')
        self.assertIn('only_reasoning', out)

    def test_missing_fields_get_safe_defaults(self) -> None:
        """上游没给这些字段时要有安全默认（不能 KeyError、不能编造）。"""
        out = modelcatalog._decorate([{'id': 'x', 'efforts': []}], 'cn')[0]
        self.assertEqual(out['credits'], '')
        self.assertEqual(out['description'], '')
        self.assertEqual(out['tags'], [])
        self.assertFalse(out['is_default'])
        self.assertFalse(out['supports_images'])

    def test_upstream_fallback_maps_same_named_fields(self) -> None:
        """回退路径（读上游 /v1/models）的同名字段要能直接透传。

        上游这次透出的 name/description/credits/tags/vendor 与我们内部**同名**，
        不需要映射；只有推理档位名不同（reasoning_supported_efforts）。
        """
        mapped = modelcatalog._map_upstream_model_fields({
            'id': 'cn:glm-5.2', 'name': 'GLM-5.2', 'credits': 'x0.05',
            'description': '[x0.05 credit] 通用对话模型', 'vendor': 'zhipu',
            'tags': ['t'], 'is_default': True, 'supports_tool_call': True,
            'reasoning_supported_efforts': ['high'], 'reasoning_default_effort': 'high',
        })
        d = modelcatalog._decorate([mapped], 'cn')[0]
        self.assertEqual(d['credits'], 'x0.05')
        self.assertEqual(d['vendor'], 'zhipu')
        self.assertTrue(d['is_default'])
        self.assertTrue(d['supports_tool_call'])
        self.assertEqual(d['efforts'], ['high'], '档位字段名映射失效')

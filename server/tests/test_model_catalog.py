"""模型目录（模型中心页数据）的回归测试。

重点：
  1. 系列归属是**按 id 前缀的命名约定推导**，认不出必须归「其他」而不是乱猜
  2. 来源如实标注：腾讯接口成功 = tencent；只有上游 /v1/models = upstream；
     两者都失败 = none 且不抛异常（页面要能正常渲染成空态）
  3. 统计全部由清单真实计算
  4. 缓存与 force 行为
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import modelcatalog  # noqa: E402
from server.services.tencent import _non_chat_model as non_chat_model


def _m(mid: str, name: str = '', ctx: int = 0, out: int = 0, efforts=None) -> dict:
    return {'id': mid, 'name': name, 'context_length': ctx,
            'max_output_tokens': out, 'efforts': efforts or []}


class SeriesTest(unittest.TestCase):
    def test_known_prefixes(self) -> None:
        cases = {
            'glm-5.2': '智谱 GLM',
            'deepseek-v4-pro': 'DeepSeek',
            'kimi-k2.7': 'Kimi',
            'minimax-m3': 'MiniMax',
            'hy3': '腾讯混元',
            'hunyuan-x': '腾讯混元',
            'auto': '自动选择',
        }
        for mid, want in cases.items():
            self.assertEqual(modelcatalog.series_of(mid), want, mid)

    def test_unknown_falls_back_to_other(self) -> None:
        for mid in ('', 'qwen-max', 'some-new-model', 'gpt-4o'):
            self.assertEqual(modelcatalog.series_of(mid), '其他', mid)

    def test_case_insensitive(self) -> None:
        self.assertEqual(modelcatalog.series_of('GLM-5.2'), '智谱 GLM')


class SummarizeTest(unittest.TestCase):
    def test_counts_are_real(self) -> None:
        models = [
            _m('glm-5.3', ctx=1048576, out=32768, efforts=['low', 'high', 'max']),
            _m('glm-5.2', ctx=131072, out=65536),
            _m('kimi-k3-1', ctx=262144, out=32768, efforts=['low']),
            _m('auto', ctx=0, out=0),
        ]
        s = modelcatalog.summarize(models)
        self.assertEqual(s['total'], 4)
        self.assertEqual(s['reasoning'], 2)      # 有 efforts 的两个
        self.assertEqual(s['large_context'], 3)  # >=128K 的三个（1M/128K/256K）
        self.assertEqual(s['max_context'], 1048576)
        self.assertEqual(s['unique_ids'], 4)
        self.assertIn('智谱 GLM', s['series'])
        self.assertIn('Kimi', s['series'])

    def test_empty(self) -> None:
        s = modelcatalog.summarize([])
        self.assertEqual(s['total'], 0)
        self.assertEqual(s['max_context'], 0)
        self.assertEqual(s['series'], [])

    def test_duplicate_ids_counted(self) -> None:
        s = modelcatalog.summarize([_m('glm-5.2'), _m('glm-5.2')])
        self.assertEqual(s['total'], 2)
        self.assertEqual(s['unique_ids'], 1)


class CatalogSourceTest(unittest.TestCase):
    """来源标注与回退链。"""

    def setUp(self) -> None:
        modelcatalog.invalidate()
        self._accts = mock.patch.object(
            modelcatalog.wb2api, 'list_auth_accounts',
            return_value=[{'file': 'a.json', 'uid': '1', 'nickname': '甲', 'realm': 'cn',
                           'is_expired': False, 'remain_seconds': 1000}],
        )
        self._accts.start()
        self.addCleanup(self._accts.stop)
        p = mock.patch.object(modelcatalog, '_load_token', return_value='tok')
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self) -> None:
        modelcatalog.invalidate()

    def test_tencent_preferred(self) -> None:
        async def fake(auth):
            return True, [_m('glm-5.2', name='GLM-5.2', ctx=131072, out=32768,
                             efforts=['low', 'high'])]
        with mock.patch.object(modelcatalog.tencent, 'fetch_models', fake):
            out = asyncio.run(modelcatalog.catalog('cn', force=True))
        self.assertEqual(out['source'], 'tencent')
        self.assertEqual(out['models'][0]['name'], 'GLM-5.2')
        self.assertEqual(out['models'][0]['efforts'], ['low', 'high'])
        self.assertEqual(out['models'][0]['series'], '智谱 GLM')

    def test_falls_back_to_upstream(self) -> None:
        async def fail(auth):
            return False, 'token 过期'
        async def upstream():
            return True, [{'id': 'glm-5.2', 'context_length': 131072,
                           'max_output_tokens': 65536}]
        with mock.patch.object(modelcatalog.tencent, 'fetch_models', fail), \
                mock.patch.object(modelcatalog.wb2api, 'get_models', upstream):
            out = asyncio.run(modelcatalog.catalog('cn', force=True))
        self.assertEqual(out['source'], 'upstream')
        self.assertEqual(out['models'][0]['id'], 'glm-5.2')
        # 回退数据没有显示名 —— 不能凭空造
        self.assertEqual(out['models'][0]['name'], '')
        # 档位则可以补：上游 /v1/models 本身会给出 reasoning_supported_efforts，
        # 缺失时还有产品级兜底表（见 _EFFORT_FALLBACK）。glm-5.2 在兜底表里是
        # high/xhigh —— 这与上游模型中心的口径一致，不是我们编的。
        self.assertEqual(out['models'][0]['efforts'], ['high', 'xhigh'])
        self.assertTrue(out['errors'], '应保留失败原因便于排查')

    def test_all_sources_failed_is_empty_not_crash(self) -> None:
        async def fail(auth):
            return False, 'token 过期'
        async def upstream_fail():
            return False, {'error': 'connection refused'}
        with mock.patch.object(modelcatalog.tencent, 'fetch_models', fail), \
                mock.patch.object(modelcatalog.wb2api, 'get_models', upstream_fail):
            out = asyncio.run(modelcatalog.catalog('cn', force=True))
        self.assertEqual(out['source'], 'none')
        self.assertEqual(out['models'], [])
        self.assertTrue(out['errors'])

    def test_no_accounts_falls_through_gracefully(self) -> None:
        async def upstream():
            return True, [{'id': 'hy3'}]
        with mock.patch.object(modelcatalog.wb2api, 'list_auth_accounts', return_value=[]), \
                mock.patch.object(modelcatalog.wb2api, 'get_models', upstream):
            out = asyncio.run(modelcatalog.catalog('cn', force=True))
        self.assertEqual(out['source'], 'upstream')

    def test_expired_accounts_are_skipped_for_tencent(self) -> None:
        """过期的账号不去调腾讯，直接走上游回退。"""
        calls = []

        async def fake(auth):
            calls.append(token)
            return True, [_m('glm-5.2')]

        async def upstream():
            return True, [{'id': 'glm-5.2'}]

        with mock.patch.object(modelcatalog.wb2api, 'list_auth_accounts',
                               return_value=[{'file': 'a.json', 'uid': '1', 'realm': 'cn',
                                              'is_expired': True, 'remain_seconds': 0}]), \
                mock.patch.object(modelcatalog.tencent, 'fetch_models', fake), \
                mock.patch.object(modelcatalog.wb2api, 'get_models', upstream):
            out = asyncio.run(modelcatalog.catalog('cn', force=True))
        self.assertEqual(calls, [], '过期账号不应被调用')
        self.assertEqual(out['source'], 'upstream')

    def test_second_account_used_when_first_fails(self) -> None:
        # fetch_models 现在收的是 auth dict（含 realm/domain），用 token 区分账号
        async def fake(auth):
            if auth.get('access_token') == 'tok-b':
                return True, [_m('kimi-k3-1')]
            return False, 'first failed'

        accts = [
            {'file': 'a.json', 'uid': '1', 'nickname': '甲', 'realm': 'cn', 'is_expired': False, 'remain_seconds': 900},
            {'file': 'b.json', 'uid': '2', 'nickname': '乙', 'realm': 'cn', 'is_expired': False, 'remain_seconds': 800},
        ]
        with mock.patch.object(modelcatalog.wb2api, 'list_auth_accounts', return_value=accts), \
                mock.patch.object(modelcatalog, '_load_token',
                                 side_effect=lambda f: 'tok-b' if f == 'b.json' else 'tok-a'), \
                mock.patch.object(modelcatalog.tencent, 'fetch_models', fake):
            out = asyncio.run(modelcatalog.catalog('cn', force=True))
        self.assertEqual(out['source'], 'tencent')
        self.assertEqual(out['models'][0]['id'], 'kimi-k3-1')
        self.assertEqual(out['via'], '乙')


class CacheTest(unittest.TestCase):
    def setUp(self) -> None:
        modelcatalog.invalidate()

    def tearDown(self) -> None:
        modelcatalog.invalidate()

    def test_cache_hit_marks_cached(self) -> None:
        calls = {'n': 0}

        async def fake(auth):
            calls['n'] += 1
            return True, [_m('glm-5.2')]

        with mock.patch.object(modelcatalog.wb2api, 'list_auth_accounts',
                               return_value=[{'file': 'a.json', 'uid': '1', 'realm': 'cn',
                                              'is_expired': False, 'remain_seconds': 1}]), \
                mock.patch.object(modelcatalog, '_load_token', return_value='t'), \
                mock.patch.object(modelcatalog.tencent, 'fetch_models', fake):
            first = asyncio.run(modelcatalog.catalog('cn'))
            second = asyncio.run(modelcatalog.catalog('cn'))
        self.assertFalse(first['cached'])
        self.assertTrue(second['cached'])
        self.assertEqual(calls['n'], 1, '缓存期内不应重复请求')

    def test_force_bypasses_cache(self) -> None:
        calls = {'n': 0}

        async def fake(auth):
            calls['n'] += 1
            return True, [_m('glm-5.2')]

        with mock.patch.object(modelcatalog.wb2api, 'list_auth_accounts',
                               return_value=[{'file': 'a.json', 'uid': '1', 'realm': 'cn',
                                              'is_expired': False, 'remain_seconds': 1}]), \
                mock.patch.object(modelcatalog, '_load_token', return_value='t'), \
                mock.patch.object(modelcatalog.tencent, 'fetch_models', fake):
            asyncio.run(modelcatalog.catalog('cn'))
            out = asyncio.run(modelcatalog.catalog('cn', force=True))
        self.assertEqual(calls['n'], 2)
        self.assertFalse(out['cached'])

    def test_failures_use_short_ttl(self) -> None:
        """失败结果只缓存 60 秒，避免把「恰好失败」记成 5 分钟不可用。"""
        async def fail(auth):
            return False, 'x'
        async def upstream_fail():
            return False, {'error': 'y'}
        with mock.patch.object(modelcatalog.wb2api, 'list_auth_accounts', return_value=[]), \
                mock.patch.object(modelcatalog.tencent, 'fetch_models', fail), \
                mock.patch.object(modelcatalog.wb2api, 'get_models', upstream_fail):
            asyncio.run(modelcatalog.catalog('cn', force=True))
        self.assertEqual(modelcatalog._slot('cn')['ttl'], modelcatalog._TTL_FAIL)

    def test_tencent_success_uses_long_ttl(self) -> None:
        async def fake(auth):
            return True, [_m('glm-5.2')]
        with mock.patch.object(modelcatalog.wb2api, 'list_auth_accounts',
                               return_value=[{'file': 'a.json', 'uid': '1', 'realm': 'cn',
                                              'is_expired': False, 'remain_seconds': 1}]), \
                mock.patch.object(modelcatalog, '_load_token', return_value='t'), \
                mock.patch.object(modelcatalog.tencent, 'fetch_models', fake):
            asyncio.run(modelcatalog.catalog('cn', force=True))
        self.assertEqual(modelcatalog._slot('cn')['ttl'], modelcatalog._TTL_OK)


if __name__ == '__main__':
    unittest.main()


class NonChatFilterTest(unittest.TestCase):
    """非对话模型的过滤规则（issue #461：视频模型能被选中但调用必然失败）。

    背景：模型中心是给人**挑模型**的地方，而本网关只转发 chat 协议。上游只在
    「国内版」剔除 `text-to-image`，但 `kling-video-v3` / `minimax-video-h3`
    这类视频生成模型没有任何过滤 —— 它们出现在列表里，用户点了就失败，而失败
    信息讲的是「模型不支持」，看不出是模型本身的用途问题。

    这里把规则补到视频/音频，并**额外加一条否决**：能读视频但产出文本的多模态
    对话模型（`glm-4v`、`qwen-vl`、`*-video-understanding`）必须保留 —— 剔掉
    可用模型比漏剔一个更糟。
    """

    def test_video_generation_is_filtered(self) -> None:
        for mid in ('kling-video-v3', 'minimax-video-h3', 'hunyuan-video-pro',
                    'some-video-gen-1', 'foo-tts-bar'):
            self.assertTrue(non_chat_model(mid, 8192, []), mid)

    def test_video_tags_are_filtered(self) -> None:
        for tag in ('text-to-video', 'image-to-video', 'text-to-speech', 'video-generation'):
            self.assertTrue(non_chat_model('whatever', 8192, [tag]), tag)

    def test_chat_models_pass(self) -> None:
        for mid in ('glm-4.6', 'deepseek-v4.1-flash', 'hy4flash-free', 'kimi-k2'):
            self.assertFalse(non_chat_model(mid, 8192, []), mid)

    def test_multimodal_chat_is_not_filtered(self) -> None:
        """能读视频/图片但产出文本的模型必须保留 —— 剔掉可用模型更糟。"""
        for mid in ('glm-4v', 'qwen2.5-vl-72b', 'xxx-video-understanding',
                    'gpt-4o-audio-preview', 'gemini-omni'):
            self.assertFalse(non_chat_model(mid, 8192, []), mid)

    def test_existing_rules_still_hold(self) -> None:
        """既有三类规则不能因为这次扩规则而失效。"""
        self.assertTrue(non_chat_model('nes-embed', 8192, []), '嵌入模型')
        self.assertTrue(non_chat_model('completion-x', 8192, []), '补全模型')
        self.assertTrue(non_chat_model('codewise-y', 8192, []), '代码专用')
        self.assertTrue(non_chat_model('tiny', 128, []), 'tiny 输出')
        self.assertTrue(non_chat_model('img', 8192, ['text-to-image']), 'tag 图片')

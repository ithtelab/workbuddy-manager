"""密钥导出：把一把密钥转成 cc-switch / ZCode 的配置片段（纯函数 + 端点）。

为什么需要这个功能：用户拿到面板密钥后，要手工把它填进 cc-switch 或 ZCode 的
配置里（baseUrl、模型名、各档位模型都要填对），很容易填错——尤其模型名在两边
口径不同（面板显示裸名 `glm-5.2`，网关与客户端要 `cn:glm-5.2`），填错的表现是
「能选中、一发就 404」。

为什么只能导出「刚创建的」密钥：面板只存哈希（`keysvc.create_key` 里
`out['key'] = token` 是唯一一次明文），列表行里的 prefix 拼不出完整密钥。因此
`/api/keys/export` 接受明文入参而**不按 key_id 查库**——这不是偷懒，是安全设计
的必然结果；如果哪天有人把它改成 key_id + 查库，那就等于库里有明文了。

本文件钉住五件事：

1. **模型 id 必须是网关口径**（带 `cn:` / `global:` 前缀），裸名会被补前缀；
   已有前缀的原样保留（`global:` 决定路由，不能按 realm 改写）。
2. **`global:` 不被 realm 覆盖**：给国际版密钥导出时，`global:xxx` 与
   `cn:xxx` 是**两个不同的模型**，改写前缀会把用户指到另一个模型上。
3. **空模型清单不能产出不可用配置**：至少要有一个默认模型，否则客户端里
   选中即报错。给不出就 400，不静默生成空清单。
4. **导出是敏感操作**：端点必须要求管理员、必须写审计，且审计里**不记密钥**。
5. **claude 的 base url 不带 `/v1`**（codex / ZCode 要带）：口径混用会打到
   `/v1/v1/messages`，那里返回 405 而不是 404，排查时极具误导性。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import keyexport  # noqa: E402

TOKEN = 'wbk_test_token_for_export_only'


class ModelIdTests(unittest.TestCase):
    """模型 id 的口径统一（最容易静默出错的一处）。"""

    def test_bare_name_gets_realm_prefix(self):
        self.assertEqual(keyexport.gateway_model_id('glm-5.2', 'cn'), 'cn:glm-5.2')
        self.assertEqual(keyexport.gateway_model_id('gpt-5.5', 'global'), 'global:gpt-5.5')

    def test_existing_prefix_is_kept_as_is(self):
        """已有前缀不能按 realm 改写：改了就指向另一个模型。"""
        self.assertEqual(keyexport.gateway_model_id('global:gpt-5.5', 'cn'),
                         'global:gpt-5.5')
        self.assertEqual(keyexport.gateway_model_id('cn:glm-5.2', 'global'),
                         'cn:glm-5.2')

    def test_blank_name_is_rejected(self):
        for bad in ('', '   ', None):
            with self.assertRaises(ValueError):
                keyexport.gateway_model_id(bad, 'cn')

    def test_unknown_realm_falls_back_to_cn(self):
        self.assertEqual(keyexport.gateway_model_id('glm-5.2', 'weird'), 'cn:glm-5.2')


class BaseUrlTests(unittest.TestCase):

    def test_appends_v1_once(self):
        self.assertEqual(keyexport.gateway_base_url('http://a.b'), 'http://a.b/v1')
        self.assertEqual(keyexport.gateway_base_url('http://a.b/'), 'http://a.b/v1')
        self.assertEqual(keyexport.gateway_base_url('http://a.b/v1'), 'http://a.b/v1')

    def test_blank_is_rejected(self):
        with self.assertRaises(ValueError):
            keyexport.gateway_base_url('   ')


class AnthropicBaseUrlTests(unittest.TestCase):
    """claude 的 base url 要去掉 /v1 —— 这个口径错了会 405 而不是 404。

    记录一个真实事故：导出给 cc-switch claude 的值曾是 `http://panel/v1/`，
    而 Claude Code 会在其后拼 `/v1/messages`，于是打到 `/v1/v1/messages`；
    面板上该路径**存在但不是 POST 路由**，返回 **405 Method Not Allowed**，
    看起来像"方法用错了"，实际是 base url 多带了一截。
    """

    def test_strips_v1_and_keeps_trailing_slash(self):
        self.assertEqual(keyexport.anthropic_base_url('http://a.b/v1'), 'http://a.b/')
        # 传入已带尾斜杠的形态也要能吃下
        self.assertEqual(keyexport.anthropic_base_url('http://a.b/v1/'), 'http://a.b/')

    def test_subpath_deployment_keeps_prefix(self):
        """子路径部署（面板在 /workbuddy-manager 下）时只去 /v1，不能把前缀吃掉。"""
        self.assertEqual(
            keyexport.anthropic_base_url('https://x.com/workbuddy-manager/v1'),
            'https://x.com/workbuddy-manager/')

    def test_without_v1_is_idempotent(self):
        self.assertEqual(keyexport.anthropic_base_url('http://a.b'), 'http://a.b/')
        self.assertEqual(keyexport.anthropic_base_url('http://a.b/'), 'http://a.b/')

    def test_blank_is_rejected(self):
        with self.assertRaises(ValueError):
            keyexport.anthropic_base_url('  ')


class CCSwitchTests(unittest.TestCase):

    def test_claude_shape_matches_real_config(self):
        """结构与 cc-switch 库里真实 provider 一致（claude 用 env.ANTHROPIC_*）。"""
        cfg = keyexport.to_ccswitch(token=TOKEN, base_url='http://h/v1', app='claude',
                                    name='WB', models=['glm-5.2'], realm='cn')
        env = cfg['env']
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], TOKEN)
        # **根地址**（去掉 /v1）：SDK 自己拼 /v1/messages。给成 http://h/v1/ 的话
        # 实际请求是 /v1/v1/messages，面板上没有这个 POST 路由 → 405。
        self.assertEqual(env['ANTHROPIC_BASE_URL'], 'http://h/')
        self.assertEqual(env['ANTHROPIC_MODEL'], 'cn:glm-5.2')
        # 三个档位都要有值，否则客户端会拿官方模型名打到本端点
        for slot in ('ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
                     'ANTHROPIC_DEFAULT_OPUS_MODEL'):
            self.assertEqual(env[slot], 'cn:glm-5.2')

    def test_codex_shape_matches_real_config(self):
        """codex 用 auth.OPENAI_API_KEY + config（TOML 文本）。"""
        cfg = keyexport.to_ccswitch(token=TOKEN, base_url='http://h/v1', app='codex',
                                    name='WB', models=['glm-5.2'], realm='cn')
        self.assertEqual(cfg['auth']['OPENAI_API_KEY'], TOKEN)
        toml = cfg['config']
        self.assertIn('base_url = "http://h/v1"', toml)
        self.assertIn('model = "cn:glm-5.2"', toml)
        self.assertIn('wire_api = "responses"', toml)
        # 上游是 OpenAI 兼容形态，需要 OpenAI 风格鉴权
        self.assertIn('requires_openai_auth = true', toml)

    def test_toml_quotes_are_escaped(self):
        """模型名里出现引号时不能破坏 TOML（否则 cc-switch 整段配置解析失败）。"""
        cfg = keyexport.to_ccswitch(token=TOKEN, base_url='http://h/v1', app='codex',
                                    name='a"b', models=['m"x'], realm='cn')
        self.assertIn('\\"', cfg['config'])

    def test_empty_models_need_a_default(self):
        """空清单且无默认 → 400；有默认 → 用默认兜底（不产出不可用配置）。"""
        with self.assertRaises(ValueError):
            keyexport.to_ccswitch(token=TOKEN, base_url='http://h/v1', app='claude',
                                  name='WB', models=[], realm='cn')
        cfg = keyexport.to_ccswitch(token=TOKEN, base_url='http://h/v1', app='claude',
                                    name='WB', models=[], realm='cn',
                                    default_model='glm-5.2')
        self.assertEqual(cfg['env']['ANTHROPIC_MODEL'], 'cn:glm-5.2')

    def test_bad_app_and_missing_token_rejected(self):
        with self.assertRaises(ValueError):
            keyexport.to_ccswitch(token=TOKEN, base_url='http://h/v1', app='vscode',
                                  name='WB', models=['m'], realm='cn')
        with self.assertRaises(ValueError):
            keyexport.to_ccswitch(token='  ', base_url='http://h/v1', app='claude',
                                  name='WB', models=['m'], realm='cn')

    def test_duplicate_models_collapse_preserving_order(self):
        cfg = keyexport.to_ccswitch(token=TOKEN, base_url='http://h/v1', app='claude',
                                    name='WB', models=['b', 'a', 'b'], realm='cn')
        self.assertEqual(cfg['env']['ANTHROPIC_MODEL'], 'cn:b')  # 首个保留


class ZCodeTests(unittest.TestCase):

    def test_shape_matches_real_config(self):
        """结构与 ZCode provider_config.json 里真实 provider 一致。"""
        out = keyexport.to_zcode(token=TOKEN, base_url='http://h/v1', name='WB',
                                 provider_id='workbuddy-abc',
                                 models=['glm-5.2', 'kimi-k3-1'], realm='cn',
                                 context_windows={'cn:kimi-k3-1': 1000000})
        rule = out['providerRule']
        self.assertEqual(rule['providerId'], 'workbuddy-abc')
        self.assertEqual(rule['config']['access']['apiKey'], TOKEN)
        self.assertEqual(rule['config']['api']['type'], 'openai-responses')
        self.assertEqual(rule['config']['api']['baseUrl'], 'http://h/v1')
        self.assertEqual(rule['config']['personalModelIds'],
                         ['cn:glm-5.2', 'cn:kimi-k3-1'])
        # modelOrder 与模型清单一致，否则 ZCode 里顺序与我们给的不同
        self.assertEqual(rule['config']['modelOrder'], rule['config']['personalModelIds'])

    def test_context_window_only_when_known(self):
        """拿不到上下文长度就不写该字段，让客户端用自己的默认值。"""
        out = keyexport.to_zcode(token=TOKEN, base_url='http://h/v1', name='WB',
                                 provider_id='p', models=['a', 'b'], realm='cn',
                                 context_windows={'cn:a': 999})
        rules = {r['modelId']: r['config']['properties']
                 for r in out['providerModelRules']}
        self.assertEqual(rules['cn:a'], {'contextWindow': 999})
        self.assertEqual(rules['cn:b'], {})

    def test_global_models_not_rewritten(self):
        out = keyexport.to_zcode(token=TOKEN, base_url='http://h/v1', name='WB',
                                 provider_id='p', models=['global:gpt-5.5'],
                                 realm='global')
        self.assertEqual(out['providerRule']['config']['personalModelIds'],
                         ['global:gpt-5.5'])

    def test_missing_provider_id_or_token_rejected(self):
        with self.assertRaises(ValueError):
            keyexport.to_zcode(token=TOKEN, base_url='http://h/v1', name='WB',
                               provider_id='  ', models=['a'], realm='cn')
        with self.assertRaises(ValueError):
            keyexport.to_zcode(token='', base_url='http://h/v1', name='WB',
                               provider_id='p', models=['a'], realm='cn')


class SerialisationTests(unittest.TestCase):
    """产出的片段必须能直接 json.dumps（前端要原样展示/下载）。"""

    def test_both_clients_are_json_serialisable(self):
        cc = keyexport.to_ccswitch(token=TOKEN, base_url='http://h/v1', app='claude',
                                   name='WB', models=['a'], realm='cn')
        zc = keyexport.to_zcode(token=TOKEN, base_url='http://h/v1', name='WB',
                                provider_id='p', models=['a'], realm='cn')
        json.dumps(cc)
        json.dumps(zc)


class EndpointTests(unittest.TestCase):
    """端点行为：客户端校验、管理员依赖、审计。

    用 `app.dependency_overrides` 而不是 `mock.patch`：`Depends(require_admin)`
    在应用构建时就绑定了解析出来的函数对象，之后再 patch 模块属性对它无效
    （那正是这三个测试第一次写时会 401 的原因）。
    """

    ADMIN = {'username': 'admin', 'role': 'admin', 'kind': 'user'}

    def _client(self, *, admin: bool):
        from fastapi.testclient import TestClient
        from server import security
        from server.main import app
        if admin:
            app.dependency_overrides[security.require_admin] = lambda: self.ADMIN
            app.dependency_overrides[security.current_user] = lambda: self.ADMIN
        return TestClient(app)

    def setUp(self):
        self._added = []

    def tearDown(self):
        from server.main import app
        app.dependency_overrides.clear()

    def test_endpoint_requires_admin(self):
        """未登录必须被拒（导出会把明文交给调用方，属敏感操作）。"""
        client = self._client(admin=False)
        resp = client.post('/api/keys/export', json={'client': 'zcode', 'token': TOKEN})
        self.assertIn(resp.status_code, (401, 403))

    def test_admin_can_export_and_audit_is_written(self):
        client = self._client(admin=True)
        with mock.patch('server.security.audit') as audit, \
             mock.patch('server.keysvc.resolve', return_value={'prefix': 'wbk_abc12345',
                                                              'realm': 'cn', 'name': 'wb'}):
            resp = client.post('/api/keys/export', json={
                'client': 'zcode', 'token': TOKEN, 'base_url': 'http://panel.local',
                'models': ['glm-5.2']})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body['client'], 'zcode')
        self.assertEqual(body['base_url'], 'http://panel.local/v1')
        self.assertEqual(body['models'], ['glm-5.2'])
        rule = body['provider']['providerRule']
        self.assertEqual(rule['config']['personalModelIds'], ['cn:glm-5.2'])
        self.assertTrue(audit.called, '导出必须写审计')
        # 审计内容里不能出现密钥本身
        for call in audit.call_args_list:
            self.assertNotIn(TOKEN, str(call))

    def test_bad_client_is_400(self):
        client = self._client(admin=True)
        resp = client.post('/api/keys/export',
                           json={'client': 'vscode', 'token': TOKEN})
        self.assertEqual(resp.status_code, 400)

    def test_ccswitch_without_app_is_400(self):
        client = self._client(admin=True)
        with mock.patch('server.keysvc.resolve', return_value=None):
            resp = client.post('/api/keys/export', json={
                'client': 'ccswitch', 'token': TOKEN, 'models': ['a']})
        self.assertEqual(resp.status_code, 400)

    def test_ccswitch_claude_export_ok(self):
        """cc-switch 指定 app 后正常产出（覆盖 app 分支）。"""
        client = self._client(admin=True)
        with mock.patch('server.security.audit'), \
             mock.patch('server.keysvc.resolve', return_value=None):
            resp = client.post('/api/keys/export', json={
                'client': 'ccswitch', 'app': 'claude', 'token': TOKEN,
                'base_url': 'http://p', 'models': ['glm-5.2']})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body['app_type'], 'claude')
        self.assertEqual(body['settings_config']['env']['ANTHROPIC_AUTH_TOKEN'], TOKEN)


if __name__ == '__main__':
    unittest.main()

"""本机一键导入：写 cc-switch / ZCode 配置的边界（纯逻辑 + 端点）。

这是整个特性里**唯一会写用户数据**的一步，所以测试的重点不是"能写进去"，
而是"什么情况下必须不写"：

1. **三重门**：开关默认关、非回环来源拒绝、客户端运行中拒绝。少任何一道，
   用户都可能在不知情的情况下被改坏本机配置。
2. **不猜无法验证的开关**：cc-switch 的 `endpointAutoSelect` 语义未公开，
   我们不写它——猜错会让流量被悄悄改到别的地址上。
3. **备份先于写入**：任何一次成功写入之前都必须存在一份可回滚的副本。
4. **upsert 而非追加**：同一把密钥重复导入只能更新同一行 / 同一条规则，
   否则客户端里会堆出一串同名供应商。
5. **不碰用户自己的东西**：合并只动我们那一条，其它供应商、providerOrder
   顺序、schemaVersion、未知字段一律原样保留。
6. **明文不进审计**：审计里只能出现密钥前缀。

测试全部在临时目录里跑（`WB_CCSWITCH_DIR` / `WB_ZCODE_DIR` 覆盖路径），
不碰本机真实的 cc-switch / ZCode 数据。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import keyexport, keyimport, modelcatalog  # noqa: E402

TOKEN = 'wbk_test_token_for_import_only'
PREFIX = 'wbk_abc12345'

# 与真实库一致的关键约束（provider id 用复合主键，meta 有默认值）。
_CCSWITCH_DDL = """
CREATE TABLE providers (
    id TEXT NOT NULL, app_type TEXT NOT NULL, name TEXT NOT NULL,
    settings_config TEXT NOT NULL, website_url TEXT, category TEXT,
    created_at INTEGER, sort_index INTEGER, notes TEXT, icon TEXT,
    icon_color TEXT, meta TEXT NOT NULL DEFAULT '{}',
    is_current BOOLEAN NOT NULL DEFAULT 0,
    in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
    cost_multiplier TEXT NOT NULL DEFAULT '1.0',
    PRIMARY KEY (id, app_type));
CREATE TABLE provider_endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id TEXT NOT NULL,
    app_type TEXT NOT NULL, url TEXT NOT NULL, added_at INTEGER);
"""

# 故意留一个"别人的供应商"和一个未知字段，验证合并不会顺手清理用户的东西。
_ZCODE_SEED = {
    'schemaVersion': 1,
    'someUnknownTopKey': {'keep': 'me'},
    'config': {
        'providerOrder': ['other-provider', 'new-provider'],
        'providerConfigRules': {'providerRules': [
            {'providerId': 'other-provider', 'providerName': '别人的',
             'config': {'access': {'type': 'api-key', 'apiKey': 'sk-other'},
                        'personalModelIds': ['x'], 'modelOrder': ['x']}},
        ]},
        'modelConfigRules': {'providerModelRules': [
            {'modelId': 'x', 'providerId': 'other-provider',
             'config': {'properties': {}}},
        ]},
    },
}


class TempClientDirs(unittest.TestCase):
    """所有用例的基类：把两个客户端目录指向临时目录。"""

    # 子类设 False 可拿回**真实的**进程探测（用来验证"未知客户端"之类
    # 只有在真函数里才会走到的分支）。
    patch_running = True
    # 同理：定位客户端可执行文件、以及"本机是否注册了 ccswitch:// 协议"
    # 都会读**跑测试这台机器**的进程与注册表。不隔离的话，用例结果会随
    # "开发机上装没装 cc-switch"变化——本机真装了 cc-switch 且它正好在跑，
    # 于是 status 里会突然多出一个 exe、导入会悄悄改走深链。
    patch_locate = True
    patch_protocol = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cc_dir = self.root / 'cc-switch'
        self.zc_dir = self.root / 'zcode-v2'
        self.cc_dir.mkdir()
        self.zc_dir.mkdir()
        self.addCleanup(self._tmp.cleanup)
        env = {'WB_CCSWITCH_DIR': str(self.cc_dir), 'WB_ZCODE_DIR': str(self.zc_dir),
               # 检测到的路径缓存也放到临时目录，别污染（或读到）本机真实的缓存
               'WB_CLIENT_PATHS_FILE': str(self.root / 'client_paths.json')}
        for k, v in env.items():
            self.addCleanup(os.environ.pop, k, None)
            os.environ[k] = v
        # 默认"未运行"；需要其它状态的用例自己覆盖。
        if self.patch_running:
            patcher = mock.patch.object(keyimport, 'is_running', return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)
        if self.patch_locate:
            patcher = mock.patch.object(keyimport, '_locate',
                                        return_value=(None, ''))
            patcher.start()
            self.addCleanup(patcher.stop)
        if self.patch_protocol:
            patcher = mock.patch.object(keyimport, 'protocol_handler',
                                        return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ── 造数据 ──────────────────────────────────────────────────────
    def make_ccswitch(self) -> Path:
        path = self.cc_dir / 'cc-switch.db'
        con = sqlite3.connect(str(path))
        con.executescript(_CCSWITCH_DDL)
        # 一组"存量"数据：同 app_type 的当前项，以及一个别的 app_type。
        con.execute("INSERT INTO providers (id, app_type, name, settings_config,"
                    " is_current, category) VALUES (?,?,?,?,?,?)",
                    ('claude-old', 'claude', '旧的', '{}', 1, 'custom'))
        con.execute("INSERT INTO providers (id, app_type, name, settings_config,"
                    " is_current, category) VALUES (?,?,?,?,?,?)",
                    ('codex-old', 'codex', '旧的 codex', '{}', 1, 'custom'))
        con.commit()
        con.close()
        return path

    def make_zcode(self) -> Path:
        path = self.zc_dir / 'provider_config.json'
        path.write_text(json.dumps(_ZCODE_SEED, ensure_ascii=False, indent=2),
                        encoding='utf-8')
        return path

    def cc_settings(self, app='claude', base_url='http://h/v1'):
        return keyexport.to_ccswitch(token=TOKEN, base_url=base_url, app=app,
                                     name='WB', models=['glm-5.2'], realm='cn')

    def zc_fragment(self, provider_id='workbuddy-' + PREFIX, models=('glm-5.2',)):
        return keyexport.to_zcode(token=TOKEN, base_url='http://h/v1', name='WB',
                                  provider_id=provider_id, models=list(models),
                                  realm='cn')

    def cc_row(self, provider_id, app):
        con = sqlite3.connect(str(self.cc_dir / 'cc-switch.db'))
        try:
            return con.execute(
                'SELECT name, settings_config, is_current, meta, sort_index, '
                'category FROM providers WHERE id = ? AND app_type = ?',
                (provider_id, app)).fetchone()
        finally:
            con.close()


# ── 路径与检测 ──────────────────────────────────────────────────────────

class PathsTests(TempClientDirs):

    # 这条用例要验证「未知客户端会被拒」，必须走真实的 is_running
    patch_running = False

    def test_env_override_points_at_temp_dirs(self):
        self.assertEqual(keyimport.target_path('ccswitch'),
                         self.cc_dir / 'cc-switch.db')
        self.assertEqual(keyimport.target_path('zcode'),
                         self.zc_dir / 'provider_config.json')

    def test_unknown_client_rejected(self):
        with self.assertRaises(ValueError):
            keyimport.target_path('vscode')
        with self.assertRaises(ValueError):
            keyimport.is_running('vscode')


class StatusTests(TempClientDirs):

    def test_not_installed(self):
        st = keyimport.status('zcode')
        self.assertFalse(st['installed'])
        self.assertFalse(st['importable'])
        self.assertEqual(st['reason'], 'not_installed')

    def test_ready_to_import(self):
        self.make_ccswitch()
        st = keyimport.status('ccswitch')
        self.assertTrue(st['installed'])
        self.assertTrue(st['importable'])
        self.assertEqual(st['reason'], '')

    def test_running_blocks_import(self):
        """运行中是明确拒绝，理由要能指到"先退出客户端"这一步。"""
        self.make_ccswitch()
        with mock.patch.object(keyimport, 'is_running', return_value=True):
            st = keyimport.status('ccswitch')
        self.assertFalse(st['importable'])
        self.assertEqual(st['reason'], 'client_running')

    def test_unknown_detection_fails_closed(self):
        """探测不到就当作不能写：两种误判的代价不对称（漏判会写坏配置）。"""
        self.make_ccswitch()
        with mock.patch.object(keyimport, 'is_running', return_value=None):
            st = keyimport.status('ccswitch')
        self.assertFalse(st['importable'])
        self.assertEqual(st['reason'], 'cannot_detect')


class WriteGuardTests(TempClientDirs):

    def test_missing_client_refused_with_actionable_message(self):
        with self.assertRaises(keyimport.ImportRefused) as ctx:
            keyimport.import_ccswitch(settings_config=self.cc_settings(),
                                      app='claude', name='WB', provider_id='p')
        self.assertEqual(ctx.exception.reason, 'not_installed')
        self.assertIn('导出配置', str(ctx.exception))   # 给出可行的替代路径

    def test_running_client_refused(self):
        self.make_ccswitch()
        with mock.patch.object(keyimport, 'is_running', return_value=True):
            with self.assertRaises(keyimport.ImportRefused) as ctx:
                keyimport.import_ccswitch(settings_config=self.cc_settings(),
                                          app='claude', name='WB', provider_id='p')
        self.assertEqual(ctx.exception.reason, 'client_running')

    def test_unknown_detection_refused(self):
        self.make_ccswitch()
        with mock.patch.object(keyimport, 'is_running', return_value=None):
            with self.assertRaises(keyimport.ImportRefused) as ctx:
                keyimport.import_ccswitch(settings_config=self.cc_settings(),
                                          app='claude', name='WB', provider_id='p')
        self.assertEqual(ctx.exception.reason, 'cannot_detect')

    def test_bad_app_rejected(self):
        self.make_ccswitch()
        with self.assertRaises(ValueError):
            keyimport.import_ccswitch(settings_config=self.cc_settings(),
                                      app='vscode', name='WB', provider_id='p')


# ── cc-switch ──────────────────────────────────────────────────────────

class CCSwitchWriteTests(TempClientDirs):

    def setUp(self):
        super().setUp()
        self.make_ccswitch()
        self.pid = keyimport.ccswitch_provider_id(PREFIX, 'claude')

    def test_creates_provider_and_backup(self):
        out = keyimport.import_ccswitch(
            settings_config=self.cc_settings(), app='claude', name='WorkBuddy k',
            provider_id=self.pid, endpoint_url='http://h/v1')
        self.assertEqual(out['action'], 'created')
        row = self.cc_row(self.pid, 'claude')
        self.assertEqual(row[0], 'WorkBuddy k')
        cfg = json.loads(row[1])
        # claude 的 base url 是**根地址**（SDK 自己拼 /v1/messages）
        self.assertEqual(cfg['env']['ANTHROPIC_BASE_URL'], 'http://h/')
        self.assertEqual(cfg['env']['ANTHROPIC_AUTH_TOKEN'], TOKEN)
        self.assertEqual(json.loads(row[3])['apiFormat'], 'anthropic')
        # sort_index 留 NULL：cc-switch 自己新增的供应商就是这个形态
        self.assertIsNone(row[4])
        self.assertEqual(row[5], 'custom')
        # 未要求设当前，就不能动别人
        self.assertEqual(row[2], 0)
        self.assertEqual(self.cc_row('claude-old', 'claude')[2], 1)
        self.assertTrue(Path(out['backup']).is_file(), '写入前必须先备份')

    def test_meta_records_api_format_per_app(self):
        keyimport.import_ccswitch(settings_config=self.cc_settings(app='codex'),
                                  app='codex', name='WB codex',
                                  provider_id=self.pid, endpoint_url='http://h/v1')
        self.assertEqual(json.loads(self.cc_row(self.pid, 'codex')[3])['apiFormat'],
                         'openai_responses')

    def test_reimport_updates_instead_of_appending(self):
        keyimport.import_ccswitch(settings_config=self.cc_settings(), app='claude',
                                  name='first', provider_id=self.pid)
        out = keyimport.import_ccswitch(settings_config=self.cc_settings(),
                                        app='claude', name='second',
                                        provider_id=self.pid)
        self.assertEqual(out['action'], 'updated')
        self.assertEqual(self.cc_row(self.pid, 'claude')[0], 'second')
        con = sqlite3.connect(str(self.cc_dir / 'cc-switch.db'))
        try:
            n = con.execute('SELECT COUNT(*) FROM providers WHERE id = ?',
                            (self.pid,)).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 1, '重复导入不能堆出多行同名供应商')

    def test_set_current_flips_only_same_app_type(self):
        keyimport.import_ccswitch(settings_config=self.cc_settings(), app='claude',
                                  name='WB', provider_id=self.pid,
                                  set_current=True)
        self.assertEqual(self.cc_row(self.pid, 'claude')[2], 1)
        self.assertEqual(self.cc_row('claude-old', 'claude')[2], 0,
                         '同 app_type 的旧当前项必须让位，否则出现两个"当前"')
        self.assertEqual(self.cc_row('codex-old', 'codex')[2], 1,
                         '别的 app_type 不该被动到')

    def test_endpoint_row_is_upserted(self):
        keyimport.import_ccswitch(settings_config=self.cc_settings(), app='claude',
                                  name='WB', provider_id=self.pid,
                                  endpoint_url='http://h/v1')
        keyimport.import_ccswitch(settings_config=self.cc_settings(), app='claude',
                                  name='WB', provider_id=self.pid,
                                  endpoint_url='http://h/v1')
        con = sqlite3.connect(str(self.cc_dir / 'cc-switch.db'))
        try:
            rows = con.execute('SELECT url FROM provider_endpoints WHERE '
                               'provider_id = ? AND app_type = ?',
                               (self.pid, 'claude')).fetchall()
        finally:
            con.close()
        # 端点与 settings 的 base url 同族（这里入参给的就是它）
        self.assertEqual(rows, [('http://h/v1',)])

    def test_provider_id_is_deterministic(self):
        """同一把密钥（前缀相同）必须落到同一个 id，upsert 才成立。"""
        self.assertEqual(keyimport.ccswitch_provider_id(PREFIX, 'claude'),
                         keyimport.ccswitch_provider_id(PREFIX, 'claude'))
        self.assertNotEqual(keyimport.ccswitch_provider_id(PREFIX, 'claude'),
                            keyimport.ccswitch_provider_id(PREFIX, 'codex'))
        self.assertNotEqual(keyimport.ccswitch_provider_id(PREFIX, 'claude'),
                            keyimport.ccswitch_provider_id('wbk_other', 'claude'))


# ── ZCode ──────────────────────────────────────────────────────────────

class ZCodeWriteTests(TempClientDirs):

    def setUp(self):
        super().setUp()
        self.path = self.make_zcode()

    def _doc(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def test_merge_preserves_foreign_configuration(self):
        out = keyimport.import_zcode(fragment=self.zc_fragment())
        self.assertEqual(out['action'], 'created')
        doc = self._doc()
        self.assertEqual(doc['schemaVersion'], 1)
        self.assertEqual(doc['someUnknownTopKey'], {'keep': 'me'},
                         '未知字段必须原样保留')
        rules = doc['config']['providerConfigRules']['providerRules']
        self.assertEqual(rules[0]['providerId'], 'other-provider',
                         '别人的供应商不能被调整或删除')
        self.assertEqual(rules[0]['config']['access']['apiKey'], 'sk-other')
        self.assertEqual(rules[1]['providerId'], 'workbuddy-' + PREFIX)
        self.assertEqual(rules[1]['config']['access']['apiKey'], TOKEN)
        # 已存在的顺序不动，新的追加在末尾
        self.assertEqual(doc['config']['providerOrder'],
                         ['other-provider', 'new-provider', 'workbuddy-' + PREFIX])
        self.assertTrue(Path(out['backup']).is_file(), '写入前必须先备份')

    def test_reimport_is_upsert(self):
        keyimport.import_zcode(fragment=self.zc_fragment(models=('glm-5.2', 'kimi')))
        out = keyimport.import_zcode(fragment=self.zc_fragment(models=('glm-5.2',)))
        self.assertEqual(out['action'], 'updated')
        doc = self._doc()
        rules = doc['config']['providerConfigRules']['providerRules']
        ours = [r for r in rules if r['providerId'] == 'workbuddy-' + PREFIX]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]['config']['personalModelIds'], ['cn:glm-5.2'])
        self.assertEqual(doc['config']['providerOrder'].count('workbuddy-' + PREFIX), 1)

    def test_stale_model_rules_are_dropped_but_only_ours(self):
        """模型清单变小后，旧规则要清掉——否则文件里留着永远不生效的死规则。"""
        keyimport.import_zcode(fragment=self.zc_fragment(models=('glm-5.2', 'kimi')))
        keyimport.import_zcode(fragment=self.zc_fragment(models=('glm-5.2',)))
        ids = {(r['modelId'], r['providerId'])
               for r in self._doc()['config']['modelConfigRules']['providerModelRules']}
        self.assertIn(('cn:glm-5.2', 'workbuddy-' + PREFIX), ids)
        self.assertNotIn(('cn:kimi', 'workbuddy-' + PREFIX), ids)
        self.assertIn(('x', 'other-provider'), ids)      # 别人的不动

    def test_overlapping_reimport_does_not_duplicate_model_rules(self):
        """回归用例：重叠的 modelId 曾被「保留旧的 + 又追加新的」写成两条相同规则。

        真实数据里踩到过——44 个模型的清单里有 1 个与上次重叠，缩量到 3 个后
        规则总数是 65（应为 64）。同一 (providerId, modelId) 出现两条，
        客户端按哪条生效是不确定的。
        """
        keyimport.import_zcode(fragment=self.zc_fragment(models=('glm-5.2', 'kimi')))
        keyimport.import_zcode(fragment=self.zc_fragment(models=('glm-5.2', 'other')))
        rules = [r for r in
                 self._doc()['config']['modelConfigRules']['providerModelRules']
                 if r['providerId'] == 'workbuddy-' + PREFIX]
        ids = sorted(r['modelId'] for r in rules)
        self.assertEqual(ids, ['cn:glm-5.2', 'cn:other'])
        self.assertEqual(len(ids), len(set(ids)), '同一 modelId 不能出现两条规则')

    def test_set_current_moves_provider_to_front(self):
        keyimport.import_zcode(fragment=self.zc_fragment(), set_current=True)
        self.assertEqual(self._doc()['config']['providerOrder'][0],
                         'workbuddy-' + PREFIX)

    def test_broken_json_refused_and_file_untouched(self):
        original = self.path.read_text(encoding='utf-8')
        self.path.write_text('{ this is not json', encoding='utf-8')
        with self.assertRaises(keyimport.ImportRefused) as ctx:
            keyimport.import_zcode(fragment=self.zc_fragment())
        self.assertEqual(ctx.exception.reason, 'bad_format')
        # 拒绝时不能留下半截文件
        self.assertEqual(self.path.read_text(encoding='utf-8'), '{ this is not json')
        self.assertTrue(original)

    def test_fragment_without_provider_id_rejected(self):
        with self.assertRaises(ValueError):
            keyimport.import_zcode(fragment={'providerRule': {'providerName': 'x'}})


# ── 端点 ────────────────────────────────────────────────────────────────

class ImportEndpointTests(TempClientDirs):
    """端点的三重门：开关、回环来源、客户端状态。

    用 `app.dependency_overrides` 而不是 `mock.patch`（理由同 test_key_export）。
    `client_ip` 被 patch：TestClient 的对端主机名是 "testclient"，不是回环地址，
    不 patch 就永远只能测到"拒绝"那一半。
    """

    ADMIN = {'username': 'admin', 'role': 'admin', 'kind': 'user'}

    def _client(self):
        from fastapi.testclient import TestClient
        from server import security
        from server.main import app
        app.dependency_overrides[security.require_admin] = lambda: self.ADMIN
        app.dependency_overrides[security.current_user] = lambda: self.ADMIN
        self.addCleanup(app.dependency_overrides.clear)
        return TestClient(app)

    def _payload(self, **extra):
        body = {'client': 'ccswitch', 'app': 'claude', 'token': TOKEN,
                'base_url': 'http://h', 'models': ['glm-5.2']}
        body.update(extra)
        return body

    def _enable(self, enabled=True):
        patcher = mock.patch('server.config.LOCAL_IMPORT_ENABLED', enabled)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _loopback(self, value=True):
        patcher = mock.patch('server.routers.keys.client_ip',
                             return_value='127.0.0.1' if value else '10.0.0.9')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disabled_by_default_is_403(self):
        """默认关闭：写用户桌面应用数据必须是显式选择。"""
        self.make_ccswitch()
        with mock.patch('server.config.LOCAL_IMPORT_ENABLED', False):
            resp = self._client().post('/api/keys/import-local', json=self._payload())
        self.assertEqual(resp.status_code, 403)
        self.assertIn('WB_LOCAL_IMPORT', resp.json()['detail'])

    def test_remote_caller_is_403(self):
        """面板部署在服务器上时，这条路必须在写盘之前就被拦下。"""
        self.make_ccswitch()
        self._enable()
        self._loopback(False)
        resp = self._client().post('/api/keys/import-local', json=self._payload())
        self.assertEqual(resp.status_code, 403)
        self.assertIn('本机', resp.json()['detail'])

    def test_requires_admin(self):
        from fastapi.testclient import TestClient
        from server.main import app
        self._enable()
        resp = TestClient(app).post('/api/keys/import-local', json=self._payload())
        self.assertIn(resp.status_code, (401, 403))

    def test_running_client_is_409(self):
        self.make_ccswitch()
        self._enable()
        self._loopback()
        with mock.patch.object(keyimport, 'is_running', return_value=True):
            resp = self._client().post('/api/keys/import-local', json=self._payload())
        self.assertEqual(resp.status_code, 409)
        self.assertIn('退出', resp.json()['detail'])

    def test_missing_client_is_404(self):
        self._enable()
        self._loopback()
        resp = self._client().post('/api/keys/import-local', json=self._payload())
        self.assertEqual(resp.status_code, 404)

    def test_ccswitch_import_writes_and_audits_without_token(self):
        self.make_ccswitch()
        self._enable()
        self._loopback()
        with mock.patch('server.security.audit') as audit, \
             mock.patch('server.keysvc.resolve',
                        return_value={'prefix': PREFIX, 'realm': 'cn', 'name': 'k'}):
            resp = self._client().post('/api/keys/import-local',
                                       json=self._payload(set_current=True))
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body['action'], 'created')
        self.assertTrue(Path(body['backup']).is_file())
        # 响应体里不能出现明文（配置已落盘，没必要再回显一次）
        self.assertNotIn(TOKEN, resp.text)
        self.assertTrue(audit.called, '写本机凭据文件必须写审计')
        for call in audit.call_args_list:
            self.assertNotIn(TOKEN, str(call))
            self.assertIn(PREFIX, str(call))

    def test_zcode_import_writes_file(self):
        self.make_zcode()
        self._enable()
        self._loopback()
        with mock.patch('server.security.audit'), \
             mock.patch('server.keysvc.resolve',
                        return_value={'prefix': PREFIX, 'realm': 'cn', 'name': 'k'}):
            resp = self._client().post('/api/keys/import-local',
                                       json=self._payload(client='zcode', app=''))
        self.assertEqual(resp.status_code, 200, resp.text)
        doc = json.loads((self.zc_dir / 'provider_config.json')
                         .read_text(encoding='utf-8'))
        ids = [r['providerId']
               for r in doc['config']['providerConfigRules']['providerRules']]
        self.assertIn('workbuddy-' + PREFIX, ids)
        self.assertIn('other-provider', ids)

    def test_status_endpoint_is_read_only_and_reports_clients(self):
        self.make_ccswitch()
        self._enable()
        self._loopback()
        resp = self._client().get('/api/keys/import-local/status')
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body['enabled'])
        self.assertTrue(body['local_caller'])
        by_name = {c['client']: c for c in body['clients']}
        self.assertTrue(by_name['ccswitch']['importable'])
        self.assertFalse(by_name['zcode']['installed'])

    def test_status_reports_disabled_without_error(self):
        """开关关着也要 200：界面要能据此隐藏这一块，报错会被当成故障。"""
        with mock.patch('server.config.LOCAL_IMPORT_ENABLED', False):
            resp = self._client().get('/api/keys/import-local/status')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()['enabled'])
        self.assertEqual(resp.json()['clients'], [])
        # 关着的时候不该去探测客户端
        self.assertNotIn('target', resp.text)

    def test_status_exposes_detection_fields(self):
        """界面要能**提前**说清"会先替你关掉客户端"，所以状态里得有这几项。"""
        self.make_ccswitch()
        self._enable()
        self._loopback()
        exe = self.root / 'cc-switch.exe'
        exe.write_bytes(b'MZ')
        with mock.patch.object(keyimport, 'is_running', return_value=True), \
             mock.patch.object(keyimport, '_locate',
                               return_value=(exe, 'protocol')):
            resp = self._client().get('/api/keys/import-local/status')
        cc = {c['client']: c for c in resp.json()['clients']}['ccswitch']
        self.assertEqual(cc['exe'], str(exe))
        self.assertEqual(cc['exe_source'], 'protocol')
        self.assertTrue(cc['needs_close'])
        # 本用例把协议也 patch 掉了，所以看不出深链能力（默认不可用）
        self.assertFalse(cc['deeplink'])

    # ── 深链 vs 直写 ────────────────────────────────────────────────
    def _with_protocol(self, exe=None):
        exe = exe or (self.root / 'cc-switch.exe')
        exe.write_bytes(b'MZ')
        patcher = mock.patch.object(keyimport, 'protocol_handler', return_value=exe)
        patcher.start()
        self.addCleanup(patcher.stop)
        return exe

    def _resolve(self):
        patcher = mock.patch('server.keysvc.resolve',
                             return_value={'prefix': PREFIX, 'realm': 'cn', 'name': 'k'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_auto_prefers_deeplink_and_leaves_the_database_alone(self):
        """官方深链优先：不写库、不备份、不关客户端——只有一条链接交给系统。"""
        db = self.make_ccswitch()
        before = db.read_bytes()
        self._enable()
        self._loopback()
        self._resolve()
        self._with_protocol()
        with mock.patch.object(keyimport, 'open_url', return_value=True) as opened, \
             mock.patch('server.security.audit'), \
             mock.patch.object(keyimport, 'stop') as stop:
            resp = self._client().post('/api/keys/import-local', json=self._payload())
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body['method'], 'deeplink')
        self.assertEqual(body['action'], 'handed-off')
        self.assertIsNone(body['lifecycle'])
        url = opened.call_args.args[0]
        self.assertIn('ccswitch://v1/import?', url)
        self.assertIn('app=claude', url)
        self.assertEqual(db.read_bytes(), before, '深链不该动它的数据库')
        self.assertFalse(list(self.cc_dir.glob('*.bak')), '深链不该产生备份')
        self.assertFalse(stop.called, '深链不需要写盘窗口，不该关客户端')
        # 链接里有明文密钥（交给客户端用），但响应体里绝不能有
        self.assertIn(TOKEN, url)
        self.assertNotIn(TOKEN, resp.text)

    def test_direct_mode_forces_file_write(self):
        db = self.make_ccswitch()
        self._enable()
        self._loopback()
        self._resolve()
        self._with_protocol()
        with mock.patch.object(keyimport, 'open_url') as opened, \
             mock.patch('server.security.audit'):
            resp = self._client().post(
                '/api/keys/import-local',
                json=self._payload(mode='direct', set_current=True))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['method'], 'direct')
        self.assertEqual(resp.json()['action'], 'created')
        self.assertFalse(opened.called)
        self.assertIsNotNone(self.cc_row(
            keyimport.ccswitch_provider_id(PREFIX, 'claude'), 'claude'))

    def test_explicit_deeplink_without_protocol_is_409(self):
        self.make_ccswitch()
        self._enable()
        self._loopback()
        resp = self._client().post('/api/keys/import-local',
                                   json=self._payload(mode='deeplink'))
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertIn('深链', resp.json()['detail'])

    def test_close_running_imports_and_brings_the_client_back(self):
        """用户要的"一键"：客户端开着也别让他手动退——我们关、写、再拉起来。"""
        self.make_ccswitch()
        self._enable()
        self._loopback()
        self._resolve()
        state = {'running': True}

        def fake_stop(_client):
            state['running'] = False
            return {'stopped': True, 'forced': False}

        exe = self.root / 'cc-switch.exe'
        exe.write_bytes(b'MZ')
        with mock.patch.object(keyimport, 'is_running',
                               side_effect=lambda _c: state['running']), \
             mock.patch.object(keyimport, '_locate', return_value=(exe, 'protocol')), \
             mock.patch.object(keyimport, 'stop', side_effect=fake_stop), \
             mock.patch.object(keyimport, 'reopen', return_value=True) as reopen, \
             mock.patch('server.security.audit'):
            resp = self._client().post(
                '/api/keys/import-local',
                json=self._payload(mode='direct', close_running=True))
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body['lifecycle']['stopped'], True)
        self.assertEqual(body['lifecycle']['restarted'], True)
        self.assertEqual(body['lifecycle']['exe'], str(exe))
        self.assertTrue(reopen.called)
        self.assertIsNotNone(self.cc_row(
            keyimport.ccswitch_provider_id(PREFIX, 'claude'), 'claude'))

    def test_bad_mode_is_400(self):
        self.make_ccswitch()
        self._enable()
        self._loopback()
        resp = self._client().post('/api/keys/import-local',
                                   json=self._payload(mode='teleport'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('mode', resp.json()['detail'])

    # ── 自动检测 ────────────────────────────────────────────────────
    def test_detect_endpoint_returns_what_it_found(self):
        self._enable()
        self._loopback()
        exe = self.root / 'cc-switch.exe'
        exe.write_bytes(b'MZ')
        with mock.patch.object(keyimport, 'detect',
                               return_value={'client': 'ccswitch', 'found': True,
                                             'exe': str(exe), 'source': 'scan'}) as det:
            resp = self._client().get('/api/keys/import-local/detect')
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['exe'], str(exe))
        self.assertEqual(det.call_args.kwargs, {'deep': True})

    def test_detect_is_gated_like_the_import(self):
        """检测也要过同样的两道门：开关 + 回环来源。"""
        self._enable()
        self._loopback(False)
        self.assertEqual(
            self._client().get('/api/keys/import-local/detect').status_code, 403)
        self._loopback(True)
        with mock.patch('server.config.LOCAL_IMPORT_ENABLED', False):
            self.assertEqual(
                self._client().get('/api/keys/import-local/detect').status_code, 403)


class ModelListResolutionTests(TempClientDirs):
    """导出/导入取模型清单：缓存 → 实时拉取 → 可照做的 409。

    为什么值得一组专门的用例（实测踩到的坑）：模型清单缓存是**进程内内存**，
    面板一重启就空了，而只有打开「模型中心」页才会去拉。原先导出**只读缓存**，
    于是「重启 → 建密钥 → 点导出」这条最普通的路径必然 400
    「模型清单为空且未指定默认模型」——报出的原因与密钥毫无关系，用户根本无法
    自查（实测有人因此把密钥删掉重建，白折腾一遍）。
    """

    ADMIN = {'username': 'admin', 'role': 'admin', 'kind': 'user'}
    FETCHED = {'models': [], 'source': 'tencent', 'source_label': '', 'via': '',
               'errors': []}

    def _client(self):
        from fastapi.testclient import TestClient
        from server import security
        from server.main import app
        app.dependency_overrides[security.require_admin] = lambda: self.ADMIN
        app.dependency_overrides[security.current_user] = lambda: self.ADMIN
        self.addCleanup(app.dependency_overrides.clear)
        return TestClient(app)

    def _resolve(self, realm: str = 'cn'):
        patcher = mock.patch('server.keysvc.resolve',
                             return_value={'prefix': PREFIX, 'realm': realm, 'name': 'k'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cache(self, *values):
        """给 `cached_ids` 定量身返回值：不传 = 永远 None（缓存始终空）。

        传多个值 = 按调用次序依次返回（`_export_models` 冷缓存时会调用两次：
        拉取前一次、拉取后一次），用来模拟「冷 → 热」。
        """
        if values:
            patcher = mock.patch.object(modelcatalog, 'cached_ids',
                                        side_effect=list(values))
        else:
            patcher = mock.patch.object(modelcatalog, 'cached_ids', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fetch(self, result):
        patcher = mock.patch.object(modelcatalog, 'fetch_ids_blocking',
                                    return_value=result)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def _enable(self, enabled=True):
        patcher = mock.patch('server.config.LOCAL_IMPORT_ENABLED', enabled)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _loopback(self, value=True):
        patcher = mock.patch('server.routers.keys.client_ip',
                             return_value='127.0.0.1' if value else '10.0.0.9')
        patcher.start()
        self.addCleanup(patcher.stop)

    def _export(self, models=None, realm='cn', client='ccswitch', app='claude'):
        """打一次 `/api/keys/export`。`models=None` 用「填了白名单」，`[]` 用「没填」。"""
        self._resolve(realm)
        body = {'client': client, 'token': TOKEN, 'base_url': 'http://h',
                'models': ['glm-5.2'] if models is None else models}
        if client == 'ccswitch':
            body['app'] = app
        with mock.patch('server.security.audit'):
            return self._client().post('/api/keys/export', json=body)

    # ── 缓存命中：不发网络 ──────────────────────────────────────────
    def test_warm_cache_is_used_without_any_network(self):
        """这条路径原本就不联网——修完冷缓存后也必须保持不联网。"""
        self._cache({'glm-5.2', 'hy3'})
        fetch = self._fetch(self.FETCHED)
        resp = self._export(models=[])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['models'], ['glm-5.2', 'hy3'])
        self.assertFalse(fetch.called, '缓存有值就不该去拉上游')

    # ── 冷缓存：拉一次就能成功（修的就是这个）────────────────────────
    def test_cold_cache_fetches_once_and_succeeds(self):
        self._cache(None, {'glm-5.2', 'hy3'})
        fetch = self._fetch(self.FETCHED)
        resp = self._export(models=[])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['models'], ['glm-5.2', 'hy3'])
        self.assertEqual(fetch.call_count, 1)

    # ── 拉完还是空：409 + 说清该做什么 ──────────────────────────────
    def test_no_account_message_points_at_accounts_page(self):
        """没有可用账号：用户要去「账号」页，不是去查密钥。"""
        self._cache()
        self._fetch({'models': [], 'source': 'none', 'errors': [
            '没有可用的国内版账号', '上游 /v1/models 失败: 上游返回 401']})
        resp = self._export(models=[])
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()['detail']
        self.assertIn('没有可用的国内版账号', detail)
        self.assertIn('「账号」页', detail)
        # 不能只丢一句术语让人去猜
        self.assertNotIn('未指定默认模型', detail)

    def test_fetch_failure_message_suggests_retry_or_whitelist(self):
        self._cache()
        self._fetch({'models': [], 'source': 'none',
                     'errors': ['上游 /v1/models 失败: 上游返回 401']})
        resp = self._export(models=[])
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()['detail']
        self.assertIn('稍后重试', detail)
        self.assertIn('模型白名单', detail)

    def test_global_realm_says_global(self):
        """版本的措辞要跟着密钥的版本走，否则用户会去加错版本的账号。"""
        self._cache()
        self._fetch({'models': [], 'source': 'none',
                     'errors': ['没有可用的国际版账号']})
        resp = self._export(models=[], realm='global')
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertIn('国际版', resp.json()['detail'])

    # ── 显式白名单：缓存与网络都不碰 ────────────────────────────────
    def test_explicit_models_skip_cache_and_network(self):
        patcher = mock.patch.object(modelcatalog, 'cached_ids')
        cache = patcher.start()
        self.addCleanup(patcher.stop)
        fetch = self._fetch(self.FETCHED)
        resp = self._export(models=['glm-5.2'])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(cache.called)
        self.assertFalse(fetch.called)

    # ── 导入走的是同一条解析路径 ────────────────────────────────────
    def test_import_local_shares_the_same_resolution(self):
        """导入与导出共用 `_config_context`，冷缓存也必须能导入成功。"""
        self.make_ccswitch()
        self._enable()
        self._loopback()
        self._cache(None, {'glm-5.2'})
        self._fetch(self.FETCHED)
        with mock.patch('server.keysvc.resolve',
                        return_value={'prefix': PREFIX, 'realm': 'cn', 'name': 'k'}), \
             mock.patch('server.security.audit'):
            resp = self._client().post('/api/keys/import-local',
                                       json={'client': 'ccswitch', 'app': 'claude',
                                             'token': TOKEN, 'base_url': 'http://h',
                                             'models': []})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['action'], 'created')


class SyncCatalogBridgeTests(unittest.TestCase):
    """`modelcatalog.fetch_ids_blocking`：同步路由里跑一次 async 拉取。

    这一层是"同步路由 + async 服务"的接缝，最容易在别人改动后静默坏掉
    （比如有人把 `config.http_client` 改成共享单例，跨事件循环复用连接池就炸），
    所以两头都测：能跑通、以及"已在事件循环里"时拒绝自建循环。
    """

    def test_runs_coroutine_from_sync_context(self):
        sentinel = {'models': [{'id': 'glm-5.2'}], 'source': 'tencent', 'errors': []}
        with mock.patch.object(modelcatalog, 'catalog',
                               new=mock.AsyncMock(return_value=sentinel)):
            self.assertEqual(modelcatalog.fetch_ids_blocking('cn'), sentinel)

    def test_refuses_to_nest_a_second_loop(self):
        """已在事件循环里时宁可"取不到"，也不能把请求卡死。"""

        async def call():
            return modelcatalog.fetch_ids_blocking('cn')

        with mock.patch.object(modelcatalog, 'catalog',
                               new=mock.AsyncMock(side_effect=AssertionError('不该发网络'))):
            result = asyncio.run(call())
        self.assertEqual(result['models'], [])
        self.assertIn('事件循环', result['errors'][0])


class ClientPathDetectionTests(TempClientDirs):
    """自动检测客户端位置：路径在不同机器上完全不一样，不能写死。

    这组用例把宿主环境彻底挡在外面（`_from_process` / `_from_registry` /
    `protocol_handler` 都 patch 成 None），否则"跑测试这台机器真的装了
    cc-switch、而且注册表里真有 ccswitch:// 协议"会让结果飘。
    """

    patch_locate = False

    def setUp(self):
        super().setUp()
        for name in ('_from_process', '_from_registry'):
            patcher = mock.patch.object(keyimport, name, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake_exe(self, name='cc-switch.exe', sub=''):
        d = self.root / 'install' / sub if sub else self.root / 'install'
        d.mkdir(parents=True, exist_ok=True)
        exe = d / name
        exe.write_bytes(b'MZ')          # 内容无所谓，is_file() 才是判据
        return exe

    # ── 覆盖 / 缓存 ─────────────────────────────────────────────────
    def test_env_override_wins(self):
        exe = self._fake_exe()
        os.environ['WB_CCSWITCH_EXE'] = str(exe)
        self.addCleanup(os.environ.pop, 'WB_CCSWITCH_EXE', None)
        self.assertEqual(keyimport.find_executable('ccswitch'), exe)
        self.assertEqual(keyimport._locate('ccswitch')[1], 'env')

    def test_env_override_pointing_nowhere_is_ignored(self):
        """覆盖值写错时不能"就用它"——拉起来的是个不存在的路径。"""
        os.environ['WB_CCSWITCH_EXE'] = str(self.root / 'nope.exe')
        self.addCleanup(os.environ.pop, 'WB_CCSWITCH_EXE', None)
        self.assertIsNone(keyimport.find_executable('ccswitch'))

    @unittest.skipUnless(sys.platform == 'win32',
                         '协议注册表与 .exe 扫描是 Windows 专属（keyimport._from_registry / '
                         '_scan_respects_depth 在非 Windows 上按设计返回 None）；'
                         'POSIX 侧的契约由 test_posix_detection_never_invents_a_path 钉住')
    def test_detect_remembers_and_cache_is_reused(self):
        """点一次「自动检测」之后就该长期有效（缓存命中，来源标 cache）。"""
        exe = self._fake_exe(sub='cc-switch')
        os.environ['WB_CLIENT_SCAN_ROOTS'] = str(self.root)
        self.addCleanup(os.environ.pop, 'WB_CLIENT_SCAN_ROOTS', None)

        found = keyimport.detect('ccswitch')
        self.assertTrue(found['found'], found)
        self.assertEqual(Path(found['exe']), exe)
        self.assertEqual(found['source'], 'scan')
        # 扫描根拿掉，只剩缓存也不影响定位
        os.environ.pop('WB_CLIENT_SCAN_ROOTS')
        self.assertTrue((self.root / 'client_paths.json').is_file())
        self.assertEqual(keyimport._locate('ccswitch'), (exe, 'cache'))

    def test_stale_cache_entry_is_dropped(self):
        """客户端卸载/搬家后，旧路径不能再认（否则会去拉起不存在的东西）。"""
        exe = self._fake_exe()
        (self.root / 'client_paths.json').write_text(
            json.dumps({'ccswitch': {'exe': str(exe), 'source': 'scan'}}),
            encoding='utf-8')
        self.assertEqual(keyimport.find_executable('ccswitch'), exe)
        exe.unlink()
        self.assertIsNone(keyimport.find_executable('ccswitch'))

    def test_posix_detection_never_invents_a_path(self):
        """非 Windows 上不做猜测：要么给出**真实存在**的路径，要么明确说没有。

        Windows 检测链里的注册表与 .exe 扫描在 POSIX 上不适用（`keyimport.py` 里
        那两处 `sys.platform != 'win32': return None`），但「环境变量 / 常见安装位」
        这两级是跨平台的。这条钉的是**契约**而不是某个具体结果：返回的东西必须真的
        存在，不能凭空拼一个路径出来。
        """
        with mock.patch.object(sys, 'platform', 'linux'):
            got = keyimport.find_executable('ccswitch')
        self.assertTrue(got is None or got.exists(),
                        f'非 Windows 上返回了不存在的路径：{got}')

    @unittest.skipUnless(sys.platform == 'win32',
                         '协议注册表与 .exe 扫描是 Windows 专属（keyimport._from_registry / '
                         '_scan_respects_depth 在非 Windows 上按设计返回 None）；'
                         'POSIX 侧的契约由 test_posix_detection_never_invents_a_path 钉住')
    def test_scan_respects_depth(self):
        """两层深的安装目录要能找到（绿色版常解压在某个盘的根下）。"""
        deep = self._fake_exe(sub='a/b')
        os.environ['WB_CLIENT_SCAN_ROOTS'] = str(self.root)
        self.addCleanup(os.environ.pop, 'WB_CLIENT_SCAN_ROOTS', None)
        self.assertEqual(keyimport._scan('ccswitch'), deep)

    def test_scan_skips_noise_directories(self):
        """不往 node_modules 这类目录里钻：里面可能躺着一堆同名文件。"""
        noise = self.root / 'junk' / 'node_modules' / 'x'
        noise.mkdir(parents=True)
        (noise / 'cc-switch.exe').write_bytes(b'MZ')
        os.environ['WB_CLIENT_SCAN_ROOTS'] = str(self.root / 'junk')
        self.addCleanup(os.environ.pop, 'WB_CLIENT_SCAN_ROOTS', None)
        self.assertIsNone(keyimport._scan('ccswitch'))

    def test_protocol_registration_is_a_trusted_source(self):
        """`ccswitch://` 指向谁，谁就是客户端——客户端没开着时这是最准的来源。"""
        exe = self._fake_exe()
        with mock.patch.object(keyimport, 'protocol_handler', return_value=exe):
            self.assertEqual(keyimport._locate('ccswitch'), (exe, 'protocol'))

    # ── 协议处理器（注册表）────────────────────────────────────────
    def test_protocol_handler_command_is_parsed(self):
        """`"E:\\dir\\cc-switch.exe" "%1"` → 真实路径。"""
        exe = self._fake_exe()
        self.assertEqual(
            keyimport._exe_from_registry_value(f'"{exe}" "%1"', 'cc-switch.exe'),
            exe)
        # 尾随参数（DisplayIcon 常见写法）也要能认出来
        self.assertEqual(
            keyimport._exe_from_registry_value(f'"{exe}",0', 'cc-switch.exe'), exe)

    def test_uninstaller_is_never_mistaken_for_the_client(self):
        """实测踩到：ZCode 的 DisplayIcon 指向 `Uninstall ZCode.exe`。

        真把它当成客户端"拉起来"，用户看到的是卸载向导——所以注册表取值一律
        挡掉 unins/setup/update 这类名字。
        """
        exe = self._fake_exe(name='Uninstall ZCode.exe')
        self.assertIsNone(
            keyimport._exe_from_registry_value(f'"{exe}",0', 'ZCode.exe'))
        # 同名目录下的真客户端仍然认得出来（走 is_dir 分支）
        self.assertEqual(
            keyimport._exe_from_registry_value(str(exe.parent), 'Uninstall ZCode.exe'),
            exe)

    def test_deeplink_supported_follows_protocol_registration(self):
        self.assertFalse(keyimport.deeplink_supported('ccswitch'))
        with mock.patch.object(keyimport, 'protocol_handler',
                               return_value=self._fake_exe()):
            self.assertTrue(keyimport.deeplink_supported('ccswitch'))
            # ZCode 虽然也注册了 zcode://，但协议格式没公开，不能拿去当深链用
            self.assertFalse(keyimport.deeplink_supported('zcode'))


class DeepLinkTests(TempClientDirs):
    """cc-switch 官方深链 `ccswitch://v1/import`（提案 §10 的首选路径）。

    为什么值得单独测：这条链接**含明文密钥**并且会被交给另一个进程，
    参数拼错/多拼一个字段，代价是用户的密钥泄露或一条坏配置进入客户端。
    """

    patch_locate = False

    def setUp(self):
        super().setUp()
        exe = self.root / 'cc-switch.exe'
        exe.write_bytes(b'MZ')
        self.exe = exe
        patcher = mock.patch.object(keyimport, 'protocol_handler', return_value=exe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _settings(self, app='claude'):
        return keyexport.to_ccswitch(
            token=TOKEN, base_url='http://h/v1', app=app, name='WB 22',
            models=['glm-5.2'], realm='cn')

    def _params(self, url):
        import urllib.parse
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts.scheme, 'ccswitch')
        self.assertEqual(parts.netloc, 'v1')
        self.assertEqual(parts.path, '/import')
        return urllib.parse.parse_qs(parts.query)

    def test_claude_link_carries_json_config(self):
        settings = self._settings('claude')
        url = keyimport.build_ccswitch_deeplink(
            app='claude', name='WB 22', settings_config=settings,
            token=TOKEN, base_url='http://h/', model='cn:glm-5.2',
            set_current=True)
        q = self._params(url)
        self.assertEqual(q['resource'], ['provider'])
        self.assertEqual(q['app'], ['claude'])
        self.assertEqual(q['name'], ['WB 22'])
        self.assertEqual(q['endpoint'], ['http://h/'])
        self.assertEqual(q['model'], ['cn:glm-5.2'])
        self.assertEqual(q['apiKey'], [TOKEN])
        self.assertEqual(q['configFormat'], ['json'])
        self.assertEqual(q['enabled'], ['true'])
        # config 是完整配置的 base64（= settings.json 的内容）
        decoded = json.loads(base64.b64decode(q['config'][0]).decode('utf-8'))
        self.assertEqual(decoded, settings)
        self.assertEqual(decoded['env']['ANTHROPIC_BASE_URL'], 'http://h/')

    def test_codex_link_carries_toml_text(self):
        """codex 的 config 是 TOML 文本本身，不是包着它的那个 JSON。"""
        settings = self._settings('codex')
        url = keyimport.build_ccswitch_deeplink(
            app='codex', name='WB 22', settings_config=settings,
            token=TOKEN, base_url='http://h/v1', set_current=False)
        q = self._params(url)
        self.assertEqual(q['configFormat'], ['toml'])
        self.assertEqual(base64.b64decode(q['config'][0]).decode('utf-8'),
                         settings['config'])
        # 没勾"设为当前"就不带 enabled：对 false 的处理文档没写死，不猜
        self.assertNotIn('enabled', q)

    def test_link_does_not_require_a_written_file(self):
        """深链不需要写盘窗口：客户端正在运行、配置库甚至不存在都能导入。"""
        with mock.patch.object(keyimport, 'is_running', return_value=True), \
             mock.patch.object(keyimport, 'open_url', return_value=True) as opened:
            out = keyimport.import_ccswitch_deeplink(
                app='claude', name='WB', settings_config=self._settings(),
                token=TOKEN, base_url='http://h/')
        self.assertEqual(out['action'], 'handed-off')
        self.assertIn('ccswitch://v1/import?', opened.call_args.args[0])
        # 关键：没有落盘、没有备份、也没有动客户端进程
        self.assertEqual(out['target'], '')
        self.assertEqual(out['backup'], '')

    def test_missing_protocol_is_refused_with_alternative(self):
        with mock.patch.object(keyimport, 'protocol_handler', return_value=None):
            with self.assertRaises(keyimport.ImportRefused) as ctx:
                keyimport.import_ccswitch_deeplink(
                    app='claude', name='WB', settings_config=self._settings())
        self.assertEqual(ctx.exception.reason, 'deeplink_unavailable')
        self.assertIn('直接写入', str(ctx.exception))

    def test_open_failure_is_refused(self):
        with mock.patch.object(keyimport, 'open_url', return_value=False):
            with self.assertRaises(keyimport.ImportRefused) as ctx:
                keyimport.import_ccswitch_deeplink(
                    app='claude', name='WB', settings_config=self._settings())
        self.assertEqual(ctx.exception.reason, 'cannot_open')


class ClientLifecycleTests(TempClientDirs):
    """直写路径的"关掉—写完—拉起来"（`client_closed`）。

    这是整个项目里唯一会**终止并启动别的进程**的逻辑，两条底线：
    没授权就不许关；关了就必须（无论中间成功还是抛错）拉起来。
    """

    patch_locate = False

    def setUp(self):
        super().setUp()
        self.make_ccswitch()
        self.exe = self.root / 'cc-switch.exe'
        self.exe.write_bytes(b'MZ')
        patcher = mock.patch.object(keyimport, '_locate',
                                    return_value=(self.exe, 'env'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _running(self, *values):
        """按调用次序给出 is_running 的结果（True,True,False… 这种）。"""
        patcher = mock.patch.object(keyimport, 'is_running',
                                    side_effect=list(values))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_running_without_authorisation_is_refused(self):
        with mock.patch.object(keyimport, 'is_running', return_value=True), \
             mock.patch.object(keyimport, 'stop') as stop:
            with self.assertRaises(keyimport.ImportRefused) as ctx:
                with keyimport.client_closed('ccswitch'):
                    pass
        self.assertEqual(ctx.exception.reason, 'client_running')
        self.assertFalse(stop.called, '没授权就绝不能关用户的客户端')

    def test_unknown_detection_never_stops_anything(self):
        """探测不了就不敢关：`is_running` 返回 None 时不进关闭分支。"""
        with mock.patch.object(keyimport, 'is_running', return_value=None), \
             mock.patch.object(keyimport, 'stop') as stop:
            with keyimport.client_closed('ccswitch', close_running=True) as lc:
                pass
        self.assertFalse(stop.called)
        self.assertFalse(lc.stopped)

    def test_authorised_close_then_reopen(self):
        with mock.patch.object(keyimport, 'is_running', return_value=True), \
             mock.patch.object(keyimport, 'stop',
                               return_value={'stopped': True, 'forced': True}) as stop, \
             mock.patch.object(keyimport, 'reopen', return_value=True) as reopen:
            with keyimport.client_closed('ccswitch', close_running=True) as lc:
                self.assertTrue(lc.stopped)
            stop.assert_called_once_with('ccswitch')
            reopen.assert_called_once_with('ccswitch', self.exe)
        self.assertTrue(lc.as_dict()['restarted'])
        self.assertTrue(lc.as_dict()['forced'])
        self.assertEqual(lc.as_dict()['exe'], str(self.exe))

    def test_reopen_happens_even_when_the_write_blows_up(self):
        """写入失败也不能把用户的客户端留在关闭状态。"""
        with mock.patch.object(keyimport, 'is_running', return_value=True), \
             mock.patch.object(keyimport, 'stop',
                               return_value={'stopped': True, 'forced': False}), \
             mock.patch.object(keyimport, 'reopen', return_value=True) as reopen:
            with self.assertRaises(RuntimeError):
                with keyimport.client_closed('ccswitch', close_running=True):
                    raise RuntimeError('写坏了')
        self.assertTrue(reopen.called)

    def test_reopen_failure_is_reported_not_hidden(self):
        """拉不起来要如实说（否则我们会告诉用户"已重新打开"，而他桌面空空）。"""
        with mock.patch.object(keyimport, 'is_running', return_value=True), \
             mock.patch.object(keyimport, 'stop',
                               return_value={'stopped': True, 'forced': False}), \
             mock.patch.object(keyimport, 'reopen', return_value=False):
            with keyimport.client_closed('ccswitch', close_running=True) as lc:
                pass
        self.assertIs(lc.as_dict()['restarted'], False)

    def test_missing_executable_means_we_do_not_stop(self):
        """定位不到可执行文件就不关：关掉却拉不起来比不改更糟。"""
        with mock.patch.object(keyimport, '_locate', return_value=(None, '')), \
             mock.patch.object(keyimport, 'is_running', return_value=True), \
             mock.patch.object(keyimport, 'stop') as stop:
            with self.assertRaises(keyimport.ImportRefused) as ctx:
                with keyimport.client_closed('ccswitch', close_running=True):
                    pass
        self.assertEqual(ctx.exception.reason, 'cannot_stop')
        self.assertFalse(stop.called)
        self.assertIn('WB_CCSWITCH_EXE', str(ctx.exception))

    def test_write_succeeds_after_the_client_is_gone(self):
        """关掉之后 `_ensure_writable` 的复查必须放行（否则白关一场）。"""
        state = {'running': True}

        def fake_running(_client):
            return state['running']

        def fake_stop(_client):
            state['running'] = False
            return {'stopped': True, 'forced': False}

        with mock.patch.object(keyimport, 'is_running', side_effect=fake_running), \
             mock.patch.object(keyimport, 'stop', side_effect=fake_stop), \
             mock.patch.object(keyimport, 'reopen', return_value=True):
            with keyimport.client_closed('ccswitch', close_running=True):
                out = keyimport.import_ccswitch(
                    settings_config=self.cc_settings(), app='claude', name='WB',
                    provider_id='p')
        self.assertEqual(out['action'], 'created')
        self.assertIsNotNone(self.cc_row('p', 'claude'))

    def test_stop_escalates_to_force_when_graceful_is_ignored(self):
        """拒绝关闭的客户端（最小化到托盘那种）要升级为强杀，而不是干等。

        **两端都验**：`stop()` 按平台选 `_terminate_windows` / `_terminate_posix`
        （同一个 `terminate(names, force=…)` 契约），所以两个都 mock、再按当前平台
        断言被选中的那个。上一版只 mock 了 Windows 那个，Linux CI 上
        `call_args_list` 就是空的——本地 Windows 全绿掩盖了这点（CI 抓到的）。
        """
        for platform in ('win32', 'linux'):
            with self.subTest(platform=platform):
                seq = [True, True, False]  # 起：在跑；优雅关闭后还在；强杀后才没了
                with mock.patch.object(sys, 'platform', platform), \
                     mock.patch.object(keyimport, 'is_running',
                                       side_effect=lambda _c: seq.pop(0) if seq else False), \
                     mock.patch.object(keyimport, '_terminate_windows') as win, \
                     mock.patch.object(keyimport, '_terminate_posix') as posix, \
                     mock.patch.object(keyimport, '_POLL', 0), \
                     mock.patch.object(keyimport, '_STOP_GRACE', 0.0):
                    out = keyimport.stop('ccswitch')
                self.assertEqual(out, {'stopped': True, 'forced': True})
                terminate = win if platform == 'win32' else posix
                # 先请它自己退，再强杀——顺序不能反
                self.assertEqual([c.kwargs['force'] for c in terminate.call_args_list],
                                 [False, True])
                self.assertEqual(terminate.call_args_list[0].args[0],
                                 keyimport.PROCESS_NAMES['ccswitch'])

    def test_stop_returns_early_when_not_running(self):
        with mock.patch.object(keyimport, 'is_running', return_value=False):
            self.assertEqual(keyimport.stop('ccswitch'),
                             {'stopped': False, 'forced': False})


if __name__ == '__main__':
    unittest.main()

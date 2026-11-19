"""上游网页改版监控（services/watchdog.py）。

这里钉住的是**判读口径**，不是实现细节：

1. 归一化必须忽略"每天都会变"的东西（bundle 哈希、时间戳、脚本内容），
   否则指纹天天变，监控退化成噪音；
2. 但**结构骨架**与**可见文案**必须留在指纹里——改版信号就在这两处；
3. 邮件启用是**条件式**的：SMTP 出口探不通就不能落成 enabled=true；
4. LLM 不可用时要退化成关键词规则，不能因为没配 LLM 就静默不告警。
"""
from __future__ import annotations

import asyncio
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import qq as qqsvc  # noqa: E402
from server.services import watchdog  # noqa: E402


class NormalizeTest(unittest.TestCase):
    def test_html_drops_scripts_and_keeps_structure(self) -> None:
        html = (
            '<html><head><script src="/app.8f3a.js">var build="20261020";</script>'
            '<style>.x{color:red}</style></head>'
            '<body><!-- tracking --><div class="b  a" id="root">'
            '<button class="claim-btn" data-testid="claim">领取 500 积分</button>'
            '</div></body></html>'
        )
        lines = watchdog.normalize_html(html)
        joined = '\n'.join(lines)
        self.assertIn('TAG div class=a b id=root', joined)
        self.assertIn('TAG button class=claim-btn data-testid=claim', joined)
        # script 的 src 要留（构建哈希是前端发布信号），但它的**内容**要丢
        self.assertIn('TAG script src=/app.8f3a.js', joined)
        self.assertIn('TEXT 领取 500 积分', joined)
        self.assertNotIn('build=', joined)       # 脚本内容丢弃
        self.assertNotIn('color:red', joined)    # 样式内容丢弃
        self.assertNotIn('tracking', joined)     # 注释丢弃

    def test_bundle_hash_is_a_signal(self) -> None:
        # SPA 的页面骨架是常量，前端一发布变的就是 script 的构建哈希 —— 必须算进指纹
        a = watchdog.normalize_html('<div><script src="/app.111.js"></script></div>')
        b = watchdog.normalize_html('<div><script src="/app.222.js"></script></div>')
        self.assertNotEqual(watchdog.digest_of(a), watchdog.digest_of(b))

    def test_anchor_ids_do_not_change_digest(self) -> None:
        # 普通链接里的内容 id 不该让指纹抖（同一版页面也会随内容变化）
        a = watchdog.normalize_html('<div><a href="/x/111">活动</a></div>')
        b = watchdog.normalize_html('<div><a href="/x/222">活动</a></div>')
        self.assertEqual(watchdog.digest_of(a), watchdog.digest_of(b))

    def test_inline_script_content_is_ignored(self) -> None:
        a = watchdog.normalize_html('<div><script>var build="1";</script>活动</div>')
        b = watchdog.normalize_html('<div><script>var build="2";</script>活动</div>')
        self.assertEqual(watchdog.digest_of(a), watchdog.digest_of(b))

    def test_real_layout_change_changes_digest(self) -> None:
        a = watchdog.normalize_html('<div class="old-entry">活动</div>')
        b = watchdog.normalize_html(
            '<div class="new-entry"><button>领取</button></div>')
        self.assertNotEqual(watchdog.digest_of(a), watchdog.digest_of(b))

    def test_js_keeps_chinese_and_endpoints(self) -> None:
        lines = watchdog.normalize_js(
            'const a="领取奖励"; const b="/billing/meter/claim-compensation";'
            ' const c="build-20261020";')
        joined = '\n'.join(lines)
        self.assertIn('STR 领取奖励', joined)
        self.assertIn('URL /billing/meter/claim-compensation', joined)
        self.assertNotIn('build-20261020', joined)

    def test_keyword_hits_are_limited_and_deduped(self) -> None:
        lines = ['TEXT 领取 500 积分', 'TEXT 领取 500 积分', 'TEXT 普通文案']
        hits = watchdog.keyword_hits(lines)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['word'], '领取')


class AssetDiscoveryTest(unittest.TestCase):
    def test_prefers_own_cdn_and_limits(self) -> None:
        html = ('<script src="https://res.wx.qq.com/a.js"></script>'
                '<link href="https://assets.workbuddy.cn/b.css">'
                '<script src="/c.js"></script>'
                '<img src="/d.png">')
        urls = watchdog.discover_assets(html, 'https://www.workbuddy.cn/page', 2)
        self.assertEqual(urls[0], 'https://assets.workbuddy.cn/b.css')
        self.assertEqual(urls[1], 'https://www.workbuddy.cn/c.js')

    def test_zero_limit_disables_assets(self) -> None:
        self.assertEqual(watchdog.discover_assets(
            '<script src="/a.js"></script>', 'https://a.example/', 0), [])

    def test_asset_lines_shape(self) -> None:
        js = watchdog.asset_lines(
            'https://assets.workbuddy.cn/a/index.js',
            'const t="领取 500 积分"; fetch("/billing/meter/claim-compensation");')
        self.assertEqual(js[0], 'ASSET /a/index.js')
        self.assertTrue(any(x.startswith('STR 领取') for x in js))
        self.assertTrue(any(x.startswith('URL /billing/') for x in js))

    def test_css_keeps_only_selectors(self) -> None:
        lines = watchdog.asset_lines(
            'https://assets.workbuddy.cn/a/index.css',
            '.claim-btn {' + chr(10) + '  color: red;' + chr(10) + '}' + chr(10))
        self.assertEqual(lines[0], 'ASSET /a/index.css')
        self.assertIn('.claim-btn {', lines)
        self.assertFalse(any('color: red' in x for x in lines))


class DiffTest(unittest.TestCase):
    def test_counts_added_and_removed(self) -> None:
        diff = watchdog.diff_lines(['a', 'b'], ['a', 'c'])
        self.assertEqual(diff['added'], 1)
        self.assertEqual(diff['removed'], 1)
        self.assertIn('+c', diff['excerpt'])

    def test_truncation_flag(self) -> None:
        diff = watchdog.diff_lines([str(i) for i in range(500)],
                                   [str(i) + 'x' for i in range(500)], limit=10)
        self.assertTrue(diff['truncated'])


class VerdictTest(unittest.TestCase):
    def test_rule_verdict_flags_claim_keywords(self) -> None:
        verdict = watchdog.rule_verdict(
            {'added': 1, 'removed': 0, 'excerpt': '', 'truncated': False},
            [{'word': '领取', 'line': 'TEXT 领取 500 积分'}])
        self.assertTrue(verdict['claimable'])
        self.assertEqual(verdict['source'], 'rule')

    def test_rule_verdict_without_claim_keywords(self) -> None:
        verdict = watchdog.rule_verdict(
            {'added': 1, 'removed': 0, 'excerpt': '', 'truncated': False},
            [{'word': '积分', 'line': 'TEXT 积分明细'}])
        self.assertFalse(verdict['claimable'])

    def test_parse_verdict_accepts_fenced_json(self) -> None:
        verdict = watchdog._parse_verdict(  # noqa: SLF001
            '```json\n{"layout_changed": true, "claimable": true, '
            '"anomaly": true, "confidence": 0.8, "summary": "新增领取入口"}\n```')
        self.assertTrue(verdict['claimable'])
        self.assertAlmostEqual(verdict['confidence'], 0.8)

    def test_parse_verdict_rejects_non_json(self) -> None:
        with self.assertRaises(RuntimeError):
            watchdog._parse_verdict('看起来像有新活动')  # noqa: SLF001

    def test_should_notify_modes(self) -> None:
        verdict = {'claimable': False, 'anomaly': False}
        self.assertTrue(watchdog._should_notify({'notify_on': 'change'}, verdict, None))  # noqa: SLF001
        self.assertFalse(watchdog._should_notify({'notify_on': 'claimable'}, verdict, None))  # noqa: SLF001
        self.assertFalse(watchdog._should_notify({'notify_on': 'anomaly'}, verdict, None))  # noqa: SLF001
        scan = {'claimable': 2}
        self.assertTrue(watchdog._should_notify({'notify_on': 'claimable'}, verdict, scan))  # noqa: SLF001


class ConfigTest(unittest.IsolatedAsyncioTestCase):
    async def test_mail_not_enabled_when_egress_blocked(self) -> None:
        stored: dict = {}
        fail = {'ok': False, 'host': 'smtp.example.com', 'port': 465,
                'error': 'timed out', 'ports': [], 'checked_at': 0}
        with mock.patch.object(watchdog.db, 'get_setting', return_value=None), \
             mock.patch.object(watchdog.db, 'set_setting',
                               side_effect=lambda k, v: stored.__setitem__(k, v)), \
             mock.patch.object(watchdog, 'probe_smtp_egress', return_value=fail):
            result = await watchdog.save_config({
                'mail': {'enabled': True, 'host': 'smtp.example.com', 'port': 465,
                         'from': 'a@example.com', 'to': ['b@example.com']},
            })
        self.assertFalse(result['config']['mail']['enabled'])
        self.assertIn('SMTP 出口不通', result['warning'])
        self.assertFalse(stored[watchdog.CONFIG_KEY]['mail']['enabled'])

    async def test_mail_enabled_when_egress_ok(self) -> None:
        ok = {'ok': True, 'host': 'smtp.example.com', 'port': 465,
              'banner': '220 hi', 'ports': [], 'checked_at': 0}
        stored: dict = {}
        with mock.patch.object(watchdog.db, 'get_setting', return_value=None), \
             mock.patch.object(watchdog.db, 'set_setting',
                               side_effect=lambda k, v: stored.__setitem__(k, v)), \
             mock.patch.object(watchdog, 'probe_smtp_egress', return_value=ok):
            result = await watchdog.save_config({
                'mail': {'enabled': True, 'host': 'smtp.example.com', 'port': 465,
                         'from': 'a@example.com', 'to': ['b@example.com']},
            })
        self.assertTrue(result['config']['mail']['enabled'])
        self.assertEqual(result['warning'], '')

    async def test_masked_password_keeps_old_value(self) -> None:
        current = watchdog._default_config()  # noqa: SLF001
        current['mail']['password'] = 'old-secret'
        current['mail']['host'] = 'smtp.example.com'
        current['mail']['enabled'] = False
        stored: dict = {}
        with mock.patch.object(watchdog.db, 'get_setting', return_value=current), \
             mock.patch.object(watchdog.db, 'set_setting',
                               side_effect=lambda k, v: stored.__setitem__(k, v)):
            await watchdog.save_config({'mail': {'password': '********'},
                                        'targets': [{'name': 'x', 'url': 'https://a.example/'}]})
        self.assertEqual(stored[watchdog.CONFIG_KEY]['mail']['password'], 'old-secret')
        self.assertEqual(watchdog.public_config(stored[watchdog.CONFIG_KEY])
                         ['mail']['password'], '********')

    async def test_partial_save_keeps_other_sections(self) -> None:
        current = watchdog._default_config()  # noqa: SLF001
        current['targets'] = [{'name': 'keep-me', 'url': 'https://a.example/',
                               'kind': 'html', 'enabled': True}]
        current['llm']['model'] = 'keep-model'
        stored: dict = {}
        with mock.patch.object(watchdog.db, 'get_setting', return_value=current),              mock.patch.object(watchdog.db, 'set_setting',
                               side_effect=lambda k, v: stored.__setitem__(k, v)):
            await watchdog.save_config({'interval_minutes': 60})
        saved = stored[watchdog.CONFIG_KEY]
        self.assertEqual(saved['targets'][0]['name'], 'keep-me')
        self.assertEqual(saved['llm']['model'], 'keep-model')
        self.assertEqual(saved['interval_minutes'], 60)

    async def test_invalid_interval_rejected(self) -> None:
        with mock.patch.object(watchdog.db, 'get_setting', return_value=None):
            with self.assertRaises(ValueError):
                await watchdog.save_config({'interval_minutes': 1})

    async def test_targets_must_be_http(self) -> None:
        with mock.patch.object(watchdog.db, 'get_setting', return_value=None):
            with self.assertRaises(ValueError):
                await watchdog.save_config(
                    {'targets': [{'name': 'x', 'url': 'file:///etc/passwd'}]})


class EgressTest(unittest.TestCase):
    def test_probe_reads_banner(self) -> None:
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        port = server.getsockname()[1]

        def serve() -> None:
            conn, _ = server.accept()
            try:
                conn.sendall(b'220 fake-smtp ready\r\n')
            finally:
                conn.close()
                server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        result = watchdog._probe_one('127.0.0.1', port, timeout=2)  # noqa: SLF001
        thread.join(timeout=3)
        self.assertTrue(result['ok'])
        self.assertIn('220', result['banner'])

    def test_probe_reports_unreachable(self) -> None:
        # 绑一个端口再立刻关掉：大概率拿到 RST/拒绝连接 —— 断言"不通"即可
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        port = server.getsockname()[1]
        server.close()
        result = watchdog._probe_one('127.0.0.1', port, timeout=1)  # noqa: SLF001
        self.assertFalse(result['ok'])
        self.assertTrue(result['error'])


class RunOnceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snapshots: dict[str, dict] = {}

    def _patch_env(self, cfg: dict, pages: list[str]):
        def fake_load(url: str):
            return self.snapshots.get(url)

        def fake_save(url: str, digest: str, lines: list[str]) -> None:
            self.snapshots[url] = {'url': url, 'digest': digest, 'lines': lines}

        return [
            mock.patch.object(watchdog, 'load_config', return_value=cfg),
            mock.patch.object(watchdog, 'load_snapshot', side_effect=fake_load),
            mock.patch.object(watchdog, '_save_snapshot', side_effect=fake_save),
            mock.patch.object(watchdog, 'fetch_target',
                              new=mock.AsyncMock(side_effect=pages)),
            mock.patch.object(watchdog.db, 'get_setting', return_value={}),
            mock.patch.object(watchdog.db, 'set_setting'),
        ]

    async def test_baseline_then_unchanged_then_changed(self) -> None:
        cfg = watchdog._default_config()  # noqa: SLF001
        cfg['targets'] = [{'name': 't', 'url': 'https://a.example/', 'kind': 'html',
                           'enabled': True}]
        cfg['scan_compensation'] = False
        pages = ['<div>活动</div>', '<div>活动</div>',
                 '<div><button>领取</button></div>']
        patches = self._patch_env(cfg, pages)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        first = await watchdog.run_once('test')
        self.assertEqual(first['targets'][0]['status'], 'baseline_created')
        second = await watchdog.run_once('test')
        self.assertEqual(second['targets'][0]['status'], 'unchanged')
        third = await watchdog.run_once('test')
        self.assertEqual(third['targets'][0]['status'], 'changed')
        self.assertIn('t', third['changed'])
        # 邮件未启用时 send_mail 会返回 skipped，而不是静默不记录
        self.assertFalse(third['mail']['ok'])
        self.assertIn('未启用', third['mail']['skipped'])

    async def test_changed_target_triggers_mail(self) -> None:
        cfg = watchdog._default_config()  # noqa: SLF001
        cfg['targets'] = [{'name': 't', 'url': 'https://a.example/', 'kind': 'html',
                           'enabled': True}]
        cfg['scan_compensation'] = False
        cfg['mail'] = {**cfg['mail'], 'enabled': True, 'host': 'smtp.example.com',
                       'from': 'a@example.com', 'to': ['b@example.com']}
        self.snapshots['https://a.example/'] = {
            'digest': watchdog.digest_of(['TAG div']), 'lines': ['TAG div']}
        patches = self._patch_env(cfg, ['<div>领取</div>'])
        sent: list[tuple[str, str]] = []

        async def fake_send(subject: str, body: str, *, cfg=None):
            sent.append((subject, body))
            return {'ok': True}

        patches.append(mock.patch.object(watchdog, 'send_mail',
                                         new=mock.AsyncMock(side_effect=fake_send)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        result = await watchdog.run_once('test')
        self.assertEqual(result['targets'][0]['status'], 'changed')
        self.assertEqual(len(sent), 1)
        self.assertIn('t', sent[0][0])
        self.assertIn('领取', sent[0][1])

    async def test_changed_target_sends_qq_alongside_mail(self) -> None:
        """两个通道互相独立：邮件发失败不影响 QQ，反之亦然。"""
        cfg = watchdog._default_config()  # noqa: SLF001
        cfg['targets'] = [{'name': 't', 'url': 'https://a.example/', 'kind': 'html',
                           'enabled': True}]
        cfg['scan_compensation'] = False
        cfg['qq'].update({'enabled': True, 'base_url': 'https://h',
                          'api_key': 'abk', 'umo': 'a:b:c'})
        self.snapshots['https://a.example/'] = {
            'digest': watchdog.digest_of(['TAG div']), 'lines': ['TAG div']}
        patches = self._patch_env(cfg, ['<div>领取</div>'])
        qq_sent: list[str] = []

        async def fake_qq(text: str, *, cfg=None):
            qq_sent.append(text)
            return {'ok': True, 'umo': 'a:b:c'}

        patches.append(mock.patch.object(watchdog, 'send_mail',
                                         new=mock.AsyncMock(return_value={'ok': False})))
        patches.append(mock.patch.object(watchdog, 'send_qq',
                                         new=mock.AsyncMock(side_effect=fake_qq)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        result = await watchdog.run_once('test')
        self.assertEqual(len(qq_sent), 1)
        self.assertIn('领取', qq_sent[0])
        self.assertTrue(result['qq']['ok'])
        self.assertFalse(result['mail']['ok'])


class StateTargetTest(unittest.TestCase):
    """落库的那份结果要「说清结论」，但不该把 diff 原文整段写进设置项。"""

    def test_trims_diff_and_keeps_verdict(self) -> None:
        result = {
            'name': 't', 'url': 'https://example.com/', 'status': 'changed',
            'digest': 'abc', 'lines': 3,
            'diff': {'added': 2, 'removed': 1,
                     'excerpt': '+' + 'x' * 5000, 'truncated': True},
            'keywords': [{'word': '领取', 'line': 'TEXT 领取'}],
            'verdict': {
                'layout_changed': True, 'claimable': True, 'anomaly': True,
                'confidence': 0.75, 'summary': '新增领取入口',
                'evidence': ['TAG button class=claim'], 'actions': ['打开页面确认'],
                'source': 'llm',
            },
        }
        out = watchdog._state_target(result)  # noqa: SLF001
        self.assertNotIn('keywords', out)
        self.assertNotIn('excerpt', out['diff'])
        self.assertEqual(out['diff'], {'added': 2, 'removed': 1, 'truncated': True})
        self.assertEqual(out['verdict']['summary'], '新增领取入口')
        self.assertTrue(out['verdict']['claimable'])
        self.assertEqual(out['verdict']['confidence'], 0.75)
        self.assertEqual(out['verdict']['source'], 'llm')

    def test_tolerates_bad_values(self) -> None:
        """库里的判读可能被手改坏（字符串、null）——界面不能因此 500。"""
        out = watchdog._state_target({  # noqa: SLF001
            'name': 't', 'status': 'changed',
            'diff': {'added': 'x', 'removed': None},
            'verdict': {'confidence': 'nope', 'evidence': None, 'actions': 'oops'},
        })
        self.assertEqual(out['diff'], {'added': 0, 'removed': 0, 'truncated': False})
        self.assertEqual(out['verdict']['confidence'], 0.0)
        self.assertEqual(out['verdict']['evidence'], [])
        self.assertEqual(out['verdict']['actions'], [])


class RouterOverrideTest(unittest.TestCase):
    """测试按钮用「表单当前值」而不是已保存配置（用户填完就要能点）。"""

    def test_merge_mail_prefers_form_values(self) -> None:
        from server.routers import watchdog as router  # noqa: PLC0415

        base = {'host': '', 'port': 465, 'security': 'ssl', 'username': '',
                'password': '', 'from': '', 'to': [], 'subject_prefix': ''}
        out = router._merge_mail(base, {  # noqa: SLF001
            'host': ' smtp.qq.com ', 'port': 587, 'security': 'starttls',
            'to': ['a@example.com', ' '], 'password': 'secret',
        })
        self.assertEqual(out['host'], 'smtp.qq.com')
        self.assertEqual(out['port'], 587)
        self.assertEqual(out['security'], 'starttls')
        self.assertEqual(out['to'], ['a@example.com'])
        self.assertEqual(out['password'], 'secret')

    def test_merge_mail_keeps_saved_password_when_masked(self) -> None:
        from server.routers import watchdog as router  # noqa: PLC0415

        base = {'host': 'smtp.old', 'port': 465, 'security': 'ssl',
                'password': 'saved-pw', 'to': []}
        out = router._merge_mail(base, {'host': 'smtp.new', 'password': '********'})  # noqa: SLF001
        self.assertEqual(out['host'], 'smtp.new')
        self.assertEqual(out['password'], 'saved-pw')

    def test_merge_llm_keeps_saved_key_when_masked(self) -> None:
        from server.routers import watchdog as router  # noqa: PLC0415

        base = {'enabled': True, 'base_url': '', 'api_key': 'sk-old', 'model': ''}
        out = router._merge_llm(base, {'base_url': ' https://x/v1 ', 'model': 'm',
                                       'api_key': '********'})  # noqa: SLF001
        self.assertEqual(out['base_url'], 'https://x/v1')
        self.assertEqual(out['model'], 'm')
        self.assertEqual(out['api_key'], 'sk-old')


class TestMailTest(unittest.IsolatedAsyncioTestCase):
    async def test_test_mail_works_before_enabling_notifications(self) -> None:
        """「发送测试邮件」不该要求先打开通知开关——用户正是要在开启前验证。"""
        cfg = {'mail': {
            'enabled': False, 'host': 'smtp.example.com', 'port': 465,
            'security': 'ssl', 'username': 'u', 'password': 'p', 'from': 'f@example.com',
            'to': ['t@example.com'], 'subject_prefix': '[监控]',
        }}
        sent: list[dict] = []

        async def fake_send(subject: str, body: str, *, cfg: dict | None = None) -> dict:
            sent.append(cfg or {})
            return {'ok': True}

        with mock.patch.object(watchdog, 'probe_smtp_egress', return_value={
                'ok': True, 'host': 'smtp.example.com', 'port': 465,
                'banner': '220 ok', 'error': '', 'ports': [], 'checked_at': 0}), \
             mock.patch.object(watchdog, 'send_mail', new=mock.AsyncMock(side_effect=fake_send)):
            result = await watchdog.test_mail(cfg)

        self.assertTrue(result['ok'])
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]['mail']['enabled'])


class SchedulerTest(unittest.TestCase):
    def test_due_respects_interval_and_enabled(self) -> None:
        cfg = watchdog._default_config()  # noqa: SLF001
        cfg['enabled'] = False
        self.assertFalse(watchdog._is_due(cfg, {'last_run_at': 0}, now=10**9))  # noqa: SLF001
        cfg['enabled'] = True
        cfg['interval_minutes'] = 60
        self.assertTrue(watchdog._is_due(cfg, {'last_run_at': 0}, now=10**9))  # noqa: SLF001
        self.assertFalse(watchdog._is_due(cfg, {'last_run_at': 10**9}, now=10**9 + 60))  # noqa: SLF001
        self.assertTrue(watchdog._is_due(cfg, {'last_run_at': 10**9}, now=10**9 + 3600))  # noqa: SLF001


class SsrfGuardTest(unittest.TestCase):
    """SSRF 防线：目标与**每一次重定向**都必须落在公网。"""

    def test_private_loopback_and_mapped_are_rejected(self) -> None:
        for addr in ('127.0.0.1', '10.1.2.3', '169.254.1.1', '0.0.0.0',
                     '::1', '::ffff:127.0.0.1'):
            with self.subTest(addr=addr):
                def addrinfo(host, port, *a, _addr=addr, **k):
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (_addr, 0))]

                with mock.patch.object(watchdog.socket, 'getaddrinfo', addrinfo):
                    with self.assertRaises(watchdog.TargetError):
                        asyncio.run(watchdog._assert_public_url(  # noqa: SLF001
                            'https://example.test/', allow_private=False))

    def test_allow_private_skips_ip_check(self) -> None:
        def addrinfo(host, port, *a, **k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0))]

        with mock.patch.object(watchdog.socket, 'getaddrinfo', addrinfo):
            asyncio.run(watchdog._assert_public_url(  # noqa: SLF001
                'https://example.test/', allow_private=True))

    def test_redirect_to_private_is_rejected_before_second_request(self) -> None:
        class _Resp:
            status_code = 302
            headers = {'location': 'http://127.0.0.1/secret'}
            encoding = 'utf-8'

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Client:
            def __init__(self):
                self.calls: list[str] = []

            def stream(self, method, url, **kw):
                self.calls.append(url)
                return _Resp()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        client = _Client()
        seen: list[str] = []

        async def assert_public(url, *, allow_private):
            seen.append(url)
            if '127.0.0.1' in url:
                raise watchdog.TargetError('非公网')

        with mock.patch.object(watchdog.config, 'http_client', lambda *a, **k: client), \
             mock.patch.object(watchdog, '_assert_public_url', assert_public):
            with self.assertRaises(watchdog.TargetError):
                asyncio.run(watchdog.fetch_target(  # noqa: SLF001
                    {'url': 'https://public.test/'}, 5, allow_private=False))
        self.assertEqual(client.calls, ['https://public.test/'],
                         '重定向目标必须在发出第二跳请求前就被校验')
        self.assertEqual(seen, ['https://public.test/', 'http://127.0.0.1/secret'])

    def test_public_redirect_is_followed(self) -> None:
        class _Resp:
            def __init__(self, status, headers=None, body=b''):
                self.status_code = status
                self.headers = headers or {}
                self.encoding = 'utf-8'
                self._body = body

            async def aiter_bytes(self):
                yield self._body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Client:
            def __init__(self):
                self.calls: list[str] = []

            def stream(self, method, url, **kw):
                self.calls.append(url)
                if len(self.calls) == 1:
                    return _Resp(301, {'location': 'https://cdn.test/final'})
                return _Resp(200, body='<div>活动</div>'.encode())

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        client = _Client()

        async def assert_public(url, *, allow_private):
            return None

        with mock.patch.object(watchdog.config, 'http_client', lambda *a, **k: client), \
             mock.patch.object(watchdog, '_assert_public_url', assert_public):
            out = asyncio.run(watchdog.fetch_target(  # noqa: SLF001
                {'url': 'https://public.test/'}, 5, allow_private=False))
        self.assertIn('活动', out)
        self.assertEqual(client.calls, ['https://public.test/', 'https://cdn.test/final'])

class _FakeResp:
    def __init__(self, status_code: int = 200, payload: object = None,
                 text: str = '') -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> object:
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


class _FakeClient:
    """替身 httpx 客户端：记录调用并按预设返回。"""

    def __init__(self, resp: _FakeResp) -> None:
        self.resp = resp
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def post(self, url: str, **kwargs):
        self.calls.append(('post', url, kwargs))
        return self.resp

    async def get(self, url: str, **kwargs):
        self.calls.append(('get', url, kwargs))
        return self.resp


class QqServiceTest(unittest.IsolatedAsyncioTestCase):
    def _patch_client(self, resp: _FakeResp) -> _FakeClient:
        fake = _FakeClient(resp)
        patcher = mock.patch.object(qqsvc.config, 'http_client',
                                    side_effect=lambda *a, **k: fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    async def test_api_base_normalizes_paths(self) -> None:
        self.assertEqual(qqsvc.api_base('https://h:6185'), 'https://h:6185/api/v1')
        self.assertEqual(qqsvc.api_base('https://h:6185/'), 'https://h:6185/api/v1')
        self.assertEqual(qqsvc.api_base('https://h:6185/api'), 'https://h:6185/api/v1')
        self.assertEqual(qqsvc.api_base('https://h:6185/api/v1/'), 'https://h:6185/api/v1')
        # 整段端点粘进来也要能用
        self.assertEqual(qqsvc.api_base('https://h:6185/api/v1/im/message'),
                         'https://h:6185/api/v1')
        self.assertEqual(qqsvc.api_base('https://h:6185/api/v1/im/bots'),
                         'https://h:6185/api/v1')
        self.assertEqual(qqsvc.api_base(''), '')

    async def test_send_message_posts_umo_and_plain_text(self) -> None:
        fake = self._patch_client(_FakeResp(payload={'status': 'ok'}, text='ok'))
        result = await qqsvc.send_message({
            'enabled': True, 'base_url': 'https://astrbot.example.com',
            'api_key': 'abk_secret', 'umo': 'qq_official:GroupMessage:G1',
            'timeout_seconds': 15,
        }, 'hello')
        self.assertTrue(result['ok'])
        method, url, kwargs = fake.calls[0]
        self.assertEqual(method, 'post')
        self.assertEqual(url, 'https://astrbot.example.com/api/v1/im/message')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer abk_secret')
        self.assertEqual(kwargs['json']['umo'], 'qq_official:GroupMessage:G1')
        self.assertEqual(kwargs['json']['message'][0]['text'], 'hello')
        self.assertEqual(kwargs['json']['message'][0]['type'], 'plain')

    async def test_send_message_truncates_long_text(self) -> None:
        fake = self._patch_client(_FakeResp(payload={'status': 'ok'}, text='ok'))
        await qqsvc.send_message({
            'enabled': True, 'base_url': 'https://h', 'api_key': 'abk',
            'umo': 'a:b:c',
        }, 'x' * (qqsvc.MAX_TEXT + 100))
        text = fake.calls[0][2]['json']['message'][0]['text']
        self.assertLessEqual(len(text), qqsvc.MAX_TEXT)
        self.assertIn('已截断', text)

    async def test_error_text_never_echoes_api_key(self) -> None:
        key = 'abk_do_not_leak'
        self._patch_client(_FakeResp(status_code=403, text=f'denied {key}'))
        result = await qqsvc.send_message({
            'enabled': True, 'base_url': 'https://h', 'api_key': key,
            'umo': 'a:b:c',
        }, 'hi')
        self.assertFalse(result['ok'])
        self.assertIn('403', result['error'])
        self.assertNotIn(key, result['error'])

    async def test_disabled_returns_skipped(self) -> None:
        result = await qqsvc.send_message({'enabled': False}, 'hi')
        self.assertFalse(result['ok'])
        self.assertIn('未启用', result['skipped'])

    async def test_list_bots_accepts_data_list(self) -> None:
        self._patch_client(_FakeResp(payload={'data': [{'id': 'qq_official'}]}))
        result = await qqsvc.list_bots({'base_url': 'https://h', 'api_key': 'abk'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['bots'][0]['id'], 'qq_official')


class QqConfigTest(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_requires_url_key_and_umo(self) -> None:
        with mock.patch.object(watchdog.db, 'get_setting', return_value=None):
            with self.assertRaises(ValueError):
                await watchdog.save_config({'qq': {'enabled': True}})
            with self.assertRaises(ValueError):
                await watchdog.save_config({'qq': {
                    'enabled': True, 'base_url': 'https://h', 'api_key': 'abk',
                    'umo': 'no-colons'}})

    async def test_masked_qq_key_keeps_old_value(self) -> None:
        current = watchdog._default_config()  # noqa: SLF001
        current['qq'].update({'enabled': True, 'base_url': 'https://h',
                              'api_key': 'abk-old', 'umo': 'a:b:c'})
        stored: dict = {}
        with mock.patch.object(watchdog.db, 'get_setting', return_value=current), \
             mock.patch.object(watchdog.db, 'set_setting',
                               side_effect=lambda k, v: stored.__setitem__(k, v)):
            result = await watchdog.save_config({'qq': {'api_key': '********'}})
        self.assertEqual(stored[watchdog.CONFIG_KEY]['qq']['api_key'], 'abk-old')
        self.assertEqual(result['config']['qq']['api_key'], '********')

    def test_public_config_tolerates_legacy_config_without_qq(self) -> None:
        """升级前落库的配置没有 qq 段，读取时不能 500。"""
        cfg = watchdog._default_config()  # noqa: SLF001
        cfg.pop('qq')
        out = watchdog.public_config(cfg)
        self.assertEqual(out['qq']['api_key'], '')

    async def test_partial_save_keeps_qq_section(self) -> None:
        current = watchdog._default_config()  # noqa: SLF001
        current['qq'].update({'enabled': True, 'base_url': 'https://h',
                              'api_key': 'abk-old', 'umo': 'a:b:c'})
        stored: dict = {}
        with mock.patch.object(watchdog.db, 'get_setting', return_value=current), \
             mock.patch.object(watchdog.db, 'set_setting',
                               side_effect=lambda k, v: stored.__setitem__(k, v)):
            await watchdog.save_config({'interval_minutes': 60})
        self.assertEqual(stored[watchdog.CONFIG_KEY]['qq']['umo'], 'a:b:c')


class QqRouterOverrideTest(unittest.TestCase):
    def test_merge_qq_prefers_form_values_and_forces_enabled(self) -> None:
        from server.routers import watchdog as router  # noqa: PLC0415

        base = {'enabled': False, 'base_url': '', 'api_key': 'abk-old',
                'umo': '', 'timeout_seconds': 15}
        out = router._merge_qq(base, {  # noqa: SLF001
            'base_url': ' https://astrbot.example.com ', 'api_key': '********',
            'umo': ' qq_official:GroupMessage:G1 ', 'timeout_seconds': 20,
        })
        self.assertTrue(out['enabled'])
        self.assertEqual(out['base_url'], 'https://astrbot.example.com')
        self.assertEqual(out['api_key'], 'abk-old')
        self.assertEqual(out['umo'], 'qq_official:GroupMessage:G1')
        self.assertEqual(out['timeout_seconds'], 20)


class QqEndpointTest(unittest.IsolatedAsyncioTestCase):
    """端点层：缺字段要 400，成功时把表单覆盖值传到服务层。"""

    def _cfg(self) -> dict:
        return {'qq': {'enabled': False, 'base_url': '', 'api_key': '',
                       'umo': '', 'timeout_seconds': 15}}

    async def test_bots_requires_url_and_key(self) -> None:
        from fastapi import HTTPException  # noqa: PLC0415
        from server.routers import watchdog as router  # noqa: PLC0415

        with mock.patch.object(router.watchdog, 'load_config', return_value=self._cfg()):
            with self.assertRaises(HTTPException) as ctx:
                await router.qq_bots(mock.Mock(), None, {'username': 'a'})
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_bots_passes_form_values(self) -> None:
        from server.routers import watchdog as router  # noqa: PLC0415

        calls: list[dict] = []

        async def fake_bots(qq: dict, **kwargs) -> dict:
            calls.append(qq)
            return {'ok': True, 'bots': [{'id': 'qq_official'}]}

        with mock.patch.object(router.watchdog, 'load_config', return_value=self._cfg()), \
             mock.patch.object(router.qqsvc, 'list_bots', new=mock.AsyncMock(side_effect=fake_bots)), \
             mock.patch.object(router, 'client_ip', return_value='1.2.3.4'), \
             mock.patch.object(router.security, 'audit'):
            result = await router.qq_bots(
                mock.Mock(), {'base_url': ' https://h ', 'api_key': 'abk'},
                {'username': 'a'})
        self.assertTrue(result['ok'])
        self.assertEqual(calls[0]['base_url'], 'https://h')
        self.assertTrue(calls[0]['enabled'])

    async def test_test_qq_merges_and_requires_umo(self) -> None:
        from fastapi import HTTPException  # noqa: PLC0415
        from server.routers import watchdog as router  # noqa: PLC0415

        cfg = self._cfg()
        with mock.patch.object(router.watchdog, 'load_config', return_value=cfg):
            with self.assertRaises(HTTPException) as ctx:
                await router.test_qq(mock.Mock(), {'base_url': 'https://h'},
                                     {'username': 'a'})
        self.assertEqual(ctx.exception.status_code, 400)

        seen: list[dict] = []

        async def fake_test(c: dict) -> dict:
            seen.append(c['qq'])
            return {'ok': True, 'stage': 'send'}

        with mock.patch.object(router.watchdog, 'load_config', return_value=cfg), \
             mock.patch.object(router.watchdog, 'test_qq',
                              new=mock.AsyncMock(side_effect=fake_test)), \
             mock.patch.object(router, 'client_ip', return_value='1.2.3.4'), \
             mock.patch.object(router.security, 'audit'):
            result = await router.test_qq(
                mock.Mock(), {'base_url': 'https://h', 'api_key': 'abk',
                              'umo': 'a:b:c'}, {'username': 'a'})
        self.assertTrue(result['ok'])
        self.assertEqual(seen[0]['umo'], 'a:b:c')


class QqNotifyTest(unittest.IsolatedAsyncioTestCase):
    async def test_test_qq_probes_then_sends(self) -> None:
        cfg = {'qq': {'enabled': False, 'base_url': 'https://h',
                      'api_key': 'abk', 'umo': 'a:b:c', 'timeout_seconds': 15}}
        probes: list[dict] = []
        sends: list[str] = []

        async def fake_bots(qq: dict, **kwargs) -> dict:
            probes.append(qq)
            return {'ok': True, 'bots': [{'id': 'qq_official'}]}

        async def fake_send(qq: dict, text: str, **kwargs) -> dict:
            sends.append(text)
            return {'ok': True}

        with mock.patch.object(qqsvc, 'list_bots', new=mock.AsyncMock(side_effect=fake_bots)), \
             mock.patch.object(qqsvc, 'send_message', new=mock.AsyncMock(side_effect=fake_send)):
            result = await watchdog.test_qq(cfg)
        self.assertTrue(result['ok'])
        self.assertEqual(result['stage'], 'send')
        # 测试不要求先启用通知
        self.assertTrue(probes[0]['enabled'])
        self.assertIn('测试', sends[0])

    async def test_test_qq_reports_auth_stage_without_sending(self) -> None:
        cfg = {'qq': {'enabled': False, 'base_url': 'https://h',
                      'api_key': 'bad', 'umo': 'a:b:c', 'timeout_seconds': 15}}
        sent = mock.AsyncMock()
        with mock.patch.object(qqsvc, 'list_bots',
                               new=mock.AsyncMock(return_value={'ok': False, 'error': '403'})), \
             mock.patch.object(qqsvc, 'send_message', new=sent):
            result = await watchdog.test_qq(cfg)
        self.assertFalse(result['ok'])
        self.assertEqual(result['stage'], 'auth')
        sent.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()

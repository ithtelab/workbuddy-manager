"""token 自动续期（issue #40）。

## 用户报的现象

> 登录的账号那个 token 不会自动续期，到期了需要重新扫码或登录。

排查结论：**不是我们的 bug，但确实没人管**。上游 workbuddy2api 有刷新能力
（`internal/upstream/client.go` 的 `RefreshToken`），只在三个内部时机触发：

  · 保活排程（`schedule.keepalive_hours`，默认每天 22 点**一次**）；
  · 每次 chat 选号后、token 距到期不足 `RefreshSkew`（默认 10 分钟）；
  · 签到前 token 临近过期。

后两条都要求**真的有人在用这个号**。于是长期闲置的账号、上游停机期间、
或保活那一刻网络抖动的账号，会一路走到过期而无人续期 —— 而 accessToken
过期后该账号就彻底不可用，只能重新扫码。

## 这里钉住的几条

  1. **按剩余寿命判断**，不是按整点跑。上游已有 keepalive 排程（整点），
     我们重复它没有意义；「快过期了才刷」才能覆盖长期闲置的号。
  2. **面板改名停用的账号不刷**（它已退出账号池，用户明确不用它），
     **但状态位停用的要刷**（issue #45 的语义就是「只摘流量、凭证保持活跃」）。
  3. **没有 refreshToken 时不静默**：如实记一条失败原因，用户才知道
     这个号只能重新扫码。
  4. **写回必须保留未知字段**：账号文件里还有 `device_token`（设备风控凭据），
     重建式写入会把它冲掉。
  5. **刷新失败不做惩罚**：不冷却、不禁用账号——续期是尽力而为的辅助动作，
     可用性判断仍归上游。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import renew, tencent, wb2api  # noqa: E402

UID = '9b212d8c-f5f7-4ad6-aa20-1d576508c8c1'


class _Resp:
    def __init__(self, status: int, payload: object) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, str):
            raise ValueError('not json')
        return self._payload


class _Client:
    def __init__(self, resp: _Resp, calls: list) -> None:
        self._resp = resp
        self._calls = calls

    async def __aenter__(self) -> '_Client':
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, **kw: object) -> _Resp:
        self._calls.append((url, kw))
        return self._resp


def _refresh(resp: _Resp, auth: dict) -> tuple[tuple, list]:
    calls: list = []

    def fake(*a: object, **kw: object) -> _Client:
        return _Client(resp, calls)

    with mock.patch.object(config, 'http_client', fake):
        out = asyncio.run(tencent.refresh_token(auth))
    return out, calls


class RefreshTokenTest(unittest.TestCase):
    def _auth(self, **kw: object) -> dict:
        base = {'access_token': 'old-at', 'refresh_token': 'rt-1',
                'uid': UID, 'enterprise_id': 'ent', 'domain': '',
                'realm': 'cn', 'device_token': ''}
        base.update(kw)
        return base

    def test_refresh_token_goes_in_header_not_body(self) -> None:
        """协议要求 `X-Refresh-Token` **头**，放 body 上游读不到。"""
        (ok, _m, fields), calls = _refresh(
            _Resp(200, {'accessToken': 'new-at', 'refreshToken': 'rt-2',
                        'expiresIn': 5184000}), self._auth())
        self.assertTrue(ok)
        self.assertEqual(fields['access_token'], 'new-at')
        self.assertEqual(fields['refresh_token'], 'rt-2')
        url, kw = calls[0]
        self.assertTrue(url.endswith('/v2/plugin/auth/token/refresh'), url)
        self.assertEqual(kw['headers'].get('X-Refresh-Token'), 'rt-1')
        self.assertEqual(kw['headers'].get('X-Auth-Refresh-Source'), 'plugin',
                         '缺少官方客户端刷新渠道标识，可能被风控当异常来源')
        self.assertNotIn('json', kw, '刷新接口的 refreshToken 走头，不该塞 body')

    def test_expires_at_computed_from_expires_in(self) -> None:
        before = int(time.time())
        (_ok, _m, fields), _ = _refresh(
            _Resp(200, {'accessToken': 'new-at', 'expiresIn': 3600}), self._auth())
        self.assertGreaterEqual(fields['expires_at'], before + 3590)
        self.assertLessEqual(fields['expires_at'], before + 3610)

    def test_missing_expires_in_keeps_old_expiry(self) -> None:
        """上游没返回有效期时**不要**乱写——写错会让面板显示误导信息。

        与上游 preserveExpiry 同口径。
        """
        (_ok, _m, fields), _ = _refresh(
            _Resp(200, {'accessToken': 'new-at'}), self._auth())
        self.assertNotIn('expires_at', fields, '缺 expiresIn 时不该编造到期时间')

    def test_absurd_expires_in_ignored(self) -> None:
        """超量级的 expiresIn 是脏数据（上游同样按脏值处理，防 token 永不刷新）。"""
        (_ok, _m, fields), _ = _refresh(
            _Resp(200, {'accessToken': 'new-at', 'expiresIn': 10 ** 12}), self._auth())
        self.assertNotIn('expires_at', fields)

    def test_no_refresh_token_short_circuits(self) -> None:
        (ok, msg, fields), calls = _refresh(_Resp(200, {}), self._auth(refresh_token=''))
        self.assertFalse(ok)
        self.assertIn('重新扫码', msg)
        self.assertEqual(calls, [], '没有 refreshToken 就不该发请求')
        self.assertEqual(fields, {})

    def test_http_error_mentions_relogin(self) -> None:
        (ok, msg, _f), _ = _refresh(_Resp(401, {'error': 'x'}), self._auth())
        self.assertFalse(ok)
        self.assertIn('重新扫码', msg, '该告诉用户刷新失败就得重新登录')

    def test_response_without_access_token_fails(self) -> None:
        ok, msg, _ = _refresh(_Resp(200, {'refreshToken': 'rt-2'}), self._auth())[0]
        self.assertFalse(ok, '响应里没有 accessToken 却报成功 —— 用户会以为续期了')
        self.assertIn('重新扫码', msg)

    def test_wrapped_in_data_key_also_accepted(self) -> None:
        """登录接口把结果包在 data 里，刷新接口两种形态都认。"""
        (ok, _m, fields), _ = _refresh(
            _Resp(200, {'data': {'accessToken': 'new-at'}}), self._auth())
        self.assertTrue(ok)
        self.assertEqual(fields['access_token'], 'new-at')

    def test_non_json_response_fails_gracefully(self) -> None:
        ok, msg, _ = _refresh(_Resp(200, 'not json at all'), self._auth())[0]
        self.assertFalse(ok)
        self.assertIn('JSON', msg)


class NeedsRenewTest(unittest.TestCase):
    def test_inside_window(self) -> None:
        now = 1_000_000
        self.assertTrue(renew._needs_renew(now + 2 * 86400, now))
        self.assertTrue(renew._needs_renew(now + 60, now))

    def test_outside_window(self) -> None:
        now = 1_000_000
        self.assertFalse(renew._needs_renew(now + 30 * 86400, now))

    def test_already_expired_counts_as_needing_renew(self) -> None:
        """已过期也要试——refreshToken 往往比 accessToken 活得久，能救回来。"""
        now = 1_000_000
        self.assertTrue(renew._needs_renew(now - 86400, now))

    def test_unknown_expiry_is_not_guessed(self) -> None:
        """解不出到期时间就不要臆测（0 或负数当未知，交给上游）。"""
        self.assertFalse(renew._needs_renew(0))
        self.assertFalse(renew._needs_renew(-5))

    def test_threshold_is_days_not_minutes(self) -> None:
        """阈值必须是「天」量级。

        上游选号路径用 10 分钟是合理的（它每次请求都判），而巡检是按小时跑的，
        10 分钟窗口会被直接跳过——那样这个模块就等于没写。
        """
        self.assertGreaterEqual(renew._RENEW_BEFORE_SECONDS, 86400)
        self.assertLessEqual(renew._CHECK_INTERVAL_SECONDS, renew._RENEW_BEFORE_SECONDS)


class UpdateAuthTokensTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = self.dir
        self.name = f'workbuddy-{UID}.json'
        self.doc = {
            'account': {'uid': UID, 'nickname': '测试号', 'enterpriseId': 'e'},
            'auth': {'accessToken': 'old-at', 'refreshToken': 'rt-1',
                     'expiresAt': 1000, 'domain': 'copilot.tencent.com',
                     'realm': 'cn'},
            'device_token': 'dev-secret',
            'some_future_field': {'keep': 'me'},
        }
        (self.dir / self.name).write_text(
            json.dumps(self.doc, ensure_ascii=False), encoding='utf-8')

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _read(self) -> dict:
        return json.loads((self.dir / self.name).read_text(encoding='utf-8'))

    def test_updates_tokens_only(self) -> None:
        tencent.update_auth_tokens(self.name, {
            'access_token': 'new-at', 'refresh_token': 'rt-2', 'expires_at': 9999})
        got = self._read()
        self.assertEqual(got['auth']['accessToken'], 'new-at')
        self.assertEqual(got['auth']['refreshToken'], 'rt-2')
        self.assertEqual(got['auth']['expiresAt'], 9999)
        # 其余字段一个都不能动
        self.assertEqual(got['auth']['domain'], 'copilot.tencent.com')
        self.assertEqual(got['account']['nickname'], '测试号')

    def test_preserves_device_token(self) -> None:
        """device_token 是设备风控凭据，丢了会静默降级风控形态。

        这条正是「重建式写入」会踩的坑：`write_auth_file` 是给新建/重登用的，
        它按登录响应重建整份文件。续期路径必须**就地合并**。
        """
        tencent.update_auth_tokens(self.name, {'access_token': 'new-at'})
        self.assertEqual(self._read()['device_token'], 'dev-secret')

    def test_preserves_unknown_fields(self) -> None:
        """上游将来加的字段不能被我们抹掉（那份文件是双方共写的）。"""
        tencent.update_auth_tokens(self.name, {'access_token': 'new-at'})
        self.assertEqual(self._read()['some_future_field'], {'keep': 'me'})

    def test_partial_fields_leave_others_alone(self) -> None:
        tencent.update_auth_tokens(self.name, {'access_token': 'new-at'})
        got = self._read()
        self.assertEqual(got['auth']['refreshToken'], 'rt-1')
        self.assertEqual(got['auth']['expiresAt'], 1000, '没给 expires_at 不该改它')

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            tencent.update_auth_tokens('workbuddy-nope.json', {'access_token': 'x'})

    def test_corrupt_file_not_overwritten(self) -> None:
        """解析失败时**宁可不写**——那可能是用户仅存的凭证。"""
        (self.dir / self.name).write_text('{ this is not json', encoding='utf-8')
        with self.assertRaises(ValueError):
            tencent.update_auth_tokens(self.name, {'access_token': 'x'})
        self.assertEqual((self.dir / self.name).read_text(encoding='utf-8'),
                         '{ this is not json')

    def test_traversal_rejected(self) -> None:
        for bad in ('../workbuddy-x.json', '/etc/passwd', 'other.json',
                    'workbuddy-x.txt'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                tencent.update_auth_tokens(bad, {'access_token': 'x'})

    def test_write_is_atomic_and_leaves_no_temp(self) -> None:
        tencent.update_auth_tokens(self.name, {'access_token': 'new-at'})
        leftovers = [p.name for p in self.dir.iterdir() if p.name != self.name]
        self.assertEqual(leftovers, [], f'留下了临时文件：{leftovers}')


class RenewOnceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = self.dir

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _write(self, uid: str, *, expires_in: int, refresh: str = 'rt',
               disabled: bool = False) -> str:
        name = f'workbuddy-{uid}.json' + ('.disabled' if disabled else '')
        (self.dir / name).write_text(json.dumps({
            'account': {'uid': uid, 'nickname': uid, 'enterpriseId': 'e'},
            'auth': {'accessToken': 'at', 'refreshToken': refresh,
                     'expiresAt': int(time.time()) + expires_in,
                     'domain': '', 'realm': 'cn'},
        }), encoding='utf-8')
        return name

    def _run(self, resp: _Resp) -> tuple[dict, list]:
        calls: list = []

        def fake(*a: object, **kw: object) -> _Client:
            return _Client(resp, calls)

        with mock.patch.object(config, 'http_client', fake), \
             mock.patch.object(renew, '_record'):
            out = asyncio.run(renew.renew_once())
        return out, calls

    def test_renews_only_near_expiry(self) -> None:
        """只刷快到期的：刚签发的账号不该被白刷一次（那只是无效请求）。"""
        self._write('soon0001', expires_in=2 * 86400)
        self._write('fresh001', expires_in=50 * 86400)
        stats, calls = self._run(_Resp(200, {'accessToken': 'new-at'}))
        self.assertEqual(stats['checked'], 1, '把不该刷的账号也刷了')
        self.assertEqual(len(calls), 1)
        self.assertEqual(stats['renewed'], 1)

    def test_disabled_by_panel_skipped(self) -> None:
        """改名停用的账号已退出账号池，用户明确不用它 —— 不刷。"""
        self._write('paused01', expires_in=3600, disabled=True)
        stats, calls = self._run(_Resp(200, {'accessToken': 'new-at'}))
        self.assertEqual(stats['checked'], 0)
        self.assertEqual(stats['skipped'], 1)
        self.assertEqual(calls, [])

    def test_expired_account_is_attempted(self) -> None:
        """已过期的也试一次：refreshToken 常比 accessToken 活得久，能救回来。"""
        self._write('expired1', expires_in=-86400)
        stats, _ = self._run(_Resp(200, {'accessToken': 'new-at'}))
        self.assertEqual(stats['renewed'], 1)

    def test_missing_refresh_token_counted_as_failure(self) -> None:
        """没有 refreshToken 要如实记失败，用户才知道这个号只能重新扫码。"""
        self._write('norefresh', expires_in=3600, refresh='')
        stats, calls = self._run(_Resp(200, {'accessToken': 'new-at'}))
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(calls, [], '没有 refreshToken 却还是发了请求')

    def test_failure_log_records_real_uid_not_filename(self) -> None:
        """续期失败落库时 uid 必须是账号真实 uid，写文件名会导致版本筛选过滤掉该记录。"""
        renew._last_fail_log.clear()
        self._write('real-uid-001', expires_in=3600, refresh='')
        logged: list[dict] = []
        with mock.patch.object(renew.db, 'add_task_logs', side_effect=lambda items: logged.extend(items)):
            stats = asyncio.run(renew.renew_once())
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]['uid'], 'real-uid-001')

    def test_upstream_failure_does_not_penalise(self) -> None:
        """刷新失败只是记一笔 —— 不冷却、不禁用账号。

        续期是尽力而为的辅助动作，失败可能只是网络抖动；可用性判断仍归上游。
        """
        self._write('flaky001', expires_in=3600)
        stats, _ = self._run(_Resp(500, {'error': 'boom'}))
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['renewed'], 0)
        # 账号文件原样（没被写坏）
        raw = json.loads((self.dir / 'workbuddy-flaky001.json').read_text(encoding='utf-8'))
        self.assertEqual(raw['auth']['accessToken'], 'at')

    def test_successful_renewal_persists(self) -> None:
        self._write('good0001', expires_in=3600)
        self._run(_Resp(200, {'accessToken': 'new-at', 'refreshToken': 'rt-2',
                              'expiresIn': 5184000}))
        raw = json.loads((self.dir / 'workbuddy-good0001.json').read_text(encoding='utf-8'))
        self.assertEqual(raw['auth']['accessToken'], 'new-at')
        self.assertEqual(raw['auth']['refreshToken'], 'rt-2')
        self.assertGreater(raw['auth']['expiresAt'], time.time() + 50 * 86400)


class WiringTest(unittest.TestCase):
    def test_started_and_stopped_with_app(self) -> None:
        src = (Path(__file__).resolve().parents[2] / 'server/main.py').read_text(encoding='utf-8')
        self.assertIn('renew.start_scheduler()', src)
        self.assertIn('renew.stop_scheduler()', src)

    def test_logs_to_task_log_for_visibility(self) -> None:
        """续期结果要落任务记录（kind=keepalive），用户才能看见。

        只打容器日志没用——那个重建即丢，而用户排查「为什么这个号掉线了」
        看的是任务记录页。
        """
        src = (Path(__file__).resolve().parents[2]
               / 'server/services/renew.py').read_text(encoding='utf-8')
        self.assertIn("'kind': 'keepalive'", src)
        self.assertIn('db.add_task_logs', src)

    def test_task_logs_realm_filter_includes_secondary_groups_and_notes(self) -> None:
        from server import db, upstreamsvc
        from server.routers import accounts as accounts_router
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orig = config.DB_PATH, config.AUTH_DIR
            try:
                config.DB_PATH = root / 'manager.db'
                config.AUTH_DIR = root / 'auths-default'
                config.AUTH_DIR.mkdir()
                sec_dir = root / 'auths-sec'
                sec_dir.mkdir()
                db._conn = None
                db.connect()
                sec_uid = 'feedbeef-0000-0000-0000-000000000002'
                (sec_dir / 'workbuddy-sec.json').write_text(json.dumps({
                    'account': {'uid': sec_uid, 'nickname': '138****9999'},
                    'auth': {'accessToken': 'at', 'refreshToken': 'rt', 'expiresAt': int(time.time()) + 86400,
                             'realm': 'cn', 'domain': 'copilot.tencent.com'},
                }), encoding='utf-8')
                db.set_account_note(sec_uid, '二组备用号')
                db.add_task_logs([{
                    'ts': int(time.time()), 'uid': sec_uid[:8], 'kind': 'travel',
                    'level': 'credit', 'credits': 10, 'message': 'claim ok record=1 reward=10',
                    'dedup_key': 'sec-1',
                }])
                sec_group = {'id': 2, 'name': '分组二', 'auth_dir': str(sec_dir), 'is_default': False}
                with mock.patch.object(upstreamsvc, 'list_upstreams', return_value=[sec_group]):
                    res = accounts_router.task_logs(realm='cn', user={'role': 'admin'})
                self.assertEqual(len(res['logs']), 1)
                self.assertEqual(res['logs'][0]['nickname'], '138****9999')
                self.assertEqual(res['logs'][0]['note'], '二组备用号')
            finally:
                if db._conn:
                    db._conn.close()
                db._conn = None
                config.DB_PATH, config.AUTH_DIR = orig


if __name__ == '__main__':
    unittest.main()

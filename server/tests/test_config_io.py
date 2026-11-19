"""上游配置读写的回归测试：确保可视化字段不会把 config.json 写坏。

重点覆盖三个曾经的坑：
  1. *_hours 必须写成 []int（曾按数字间隔处理，会把 [9,21] 写成 6）
  2. 时长字段必须写成字符串（曾把 breaker_cooldown 当成秒数写整数）
  3. 只提交改动过的字段，不能覆盖同一段里其他未展示的键

运行：python -m unittest discover -s server -t . -p 'test_*.py'
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import wb2api  # noqa: E402


def write_cfg(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def read_cfg(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


class UpstreamConfigRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self.cfg_path

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        self._tmp.cleanup()

    # ── 时刻数组 ─────────────────────────────────────────
    def test_random_delay_roundtrip_preserves_existing_schedule_and_secrets(self) -> None:
        write_cfg(self.cfg_path, {'api_key': 'synthetic', 'schedule': {
            'checkin_hours': [9, 21], 'cat_enabled': False}})
        wb2api.save_upstream_config({'schedule': {'random_delay_minutes': 60}})
        got = read_cfg(self.cfg_path)
        self.assertEqual(got['schedule'], {'checkin_hours': [9, 21],
                                         'cat_enabled': False, 'random_delay_minutes': 60})
        self.assertEqual(got['api_key'], 'synthetic')
        wb2api.save_upstream_config({'schedule': {'random_delay_minutes': 0}})
        self.assertEqual(read_cfg(self.cfg_path)['schedule']['random_delay_minutes'], 0)

    def test_random_delay_rejects_invalid_values_without_writing(self) -> None:
        original = {'schedule': {'checkin_hours': [9, 21]}}
        write_cfg(self.cfg_path, original)
        for value in (-1, 61, True, False, 1.5, '60', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                wb2api.save_upstream_config({'schedule': {'random_delay_minutes': value}})
            self.assertEqual(read_cfg(self.cfg_path), original)

    def test_account_delays_validate_merged_range_and_preserve_configuration(self) -> None:
        write_cfg(self.cfg_path, {'api_key': 'synthetic', 'schedule': {'checkin_hours': [9, 21]}})
        wb2api.save_upstream_config({'schedule': {
            'account_delay_min_seconds': 30, 'account_delay_max_seconds': 120}})
        good = read_cfg(self.cfg_path)
        self.assertEqual(good['schedule']['account_delay_min_seconds'], 30)
        self.assertEqual(good['schedule']['account_delay_max_seconds'], 120)
        for patch in ({'account_delay_min_seconds': 121}, {'account_delay_max_seconds': 29},
                      {'account_delay_min_seconds': -1}, {'account_delay_max_seconds': 601},
                      {'account_delay_min_seconds': True}, {'account_delay_max_seconds': 1.5}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                wb2api.save_upstream_config({'schedule': patch})
            self.assertEqual(read_cfg(self.cfg_path), good)
        wb2api.save_upstream_config({'schedule': {
            'account_delay_min_seconds': 0, 'account_delay_max_seconds': 0}})
        self.assertEqual(read_cfg(self.cfg_path)['api_key'], 'synthetic')

    def test_hours_written_as_int_array(self) -> None:
        write_cfg(self.cfg_path, {'schedule': {'checkin_hours': [9, 21]}})
        wb2api.save_upstream_config({'schedule': {'travel_hours': [9, 21]}})
        got = read_cfg(self.cfg_path)['schedule']['travel_hours']
        self.assertEqual(got, [9, 21])
        self.assertIsInstance(got, list)

    def test_hours_accepts_string_of_numbers(self) -> None:
        """前端按字符串提交时也能落到正确类型（兜底解析）。"""
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config({'schedule': {'activity_hours': [10]}})
        self.assertEqual(read_cfg(self.cfg_path)['schedule']['activity_hours'], [10])

    def test_hours_dedup_and_sort(self) -> None:
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config({'schedule': {'keepalive_hours': [22, 9, 22]}})
        self.assertEqual(read_cfg(self.cfg_path)['schedule']['keepalive_hours'], [9, 22])

    def test_hours_reject_out_of_range(self) -> None:
        write_cfg(self.cfg_path, {'schedule': {'checkin_hours': [9]}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'schedule': {'checkin_hours': [24]}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'schedule': {'checkin_hours': [-1]}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'schedule': {'checkin_hours': []}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'schedule': {'checkin_hours': 6}})
        # 拒绝后原配置保持不变
        self.assertEqual(read_cfg(self.cfg_path)['schedule']['checkin_hours'], [9])

    # ── 时长字符串 ───────────────────────────────────────
    def test_duration_fields_stay_strings(self) -> None:
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config(
            {
                'cooldown': {'soft_rate': '600s', 'soft_rate_max': '2h'},
                'pool': {'breaker_cooldown': '30m', 'breaker_cooldown_max': '6h'},
                'session_sticky': {'ttl': '30m', 'gc_interval': '5m'},
            }
        )
        cfg = read_cfg(self.cfg_path)
        self.assertEqual(cfg['cooldown']['soft_rate'], '600s')
        self.assertEqual(cfg['pool']['breaker_cooldown'], '30m')
        self.assertEqual(cfg['session_sticky']['ttl'], '30m')

    def test_duration_reject_bad_format(self) -> None:
        write_cfg(self.cfg_path, {'pool': {'breaker_cooldown': '30m'}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'pool': {'breaker_cooldown': '600'}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'cooldown': {'soft_rate': 'soon'}})
        self.assertEqual(read_cfg(self.cfg_path)['pool']['breaker_cooldown'], '30m')

    def test_credit_exhausted_allows_empty_but_not_zero(self) -> None:
        """积分耗尽冷却（issue #78）：空 = 默认行为，`0` = 上游判非法。

        这条与 expiring_soon / cost_explore_interval **相反**（那两个 0 = 关闭），
        所以不能共用它们的分支：放行 `0` 会写进一份让上游启动失败（补丁里判
        `d <= 0` 报错）的配置，用户看到的是「保存成功、然后反代挂了」。
        """
        write_cfg(self.cfg_path, {})
        # 空串合法：等价于不启用，落盘为 ""，上游归一化成 0 → 保持旧行为
        wb2api.save_upstream_config({'cooldown': {'credit_exhausted': ''}})
        self.assertEqual(read_cfg(self.cfg_path)['cooldown']['credit_exhausted'], '')
        # 正时长合法，且必须**原样存字符串**（上游是 time.ParseDuration）
        wb2api.save_upstream_config({'cooldown': {'credit_exhausted': ' 10m '}})
        self.assertEqual(read_cfg(self.cfg_path)['cooldown']['credit_exhausted'], '10m')
        # 0 与 0s 必须拒（Go 的 ParseDuration 认它们，是补丁里的 d <= 0 判非法）
        for bad in ('0', '0s'):
            with self.assertRaises(ValueError, msg=f'{bad} 应被拒'):
                wb2api.save_upstream_config({'cooldown': {'credit_exhausted': bad}})
            self.assertEqual(read_cfg(self.cfg_path)['cooldown']['credit_exhausted'], '10m')
        # 非法格式与 Go 不认的 d 单位同样拒
        for bad in ('soon', '7d', '-5m', '10'):
            with self.assertRaises(ValueError, msg=f'{bad} 应被拒'):
                wb2api.save_upstream_config({'cooldown': {'credit_exhausted': bad}})
        self.assertEqual(read_cfg(self.cfg_path)['cooldown']['credit_exhausted'], '10m')

    # ── 不覆盖同段其他键 ─────────────────────────────────
    def test_partial_patch_preserves_siblings(self) -> None:
        write_cfg(
            self.cfg_path,
            {
                'schedule': {'checkin_hours': [9, 21], 'keepalive_hours': [22]},
                'pool': {'max_in_flight': 3},
            },
        )
        wb2api.save_upstream_config({'schedule': {'checkin_hours': [8]}})
        cfg = read_cfg(self.cfg_path)
        self.assertEqual(cfg['schedule']['checkin_hours'], [8])
        self.assertEqual(cfg['schedule']['keepalive_hours'], [22])
        self.assertEqual(cfg['pool']['max_in_flight'], 3)

    def test_bool_switch_round_trip(self) -> None:
        write_cfg(self.cfg_path, {'schedule': {'checkin_enabled': True}})
        wb2api.save_upstream_config({'schedule': {'checkin_enabled': False}})
        self.assertIs(read_cfg(self.cfg_path)['schedule']['checkin_enabled'], False)

    def test_unknown_section_ignored(self) -> None:
        write_cfg(self.cfg_path, {'api_key': 'secret'})
        wb2api.save_upstream_config({'api_key': 'hacked'})
        self.assertEqual(read_cfg(self.cfg_path)['api_key'], 'secret')

    # ── 顶层标量：思考内容兜底（reasoning_as_content）──────────
    def test_reasoning_as_content_written_at_top_level(self) -> None:
        """它在上游是**根级**布尔；写进 features 段等于改了不生效。"""
        write_cfg(self.cfg_path, {'api_key': 'secret', 'features': {'sanitize_blacklist_fingerprints': True}})
        wb2api.save_upstream_config({'reasoning_as_content': False})
        got = read_cfg(self.cfg_path)
        self.assertIs(got['reasoning_as_content'], False)
        # 不能污染 features 段，也不能动其它顶层键
        self.assertEqual(got['features'], {'sanitize_blacklist_fingerprints': True})
        self.assertEqual(got['api_key'], 'secret')

    def test_reasoning_as_content_defaults_to_true_when_absent(self) -> None:
        """缺省时按上游默认回显 true —— 界面要显示实际生效的状态。"""
        write_cfg(self.cfg_path, {'api_key': 'secret'})
        self.assertIs(wb2api.load_upstream_config()['reasoning_as_content'], True)

    def test_reasoning_as_content_rejects_non_bool_without_writing(self) -> None:
        """字符串 "false" 会让 Go 侧 json 解析失败、上游起不来，必须拒绝。"""
        write_cfg(self.cfg_path, {'reasoning_as_content': True})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'reasoning_as_content': 'false'})
        self.assertIs(read_cfg(self.cfg_path)['reasoning_as_content'], True)

    # ── prompt / server / upstream（新增段）─────────────────
    def test_prompt_mode_round_trip(self) -> None:
        write_cfg(self.cfg_path, {'prompt': {'mode': 'custom'}})
        wb2api.save_upstream_config({'prompt': {'mode': 'passthrough'}})
        self.assertEqual(read_cfg(self.cfg_path)['prompt']['mode'], 'passthrough')

    def test_prompt_mode_normalised_and_validated(self) -> None:
        write_cfg(self.cfg_path, {'prompt': {'mode': 'custom'}})
        wb2api.save_upstream_config({'prompt': {'mode': 'Passthrough'}})
        self.assertEqual(read_cfg(self.cfg_path)['prompt']['mode'], 'passthrough')
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'prompt': {'mode': 'replace'}})
        # 拒绝后保持原值
        self.assertEqual(read_cfg(self.cfg_path)['prompt']['mode'], 'passthrough')

    def test_prompt_file_rejects_control_chars(self) -> None:
        write_cfg(self.cfg_path, {'prompt': {'file': ''}})
        wb2api.save_upstream_config({'prompt': {'file': '/etc/prompt.md'}})
        self.assertEqual(read_cfg(self.cfg_path)['prompt']['file'], '/etc/prompt.md')
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'prompt': {'file': '/a\nb'}})

    def test_both_prompt_modes_still_accepted(self) -> None:
        """三种模式都必须能写入 —— 上游两次扩过这个枚举，我们都跟上了。

        上游 2026-09-14 把缺省从 custom 改成 passthrough（透传客户端 system），
        显式配 custom 仍受支持；2026-09-17 又新增 `append`（开头连续
        system/developer 块之后插网关 system，既有消息逐字不动，其 issue #129）。

        为什么这条重要：管理端的白名单是**独立于上游**的一份校验，上游加了新取值
        而这里没跟，用户填合法值会被面板拒掉（「上游支持、面板说不合法」）；反过来
        若面板放行了上游不认的值，用户会存进一份让上游**启动即失败**的配置
        （上游 normalizePrompt 对非法值是 fail fast），表现为「保存成功然后上游挂了」。
        """
        for mode in ('passthrough', 'custom', 'append'):
            write_cfg(self.cfg_path, {'prompt': {'mode': 'passthrough'}})
            wb2api.save_upstream_config({'prompt': {'mode': mode}})
            self.assertEqual(read_cfg(self.cfg_path)['prompt']['mode'], mode)
        # 大小写/空白仍归一化
        write_cfg(self.cfg_path, {'prompt': {'mode': 'passthrough'}})
        wb2api.save_upstream_config({'prompt': {'mode': ' Append '}})
        self.assertEqual(read_cfg(self.cfg_path)['prompt']['mode'], 'append')

    def test_prompt_mode_whitelist_matches_upstream(self) -> None:
        """白名单必须与上游 `normalizePrompt` 的取值集**逐项一致**。

        上游源码（cmd/server/config.go）的 switch 只认这三个；多一个或少一个
        都会造成上面说的两种故障。写成显式断言，便于上游再扩时一眼看到要改哪里。
        """
        upstream_values = {'', 'passthrough', 'custom', 'append'}  # 空串 = 缺省 passthrough
        for value in sorted(upstream_values - {''}):
            write_cfg(self.cfg_path, {'prompt': {'mode': 'passthrough'}})
            wb2api.save_upstream_config({'prompt': {'mode': value}})  # 不抛错即通过
        for bogus in ('replace', 'degraded', 'none', 'appendx'):
            with self.assertRaises(ValueError, msg=bogus):
                wb2api.save_upstream_config({'prompt': {'mode': bogus}})

    def test_empty_prompt_section_is_not_invented(self) -> None:
        """配置里没有 prompt 段时，不该被管理端凭空造出来。

        这条守的是「UI 默认值 = 上游默认值」这个不变量：前端把缺省字段显示为
        默认值，保存时只提交**改动过**的键；这里从后端确认「没改就不写」，
        否则老配置会在一次无关的保存里多出 prompt 段（虽然值相同，但属于
        不该发生的写入）。
        """
        write_cfg(self.cfg_path, {'pool': {'max_in_flight': 3}})
        wb2api.save_upstream_config({'pool': {'max_in_flight': 5}})
        cfg = read_cfg(self.cfg_path)
        self.assertNotIn('prompt', cfg, '不该凭空写入 prompt 段')
        self.assertEqual(cfg['pool']['max_in_flight'], 5)

    def test_server_max_body_mb_validation(self) -> None:
        write_cfg(self.cfg_path, {'server': {'max_body_mb': 8}})
        wb2api.save_upstream_config({'server': {'max_body_mb': 32}})
        self.assertEqual(read_cfg(self.cfg_path)['server']['max_body_mb'], 32)
        for bad in (0, -1, 257, 'many'):
            with self.assertRaises(ValueError):
                wb2api.save_upstream_config({'server': {'max_body_mb': bad}})
        self.assertEqual(read_cfg(self.cfg_path)['server']['max_body_mb'], 32)

    def test_upstream_user_agent_round_trip(self) -> None:
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config({'upstream': {'user_agent': 'MyClient/1.0'}})
        self.assertEqual(read_cfg(self.cfg_path)['upstream']['user_agent'], 'MyClient/1.0')
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'upstream': {'user_agent': 'bad\nua'}})

    def test_new_sections_do_not_touch_others(self) -> None:
        """保存 prompt 不应影响 timeout 等同段的其他键。"""
        write_cfg(
            self.cfg_path,
            {'upstream': {'timeout_seconds': 120, 'idle_timeout_seconds': 300}},
        )
        wb2api.save_upstream_config({'upstream': {'user_agent': 'X/1'}})
        cfg = read_cfg(self.cfg_path)['upstream']
        self.assertEqual(cfg['timeout_seconds'], 120)
        self.assertEqual(cfg['idle_timeout_seconds'], 300)
        self.assertEqual(cfg['user_agent'], 'X/1')

    # ── 整数字段区间（前端 max 只是输入框属性，服务端必须兜底）──
    def test_activity_report_count_range(self) -> None:
        write_cfg(self.cfg_path, {'schedule': {}})
        # 合法：1（旧行为）~ 50
        for v in (1, 5, 20, 50):
            wb2api.save_upstream_config({'schedule': {'activity_report_count': v}})
            self.assertEqual(read_cfg(self.cfg_path)['schedule']['activity_report_count'], v)
        # 非法：越界 / 零 / 负数 / 非整数 / bool
        for bad in (0, -1, 51, 999, 100000, 'x', 3.5, True):
            with self.assertRaises(ValueError, msg=f'{bad!r} 应被拒绝'):
                wb2api.save_upstream_config({'schedule': {'activity_report_count': bad}})
        # 拒绝后配置保持最后一次合法值
        self.assertEqual(read_cfg(self.cfg_path)['schedule']['activity_report_count'], 50)

    def test_pool_int_ranges(self) -> None:
        write_cfg(self.cfg_path, {'pool': {}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'pool': {'max_in_flight': 999}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'pool': {'breaker_threshold': 0}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'pool': {'idle_weight_max': -1}})
        # 合法值可通过
        wb2api.save_upstream_config({'pool': {'max_in_flight': 3, 'breaker_threshold': 3}})
        self.assertEqual(read_cfg(self.cfg_path)['pool']['max_in_flight'], 3)

    def test_pool_float_range(self) -> None:
        write_cfg(self.cfg_path, {'pool': {}})
        for v in (0, 0.5, 100.0):
            wb2api.save_upstream_config({'pool': {'idle_weight_per_hour': v}})
        self.assertEqual(read_cfg(self.cfg_path)['pool']['idle_weight_per_hour'], 100.0)
        for bad in (-1, 101, 'fast', True):
            with self.assertRaises(ValueError, msg=f'{bad!r} 应被拒绝'):
                wb2api.save_upstream_config({'pool': {'idle_weight_per_hour': bad}})

    # ── upstream 段的凭据与新增字段 ─────────────────────
    def test_device_token_masked_in_view(self) -> None:
        """device_token 是设备风控凭据，绝不能明文下发。"""
        write_cfg(self.cfg_path, {
            'upstream': {'device_token': 'DEVICE-SECRET-abcdef123456', 'client_name': 'WorkBuddy'},
        })
        view = wb2api.load_upstream_config()
        raw = json.dumps(view, ensure_ascii=False)
        self.assertNotIn('DEVICE-SECRET-abcdef123456', raw, '凭据不应出现在接口返回里')
        self.assertTrue(view['upstream'].get('has_device_token'))
        self.assertIn('device_token_masked', view['upstream'])

    def test_device_token_empty_keeps_original(self) -> None:
        """留空表示保持原值——前端拿到的是掩码，不能被当成清空。"""
        write_cfg(self.cfg_path, {'upstream': {'device_token': 'ORIG'}})
        wb2api.save_upstream_config({'upstream': {'device_token': ''}})
        self.assertEqual(read_cfg(self.cfg_path)['upstream']['device_token'], 'ORIG')

    def test_device_token_none_clears(self) -> None:
        """显式 null 才清除（否则没有办法删掉它）。"""
        write_cfg(self.cfg_path, {'upstream': {'device_token': 'ORIG', 'client_name': 'X'}})
        wb2api.save_upstream_config({'upstream': {'device_token': None}})
        up = read_cfg(self.cfg_path)['upstream']
        self.assertNotIn('device_token', up)
        self.assertEqual(up['client_name'], 'X', '不应误删同段其他键')

    def test_device_token_new_value_replaces(self) -> None:
        write_cfg(self.cfg_path, {'upstream': {'device_token': 'OLD'}})
        wb2api.save_upstream_config({'upstream': {'device_token': 'NEW'}})
        self.assertEqual(read_cfg(self.cfg_path)['upstream']['device_token'], 'NEW')

    def test_device_token_rejects_bad_value(self) -> None:
        write_cfg(self.cfg_path, {'upstream': {'device_token': 'ORIG'}})
        for bad in ('a' + chr(10) + 'b', 'x' * 600):
            with self.assertRaises(ValueError):
                wb2api.save_upstream_config({'upstream': {'device_token': bad}})
        self.assertEqual(read_cfg(self.cfg_path)['upstream']['device_token'], 'ORIG')

    def test_upstream_new_text_fields(self) -> None:
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config({'upstream': {
            'client_name': 'WorkBuddy', 'client_version': '5.5.4',
            'cli_version': '2.137.1', 'device_token_file': '/etc/tok',
        }})
        up = read_cfg(self.cfg_path)['upstream']
        self.assertEqual(up['client_name'], 'WorkBuddy')
        self.assertEqual(up['client_version'], '5.5.4')
        self.assertEqual(up['cli_version'], '2.137.1')
        self.assertEqual(up['device_token_file'], '/etc/tok')

    def test_upstream_passthrough_ip_bool(self) -> None:
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config({'upstream': {'passthrough_ip': True}})
        self.assertIs(read_cfg(self.cfg_path)['upstream']['passthrough_ip'], True)
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'upstream': {'passthrough_ip': 'yes'}})

    def test_upstream_gzip_body_bool(self) -> None:
        """issue #297：gzip 开关必须能落盘（否则界面关掉不生效）。"""
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config({'upstream': {'gzip_body': False}})
        self.assertIs(read_cfg(self.cfg_path)['upstream']['gzip_body'], False)
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'upstream': {'gzip_body': 'off'}})

    def test_missing_config_refuses_to_write(self) -> None:
        with self.assertRaises(FileNotFoundError):
            wb2api.save_upstream_config({'schedule': {'checkin_hours': [9]}})
        self.assertFalse(self.cfg_path.exists())


class TokenTTL(unittest.TestCase):
    """从 JWT 解出令牌总时长（供进度条按真实比例展示）。"""

    @staticmethod
    def _jwt(iat: int, exp: int) -> str:
        import base64, json as _json
        def seg(obj):
            raw = _json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')
        return f'{seg({"alg": "RS256"})}.{seg({"iat": iat, "exp": exp})}.sig'

    def test_reads_lifetime(self) -> None:
        ttl = wb2api.token_ttl_seconds(self._jwt(1000, 1000 + 60 * 86400))
        self.assertEqual(ttl, 60 * 86400)

    def test_garbage_returns_none(self) -> None:
        for bad in ('', 'not-a-jwt', 'a.b.c', 'onlyonesegment'):
            self.assertIsNone(wb2api.token_ttl_seconds(bad), bad)

    def test_missing_or_invalid_claims(self) -> None:
        import base64, json as _json
        def seg(obj):
            raw = _json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')
        # 缺 exp
        self.assertIsNone(wb2api.token_ttl_seconds(f'{seg({})}.{seg({"iat": 1})}.s'))
        # exp <= iat
        self.assertIsNone(wb2api.token_ttl_seconds(f'{seg({})}.{seg({"iat": 100, "exp": 50})}.s'))

    def test_issued_at_reads_iat(self) -> None:
        """签发时间（≈ 最近一次刷新）用于区分「刚续期」与「从没刷新过」。

        背景：界面的「有效期」是剩余时间，刷新会重新拉满，所以单看天数分不清
        「刚被保活续期」和「一直用着当初扫码的长令牌」——后者才是隐患账号。
        """
        iat = 1_700_000_000
        self.assertEqual(wb2api.token_issued_at(self._jwt(iat, iat + 7 * 86400)), iat)

    def test_issued_at_garbage_returns_none(self) -> None:
        for bad in ('', 'not-a-jwt', 'a.b.c', 'two.parts'):
            self.assertIsNone(wb2api.token_issued_at(bad), bad)

    def test_issued_at_matches_ttl_source(self) -> None:
        """两者必须来自同一个令牌，避免显示与进度条基准不一致。"""
        iat, exp = 1_700_000_000, 1_700_000_000 + 60 * 86400
        tok = self._jwt(iat, exp)
        self.assertEqual(wb2api.token_issued_at(tok), iat)
        self.assertEqual(wb2api.token_ttl_seconds(tok), exp - iat)


class ExpiryFallback(unittest.TestCase):
    """JWT 解不出时的兜底：用 auth 文件 mtime 推算窗口，而不是假定 60 天。

    原来的兜底是「解不出就当 60 天」，问题在于：一个 7 天的令牌按 60 天算，
    进度条只显示 12%，看着像快过期——属于误报。而 JWT 解不出并非罕见：
    扁平形手写 auth 文件的 accessToken 常常不是标准 JWT。

    用 mtime 是有依据的：上游刷新 token 后会**原子写回**该文件，安装器写盘
    也走同一路径，因此 mtime ≈ 最近一次写入/刷新时刻，exp - mtime 即本次
    有效期的近似值。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        self._tmp.cleanup()

    def _write(self, name: str, uid: str, expires_at: int, token: str,
               mtime: int) -> Path:
        p = Path(self._tmp.name) / name
        p.write_text(json.dumps({
            'account': {'uid': uid, 'nickname': uid},
            'auth': {'accessToken': token, 'expiresAt': expires_at},
        }), encoding='utf-8')
        os.utime(p, (mtime, mtime))
        return p

    def _acct(self, uid: str) -> dict:
        return next(a for a in wb2api.list_auth_accounts() if a['uid'] == uid)

    @staticmethod
    def _jwt(iat: int, exp: int) -> str:
        import base64
        def seg(obj):
            raw = json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')
        return f'{seg({"alg": "RS256"})}.{seg({"iat": iat, "exp": exp})}.sig'

    def test_mtime_fallback_uses_file_window_not_60_days(self) -> None:
        import time
        now = int(time.time())
        # 非 JWT + 7 天有效期 + 刚写入
        self._write('workbuddy-1.json', '1', now + 7 * 86400, 'not-a-jwt', now)
        a = self._acct('1')
        self.assertEqual(a['ttl_seconds'], 7 * 86400)
        self.assertEqual(a['issued_at'], now)

    def test_jwt_takes_priority_over_mtime(self) -> None:
        import time
        now = int(time.time())
        # JWT 说 7 天窗口、3 天前签发；mtime 设成现在也不能覆盖 JWT
        token = self._jwt(now - 3 * 86400, now + 4 * 86400)
        self._write('workbuddy-2.json', '2', now + 4 * 86400, token, now)
        a = self._acct('2')
        self.assertEqual(a['ttl_seconds'], 7 * 86400)
        self.assertEqual(a['issued_at'], now - 3 * 86400)

    def test_no_usable_signal_stays_none(self) -> None:
        """mtime 晚于 expiresAt（异常数据）时不能算出负数窗口。"""
        import time
        now = int(time.time())
        self._write('workbuddy-3.json', '3', now - 100, 'not-a-jwt', now)
        a = self._acct('3')
        self.assertIsNone(a['ttl_seconds'])
        self.assertIsNone(a['issued_at'])


class GlobalInFlightTierTest(unittest.TestCase):
    """国际版在途上限分档（上游 2680f4c 新增 `pool.max_in_flight_global`）。

    上游给 global 域单独的并发上限（官方默认 2），因为国际版风控更严。
    面板要能设置它 —— 两个容易忽略的点：

      1. **保存别的 pool 字段时不能把它弄丢**。`save_upstream_config` 是逐字段
         update（`cfg[field].update(clean)`），看着安全，但那是实现的偶然性质
         而非契约；钉住它，免得将来改成整体替换时静默丢字段。
      2. **要享受区间校验**。不登记进 `_INT_RANGES` 的话未知键被原样透传，
         用户填个 -1 也能写进上游配置。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self.cfg_path

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _write(self, data: dict) -> None:
        self.cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def _read(self) -> dict:
        return json.loads(self.cfg_path.read_text(encoding='utf-8'))

    def test_field_is_writable(self) -> None:
        self._write({'pool': {'max_in_flight': 3, 'max_in_flight_global': 2}})
        wb2api.save_upstream_config({'pool': {'max_in_flight_global': 4}})
        self.assertEqual(self._read()['pool']['max_in_flight_global'], 4)

    def test_not_lost_when_saving_sibling_field(self) -> None:
        """改 max_in_flight 时不能把分档字段弄丢。"""
        self._write({'pool': {'max_in_flight': 3, 'max_in_flight_global': 2}})
        wb2api.save_upstream_config({'pool': {'max_in_flight': 5}})
        pool = self._read()['pool']
        self.assertEqual(pool['max_in_flight'], 5)
        self.assertEqual(pool['max_in_flight_global'], 2, '分档字段被弄丢了')

    def test_range_is_validated(self) -> None:
        """与 max_in_flight 同区间；非法值必须拒绝而不是透传。"""
        self._write({'pool': {'max_in_flight_global': 2}})
        for good in (0, 2, 64):
            wb2api.save_upstream_config({'pool': {'max_in_flight_global': good}})
            self.assertEqual(self._read()['pool']['max_in_flight_global'], good)
        for bad in (-1, 65, 'x', True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wb2api.save_upstream_config({'pool': {'max_in_flight_global': bad}})


class DegradeConfigTest(unittest.TestCase):
    """连败降权参数（上游 cf1e7e5 新增，issue #114）。

    上游对 ErrClient / 传输层这类「只换号不罚」的失败累计计数，连续达阈就把账号
    **临时出池**（默认 5 次 / 10m，封顶 2h）。三个新键：`degrade_threshold`（整数）、
    `degrade_cooldown` 与 `degrade_cooldown_max`（时长字符串）。

    面板暂未提供输入框，但**手写在 config.json 里的值必须原样留住** ——
    保存设置页其它字段时把它弄丢，用户的调参会静默失效（这类「看着保存成功、
    实际丢字段」最难查）。另外 `degrade_threshold` 要享区间校验（同 breaker_threshold），
    时长键则靠既有的 `_cooldown` 后缀规则覆盖。

    注意上游把降权账号的 `/status.cooling` 置为 **true**（其 Cooling 口径含
    degradeUntil），所以界面无需改动就会显示「冷却中」——这里只钉配置读写。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self.cfg_path

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _write(self, data: dict) -> None:
        self.cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def _read(self) -> dict:
        return json.loads(self.cfg_path.read_text(encoding='utf-8'))

    def _seed(self) -> None:
        self._write({'pool': {
            'max_in_flight': 3, 'degrade_threshold': 5,
            'degrade_cooldown': '10m', 'degrade_cooldown_max': '2h',
        }})

    def test_all_three_keys_survive_sibling_save(self) -> None:
        """改 max_in_flight 时三个降权键都不能丢。"""
        self._seed()
        wb2api.save_upstream_config({'pool': {'max_in_flight': 5}})
        pool = self._read()['pool']
        self.assertEqual(pool['max_in_flight'], 5)
        self.assertEqual(pool['degrade_threshold'], 5)
        self.assertEqual(pool['degrade_cooldown'], '10m')
        self.assertEqual(pool['degrade_cooldown_max'], '2h')

    def test_threshold_writable_and_range_validated(self) -> None:
        self._seed()
        wb2api.save_upstream_config({'pool': {'degrade_threshold': 8}})
        self.assertEqual(self._read()['pool']['degrade_threshold'], 8)
        for bad in (-1, 0, 101, 'x', True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wb2api.save_upstream_config({'pool': {'degrade_threshold': bad}})

    def test_duration_keys_reject_bad_format(self) -> None:
        """时长键由既有的 `_cooldown` 后缀规则兜底 —— 确认它真的管到这两个新键。

        写错格式（例如中文「10分钟」）会让上游按 0 处理，表现为「降权不生效」，
        而界面上看不出任何异常。
        """
        self._seed()
        wb2api.save_upstream_config({'pool': {'degrade_cooldown': '30s'}})
        self.assertEqual(self._read()['pool']['degrade_cooldown'], '30s')
        for bad in ('十分钟', '10', '', 'abc'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wb2api.save_upstream_config({'pool': {'degrade_cooldown': bad}})


class CostExploreIntervalTest(unittest.TestCase):
    """`pool.cost_explore_interval`（上游 2026-09-17 新增）的校验与会话。

    这个键有个别的时长键没有的**危险形态**：上游对它是
    `time.ParseDuration` 失败即**启动报错**（`cmd/server/config.go:383`），
    而它既不匹配按 `_cooldown`/`_rate` 后缀的那条规则，也不在任何区间表里 ——
    不单独登记就等于「保存成功，然后上游起不来」，比当场拒绝难查得多。
    可达路径是 `POST /api/settings/upstream` 直接透传 body，不经过前端表单。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.UPSTREAM_CONFIG
        self.cfg_path = Path(self._tmp.name) / 'config.json'
        config.UPSTREAM_CONFIG = self.cfg_path
        self.cfg_path.write_text(json.dumps({'pool': {}}, ensure_ascii=False), encoding='utf-8')

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        self._tmp.cleanup()

    def _read(self) -> dict:
        return json.loads(self.cfg_path.read_text(encoding='utf-8'))

    def test_valid_durations_accepted(self) -> None:
        for good in ('30m', '1h', '45s', '48h'):
            with self.subTest(good=good):
                wb2api.save_upstream_config({'pool': {'cost_explore_interval': good}})
                self.assertEqual(self._read()['pool']['cost_explore_interval'], good)

    def test_zero_disables_instead_of_being_rejected(self) -> None:
        """`"0"` 是**合法值**（关停该特性），不能当格式错误拒掉。

        上游显式支持它（`CostExploreIntervalDur = 0` 表示关停，回到旧行为），
        把它拒掉等于用户没法关掉这个特性。
        """
        wb2api.save_upstream_config({'pool': {'cost_explore_interval': '0'}})
        self.assertEqual(self._read()['pool']['cost_explore_interval'], '0')

    def test_empty_falls_back_to_upstream_default(self) -> None:
        """留空 = 用上游默认（30m），与上游「空值回落默认」的行为一致。"""
        wb2api.save_upstream_config({'pool': {'cost_explore_interval': ''}})
        self.assertEqual(self._read()['pool']['cost_explore_interval'], '')

    def test_bad_format_rejected(self) -> None:
        """格式错误必须**当场拒绝** —— 放行会让上游启动失败。"""
        for bad in ('not-a-duration', '30', '-5m', '十分钟', '1.5h', '7d'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wb2api.save_upstream_config({'pool': {'cost_explore_interval': bad}})

    def test_survives_sibling_save(self) -> None:
        """改同节其它键时不能把它丢掉。"""
        wb2api.save_upstream_config({'pool': {'cost_explore_interval': '1h'}})
        wb2api.save_upstream_config({'pool': {'max_in_flight': 5}})
        pool = self._read()['pool']
        self.assertEqual(pool['cost_explore_interval'], '1h')
        self.assertEqual(pool['max_in_flight'], 5)


if __name__ == '__main__':
    unittest.main()

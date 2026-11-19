"""会在上游启动时炸掉的配置值，必须在**保存时**就拦住（issue #62）。

## 报告者的现场

他在「设置 → 上游」的提示词那一栏粘了整段提示词正文并保存。上游对 `prompt.file`
是 **fail-fast**：路径非空但读不到就启动报错退出（其 `normalizePrompt` 注释写明
「避免静默回落到内置默认」）。于是容器进入 `Restarting` 崩溃循环、整个反代不可用，
日志里是 `open read ...: file name too long`——文件名上限 255 字节，而提示词正文
远超它。

## 为什么这属于本端的问题

那一栏在界面上是「自定义提示词文件」，也写了「填路径」的提醒，但**保存时不校验**：
错的值会被原样写进上游 config.json，然后上游起不来。这与 `pool.cost_explore_interval`
（上游对它是 `time.ParseDuration` 失败即启动报错）是同一类，那次已经补过一条校验，
这次把这一类一次找齐。

## 上游启动会报错的配置项（逐个核对过）

| 上游校验位置 | 字段 | 本端 |
|---|---|---|
| `cooldown.soft_rate` / `_max` | 时长 | 已有后缀规则 ✓ |
| `pool.breaker_cooldown` / `_max` | 时长 | 同上 ✓ |
| `pool.degrade_cooldown` / `_max` | 时长 | 同上 ✓ |
| `session_sticky.ttl` / `gc_interval` | 时长 | 已有显式规则 ✓ |
| `pool.expiring_soon` | 时长 | 已有显式规则 ✓ |
| `pool.cost_explore_interval` | 时长 | 已有显式规则 ✓ |
| `admin.enabled` + 空 api_key | 组合 | 已有规则 ✓ |
| **`prompt.mode`** | 枚举 | **本次补** |
| **`prompt.file`** | 路径 | **本次补**（issue #62 本体） |

也就是说：这一类里已经**没有漏网**的字段了——这条注释本身就是那份清单，将来上游
新增 fail-fast 字段时，照着它补一行、加一条用例。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services.wb2api import _sanitize_section  # noqa: E402


class PromptFileTest(unittest.TestCase):
    """`prompt.file` 是文件路径，不是提示词正文（issue #62）。"""

    def test_empty_means_builtin_default(self) -> None:
        """留空 = 用上游内置默认提示词，必须放行。"""
        self.assertEqual(_sanitize_section('prompt', {'file': ''})['file'], '')

    def test_container_path_passes(self) -> None:
        out = _sanitize_section('prompt', {'file': '/app/data/prompt-custom.txt'})
        self.assertEqual(out['file'], '/app/data/prompt-custom.txt')

    def test_relative_path_passes(self) -> None:
        out = _sanitize_section('prompt', {'file': 'prompts/mine.md'})
        self.assertEqual(out['file'], 'prompts/mine.md')

    def test_whitespace_is_trimmed(self) -> None:
        out = _sanitize_section('prompt', {'file': '  /app/x.txt  '})
        self.assertEqual(out['file'], '/app/x.txt')

    def test_multiline_prose_rejected(self) -> None:
        """含换行 = 一定是正文（路径不可能有换行）——报告者踩的就是这个。"""
        for bad in ('你是一个助手\n请遵守规则', '第一行\r\n第二行', 'a\x00b'):
            with self.subTest(value=bad[:12]):
                with self.assertRaises(ValueError) as ctx:
                    _sanitize_section('prompt', {'file': bad})
                self.assertIn('路径', str(ctx.exception))

    def test_overlong_value_rejected(self) -> None:
        """超过 255 字节 = 撞文件名上限（用户看到的就是 file name too long）。"""
        with self.assertRaises(ValueError) as ctx:
            _sanitize_section('prompt', {'file': '你是一个很有用的助手，' * 30})
        self.assertIn('255', str(ctx.exception))

    def test_length_is_measured_in_bytes_not_chars(self) -> None:
        """按**字节**算：中文一个字三字节，90 个字就超了。

        若错按字符数算，一段 90 字的中文提示词会被放过，上游照样崩。
        """
        prose = '好' * 90          # 270 字节 > 255，字符数只有 90
        with self.assertRaises(ValueError):
            _sanitize_section('prompt', {'file': prose})
        ok = 'a' * 200             # 200 字节，放行
        self.assertEqual(_sanitize_section('prompt', {'file': ok})['file'], ok)

    def test_boundary_255_bytes_passes(self) -> None:
        """正好 255 字节放行（判据是「超过」才拒）。"""
        val = '/' + 'a' * 254
        self.assertEqual(len(val.encode('utf-8')), 255)
        self.assertEqual(_sanitize_section('prompt', {'file': val})['file'], val)

    def test_message_has_no_markdown(self) -> None:
        """文案会原样出现在提示条里（纯文本），不能带 markdown 记号。"""
        for bad in ('正文\n换行', '你' * 200):
            with self.subTest():
                with self.assertRaises(ValueError) as ctx:
                    _sanitize_section('prompt', {'file': bad})
                self.assertNotIn('**', str(ctx.exception))
                self.assertNotIn('`', str(ctx.exception))


class PromptModeTest(unittest.TestCase):
    """`prompt.mode` 非法值同样会让上游起不来（其 config.go 的启动校验）。"""

    def test_valid_modes(self) -> None:
        for mode in ('passthrough', 'custom', 'append'):
            with self.subTest(mode=mode):
                self.assertEqual(_sanitize_section('prompt', {'mode': mode})['mode'], mode)

    def test_case_is_normalised(self) -> None:
        self.assertEqual(_sanitize_section('prompt', {'mode': ' CUSTOM '})['mode'], 'custom')

    def test_invalid_mode_rejected(self) -> None:
        for bad in ('bogus', '', 'custom2', 'passthrough '):
            with self.subTest(mode=bad):
                if bad.strip().lower() in ('passthrough', 'custom', 'append'):
                    continue          # 去掉空白后是合法值，走归一化那条路
                with self.assertRaises(ValueError) as ctx:
                    _sanitize_section('prompt', {'mode': bad})
                self.assertIn('passthrough', str(ctx.exception))

    def test_missing_mode_untouched(self) -> None:
        """没传这个键时不该凭空写入一个值（保存是增量 patch）。"""
        self.assertNotIn('mode', _sanitize_section('prompt', {}))


class NoFailFastFieldLeftUncheckedTest(unittest.TestCase):
    """这一类字段的清单（模块注释里那张表）不能有漏网——这里逐条钉住。"""

    def test_duration_fields_rejected_when_malformed(self) -> None:
        cases = [
            ('cooldown', {'soft_rate': '十分钟'}),
            ('cooldown', {'soft_rate_max': '10'}),
            ('pool', {'breaker_cooldown': 'abc'}),
            ('pool', {'degrade_cooldown_max': '1x'}),
            ('session_sticky', {'ttl': 'forever'}),
            ('session_sticky', {'gc_interval': '5'}),
            ('pool', {'expiring_soon': '一周'}),
            ('pool', {'cost_explore_interval': '半小时'}),
        ]
        for section, incoming in cases:
            with self.subTest(section=section, incoming=incoming):
                with self.assertRaises(ValueError):
                    _sanitize_section(section, incoming)

    def test_duration_fields_accept_valid(self) -> None:
        cases = [
            ('cooldown', {'soft_rate': '600s', 'soft_rate_max': '2h'}),
            ('pool', {'breaker_cooldown': '10m', 'degrade_cooldown_max': '24h'}),
            ('session_sticky', {'ttl': '30m', 'gc_interval': '5m'}),
            ('pool', {'expiring_soon': '168h'}),
            ('pool', {'cost_explore_interval': '30m'}),
        ]
        for section, incoming in cases:
            with self.subTest(section=section, incoming=incoming):
                out = _sanitize_section(section, incoming)
                for k, v in incoming.items():
                    self.assertEqual(out[k], v)


if __name__ == '__main__':
    unittest.main()

class SavePathTest(unittest.TestCase):
    """走真实的保存路径：坏值**不能落到上游 config.json**（否则上游起不来）。"""

    def setUp(self) -> None:
        import json
        import tempfile

        from server import config
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.UPSTREAM_CONFIG
        self.path = Path(self._tmp.name) / 'config.json'
        config.UPSTREAM_CONFIG = self.path
        self.path.write_text(json.dumps({'prompt': {'mode': 'passthrough', 'file': ''}},
                                        ensure_ascii=False), encoding='utf-8')

    def tearDown(self) -> None:
        from server import config
        config.UPSTREAM_CONFIG = self._orig
        self._tmp.cleanup()

    def _read(self) -> dict:
        import json
        return json.loads(self.path.read_text(encoding='utf-8'))

    def test_prose_never_reaches_the_config_file(self) -> None:
        """报告者的那一步：粘正文保存 —— 必须被拒，且**文件一个字节都不改**。

        若只抛错但已经把前半段写进去，上游照样起不来；所以这里连文件内容一起断言。
        """
        from server.services import wb2api
        before = self.path.read_text(encoding='utf-8')
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config(
                {'prompt': {'file': '你是一个助手' + chr(10) + '请遵守规则'}})
        self.assertEqual(self.path.read_text(encoding='utf-8'), before)
        self.assertEqual(self._read()['prompt']['file'], '')

    def test_bad_mode_never_reaches_the_config_file(self) -> None:
        from server.services import wb2api
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'prompt': {'mode': 'bogus'}})
        self.assertEqual(self._read()['prompt']['mode'], 'passthrough')

    def test_valid_values_are_written(self) -> None:
        from server.services import wb2api
        wb2api.save_upstream_config({'prompt': {'mode': 'custom',
                                                'file': '/app/data/my-prompt.txt'}})
        prompt = self._read()['prompt']
        self.assertEqual(prompt['mode'], 'custom')
        self.assertEqual(prompt['file'], '/app/data/my-prompt.txt')

    def test_sibling_keys_survive(self) -> None:
        """拒绝一个坏值时，同一次请求里的**合法**键也不该被写进去（要么全拒、要么全收）。"""
        from server.services import wb2api
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'prompt': {'mode': 'custom',
                                                    'file': '正文' + chr(10) + '第二行'}})
        self.assertEqual(self._read()['prompt']['mode'], 'passthrough')

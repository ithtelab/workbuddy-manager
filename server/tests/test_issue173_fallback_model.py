"""模型映射的兜底模型（issue #173）。

## 问题

接入 Codex 这类客户端时，它会用一些我们映射表里没有的模型名（如 `gpt-6-luna`）
去调工具。旧行为是**原样透传**给上游 → 上游报模型不可用，用户只能一个个往映射表
里补；模型越加越多，补不过来。

## 修法

映射表里键为 `*` 的那条 = 兜底：精确别名没命中时用它，都没有才原样透传。
副作用（**拼错的模型名也会被接住**，见 `mapNewAliasDesc` 文案）是与报错人确认过
的取舍，不是疏漏。

## 这个文件钉住的三件事

1. 查表顺序：精确别名 → 兜底 → 原样；没配兜底时行为与改动前**逐字一致**。
2. `*` 不会被当成「真实别名」漏进白名单校验（它是键，不是模型名）。
3. 保存时拒绝「目标写成 `*`」——那会让 `*` 作为模型名发给上游，而界面显示
   "已设置兜底"，用户从报错里反推不出原因。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import gateway as G  # noqa: E402
from server.services import modelmap  # noqa: E402

ALIAS = 'gpt-4o-mini'
TARGET = 'glm-5.2'
FALLBACK = 'deepseek-v4.1-flash'


def _with_map(mapping):
    """把「数据库里的 model_map」替换成给定值（其余 get_setting 走真实实现）。"""
    real = G.db.get_setting

    def fake(key, default=None):
        if key == 'model_map':
            return mapping
        return real(key, default)

    return mock.patch.object(G.db, 'get_setting', side_effect=fake)


class FallbackLookupTest(unittest.TestCase):
    def test_exact_alias_wins_over_fallback(self) -> None:
        with _with_map({ALIAS: TARGET, modelmap.FALLBACK_KEY: FALLBACK}):
            self.assertEqual(G._map_model(ALIAS), TARGET)

    def test_unmapped_name_falls_back(self) -> None:
        """issue #173 的正文场景：Codex 发一个我们没映射过的名字。"""
        with _with_map({modelmap.FALLBACK_KEY: FALLBACK}):
            self.assertEqual(G._map_model('gpt-6-luna'), FALLBACK)

    def test_without_fallback_behaviour_is_unchanged(self) -> None:
        """没配兜底 = 旧行为（原样透传）。护栏：这个功能必须是纯增量。"""
        with _with_map({ALIAS: TARGET}):
            self.assertEqual(G._map_model('gpt-6-luna'), 'gpt-6-luna')

    def test_typo_is_caught_by_fallback(self) -> None:
        """已与报错人确认的副作用：拼错的模型名也会被兜底接住。"""
        with _with_map({ALIAS: 'glm-5.2', modelmap.FALLBACK_KEY: FALLBACK}):
            self.assertEqual(G._map_model('glm-5.3'), FALLBACK)

    def test_empty_and_broken_settings_do_not_blow_up(self) -> None:
        """空表 / 非对象 / 空值条目都按「没有映射」处理，不能让网关 5xx。"""
        for broken in ({}, None, 'nonsense', [], {ALIAS: ''}, {modelmap.FALLBACK_KEY: ''}):
            with self.subTest(broken=broken), _with_map(broken):
                self.assertEqual(G._map_model('gpt-6-luna'), 'gpt-6-luna')

    def test_none_model_passes_through(self) -> None:
        with _with_map({modelmap.FALLBACK_KEY: FALLBACK}):
            self.assertIsNone(G._map_model(None))

    def test_fallback_applies_to_non_string_keys_values(self) -> None:
        """json 列里混进数字/非字符串（手工改库）时不该把上游打成 500。"""
        with _with_map({1: 2, modelmap.FALLBACK_KEY: FALLBACK}):
            self.assertEqual(G._map_model('gpt-6-luna'), FALLBACK)
            self.assertEqual(G._map_model('1'), '2')


class FallbackAliasTest(unittest.TestCase):
    def test_fallback_key_is_not_an_alias(self) -> None:
        """`*` 是键不是模型名：白名单校验里不能拿它当"合法的别名"放行。"""
        aliases = modelmap.aliases({ALIAS: TARGET, modelmap.FALLBACK_KEY: FALLBACK})
        self.assertEqual(aliases, [ALIAS])

    def test_fallback_of_empty_table_is_none(self) -> None:
        for empty in ({}, None, 'nonsense', []):
            with self.subTest(empty=empty):
                self.assertIsNone(modelmap.fallback(empty))


class FallbackValidateTest(unittest.TestCase):
    def test_valid_table_round_trips(self) -> None:
        raw = {ALIAS: TARGET, modelmap.FALLBACK_KEY: FALLBACK}
        self.assertEqual(modelmap.validate(raw), raw)

    def test_target_may_not_be_the_fallback(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            modelmap.validate({modelmap.FALLBACK_KEY: modelmap.FALLBACK_KEY})
        self.assertIn('兜底', str(ctx.exception))

    def test_blank_entries_are_dropped(self) -> None:
        """旧的 save 行为保持：空键或空值的条目直接丢掉，不落库。"""
        self.assertEqual(modelmap.validate({'  ': TARGET, ALIAS: '  ', ALIAS: TARGET}), {ALIAS: TARGET})

    def test_whitespace_is_trimmed(self) -> None:
        self.assertEqual(modelmap.validate({f' {ALIAS} ': f' {TARGET} '}), {ALIAS: TARGET})

    def test_non_object_is_rejected(self) -> None:
        for bad in (None, 'x', [1], 3):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                modelmap.validate(bad)


class AliasEntriesUseFallbackTest(unittest.TestCase):
    """白名单里写了别名时补的模型条目，也要认兜底。

    否则配了兜底之后「用户把别名写进白名单」仍会拿到空清单，与"能调用"自相矛盾
    （issue #46 修的就是这个错位）。
    """

    def test_whitelist_unknown_name_with_fallback_is_listed(self) -> None:
        items = [{'id': FALLBACK, 'name': 'DeepSeek'}, {'id': 'glm-5.2'}]
        with mock.patch.object(G.db, 'get_setting',
                               side_effect=lambda key, default=None: (
                                   {modelmap.FALLBACK_KEY: FALLBACK} if key == 'model_map' else default)):
            out = G._alias_entries(items, ['gpt-6-luna'])
        self.assertEqual([e['id'] for e in out], ['gpt-6-luna'])
        self.assertEqual(out[0]['name'], 'DeepSeek')

    def test_no_fallback_keeps_old_behaviour(self) -> None:
        items = [{'id': FALLBACK}]
        with _with_map({ALIAS: TARGET}):
            self.assertEqual(G._alias_entries(items, ['gpt-6-luna']), [])


class SaveEndpointStatusTest(unittest.TestCase):
    """保存接口把校验失败做成 **400**（不是 500）。

    浏览器验收里真踩到过：直接调 `POST /api/settings/model-map` 写 `{"*": "*"}`
    得到的是 500 空 body —— 未捕获的 ValueError 被 FastAPI 当成服务器故障，界面
    上就只剩「服务器错误」，用户拿不到「该改什么」那句话。
    """

    def test_invalid_target_returns_400_with_reason(self) -> None:
        from fastapi import HTTPException

        from server.routers import settings as S  # noqa: PLC0415
        with self.assertRaises(HTTPException) as ctx:
            S.save_model_map({'*': '*'}, user={'role': 'admin'})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn('兜底', str(ctx.exception.detail))


class MapUiSourceTest(unittest.TestCase):
    """设置页必须让用户看得见兜底这条（否则功能等于不存在）。"""

    def test_ui_labels_the_fallback_row(self) -> None:
        src = (Path(__file__).resolve().parents[2]
               / 'web' / 'app' / '(main)' / 'settings' / 'layout.tsx').read_text(encoding='utf-8')
        self.assertIn("alias === '*'", src, '映射表里 `*` 那条没被单独标注成兜底')
        self.assertIn("t('settings.mapFallbackRow')", src, '兜底那条缺少可读标签')

    def test_ui_copy_warns_about_typos(self) -> None:
        import json
        loc = (Path(__file__).resolve().parents[2]
               / 'web' / 'lib' / 'i18n' / 'locales' / 'zh-CN.json')
        text = json.loads(loc.read_text(encoding='utf-8'))['settings']['mapNewAliasDesc']
        self.assertIn('*', text, '新增映射的说明里没提 `*` = 兜底')
        self.assertIn('拼错', text, '没写明「拼错的模型名也会被接住」这个副作用')


if __name__ == '__main__':
    unittest.main()

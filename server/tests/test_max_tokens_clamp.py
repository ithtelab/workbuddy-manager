"""max_tokens 上限钳制：越界值不得原样转发给上游。

背景
----
上游的 `Request.max_tokens` 是 **int32**（上限 2147483647）。客户端若按
「剩余额度 → max_tokens」做减法（实测见过 `1e10 - 已消耗`，约 99.99 亿，
是上限的 4.66 倍），上游拒绝时**不是回 400**，而是把它包成
`no_healthy_account` + 503。

后果有两层：

1. 客户端拿到 503，语义是「服务暂时不可用」，于是**无退避重试**；
2. 网关换账号重试也无效——参数越界不是账号问题，换哪个账号都一样。

网关改不到客户端的换算逻辑，但可以在转发前把值压回上限：这类越界值
一律是算错的，钳到 1e6 不影响任何正常请求（上游 /v1/models 实测最大
`max_output_tokens` 为 131072，1e6 有约 7 倍余量）。

本文件是**纯函数级**用例，不依赖上游，可离线运行。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import gateway  # noqa: E402
from server.routers.anthropic import to_openai_request  # noqa: E402


class ClampMaxTokensTest(unittest.TestCase):
    """clamp_max_tokens：越界值压回上限，其余原样保留。"""

    def test_huge_value_clamped(self) -> None:
        """实测触发值（约 99.99 亿）必须被压回上限。"""
        body = {'max_tokens': 9999969112}
        gateway.clamp_max_tokens(body)
        self.assertEqual(body['max_tokens'], gateway.MAX_TOKENS_CEILING)

    def test_just_above_int32_clamped(self) -> None:
        """刚过 int32 上限（2147483648）就要拦住——这是上游的分界点。"""
        body = {'max_tokens': 2147483648}
        gateway.clamp_max_tokens(body)
        self.assertEqual(body['max_tokens'], gateway.MAX_TOKENS_CEILING)

    def test_beyond_ceiling_clamped(self) -> None:
        """超过阈值的值一律压回。"""
        for value in (1_000_001, 2147483648, 10 ** 10, 10 ** 18):
            with self.subTest(value=value):
                body = {'max_tokens': value}
                gateway.clamp_max_tokens(body)
                self.assertEqual(body['max_tokens'], gateway.MAX_TOKENS_CEILING)

    def test_above_model_max_but_below_ceiling_untouched(self) -> None:
        """略超模型上限（实测最大 131072）但没过阈值的，是**用户真填的值**，不拦。

        上游会自行按模型上限拒绝或截断；网关只兜「算错的巨值」这一类，
        不去复刻每个模型的 max_output_tokens——那需要每次请求都查模型元数据，
        代价与收益不成比例，且模型清单会变。
        """
        for value in (131073, 200000, 999999):
            with self.subTest(value=value):
                body = {'max_tokens': value}
                gateway.clamp_max_tokens(body)
                self.assertEqual(body['max_tokens'], value)

    def test_normal_value_untouched(self) -> None:
        """正常取值必须逐字不动——钳制不能改变既有请求语义。"""
        for value in (1, 100, 4096, 131072, 1_000_000):
            with self.subTest(value=value):
                body = {'max_tokens': value}
                gateway.clamp_max_tokens(body)
                self.assertEqual(body['max_tokens'], value)

    def test_responses_field_clamped_too(self) -> None:
        """/v1/responses 用的是 `max_output_tokens`，同样要拦。"""
        body = {'max_output_tokens': 9999969112}
        gateway.clamp_max_tokens(body)
        self.assertEqual(body['max_output_tokens'], gateway.MAX_TOKENS_CEILING)

    def test_both_fields_clamped(self) -> None:
        """两个字段同时出现时都要压回。"""
        body = {'max_tokens': 10 ** 10, 'max_output_tokens': 10 ** 10}
        gateway.clamp_max_tokens(body)
        self.assertEqual(body['max_tokens'], gateway.MAX_TOKENS_CEILING)
        self.assertEqual(body['max_output_tokens'], gateway.MAX_TOKENS_CEILING)

    def test_max_completion_tokens_clamped(self) -> None:
        """OpenAI 较新的 `max_completion_tokens` 同样按 int32 读，实测越界也 503。

        网关是透传的，客户端发什么就转发什么，不能因为本仓库没主动用过它
        就漏掉——部分客户端已默认用这个名字。
        """
        body = {'max_completion_tokens': 9999969112}
        gateway.clamp_max_tokens(body)
        self.assertEqual(body['max_completion_tokens'], gateway.MAX_TOKENS_CEILING)

    def test_max_completion_tokens_normal_untouched(self) -> None:
        body = {'max_completion_tokens': 4096}
        gateway.clamp_max_tokens(body)
        self.assertEqual(body['max_completion_tokens'], 4096)

    def test_all_three_fields_clamped(self) -> None:
        """三种协议的输出上限字段同时出现时都要压回。"""
        body = {'max_tokens': 10 ** 10,
                'max_completion_tokens': 10 ** 10,
                'max_output_tokens': 10 ** 10}
        gateway.clamp_max_tokens(body)
        for field in gateway._MAX_TOKENS_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(body[field], gateway.MAX_TOKENS_CEILING)

    def test_field_list_covers_all_protocols(self) -> None:
        """字段清单本身要钉住：漏一个协议就等于防护缺失。

        协议层用的是哪个名字（OpenAI / Anthropic / Responses）写在这里，
        以后新增协议时这里会先响。
        """
        self.assertEqual(set(gateway._MAX_TOKENS_FIELDS),
                         {'max_tokens', 'max_completion_tokens', 'max_output_tokens'})

    def test_non_numeric_left_to_upstream(self) -> None:
        """非数字不猜、不置默认，交回上游判——网关不替上游做协议决定。"""
        for value in (None, 'abc', '99999999999', [1], {'a': 1}):
            with self.subTest(value=value):
                body = {'max_tokens': value}
                gateway.clamp_max_tokens(body)
                self.assertEqual(body['max_tokens'], value)

    def test_bool_not_treated_as_int(self) -> None:
        """bool 是 int 的子类，但不能当成 token 数去钳。"""
        body = {'max_tokens': True}
        gateway.clamp_max_tokens(body)
        self.assertEqual(body['max_tokens'], True)

    def test_zero_and_negative_untouched(self) -> None:
        """0 与负数由上游报错（语义不变）；钳制只管「过大」这一种越界。"""
        for value in (0, -1, -9999999999):
            with self.subTest(value=value):
                body = {'max_tokens': value}
                gateway.clamp_max_tokens(body)
                self.assertEqual(body['max_tokens'], value)

    def test_missing_field_not_invented(self) -> None:
        """没给这个字段时不能凭空造一个。"""
        body = {'model': 'x'}
        gateway.clamp_max_tokens(body)
        self.assertNotIn('max_tokens', body)
        self.assertNotIn('max_output_tokens', body)

    def test_non_dict_body_is_noop(self) -> None:
        """非 dict 入参静默返回，不抛异常。"""
        for body in (None, 'x', 123, [1, 2]):
            with self.subTest(body=body):
                gateway.clamp_max_tokens(body)  # type: ignore[arg-type]

    def test_float_value_clamped(self) -> None:
        """浮点同样要拦（JSON 数字不保证是整数形态）。"""
        body = {'max_tokens': 9.99e9}
        gateway.clamp_max_tokens(body)
        self.assertEqual(body['max_tokens'], gateway.MAX_TOKENS_CEILING)


class AnthropicLayerClampTest(unittest.TestCase):
    """转换层拿到的是已收敛的值（入口钳制 + 转换复制）。"""

    def test_conversion_keeps_clamped_value(self) -> None:
        """先钳后转：`to_openai_request` 不应再产出越界值。"""
        body = {'model': 'x', 'max_tokens': 9999969112,
                'messages': [{'role': 'user', 'content': 'hi'}]}
        gateway.clamp_max_tokens(body)
        out = to_openai_request(body)
        self.assertEqual(out['max_tokens'], gateway.MAX_TOKENS_CEILING)
        self.assertLessEqual(out['max_tokens'], 2147483647)

    def test_conversion_without_clamp_still_passthrough(self) -> None:
        """转换函数本身仍是纯复制（钳制在入口，职责不重叠）。"""
        out = to_openai_request({'model': 'x', 'max_tokens': 9999969112,
                                 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(out['max_tokens'], 9999969112)


class CeilingSanityTest(unittest.TestCase):
    """阈值本身的合理性——防止后人随手调大导致防护失效。"""

    def test_ceiling_below_int32(self) -> None:
        """必须低于 int32 上限，否则等于没设防。"""
        self.assertLess(gateway.MAX_TOKENS_CEILING, 2147483647)

    def test_ceiling_above_known_model_max(self) -> None:
        """必须高于已知模型的最大输出上限（实测 131072），不能误伤。"""
        self.assertGreater(gateway.MAX_TOKENS_CEILING, 131072)


if __name__ == '__main__':
    unittest.main()

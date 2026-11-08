"""账号运行状态字段的透传（冷却剩余时间 / 被限流模型清单）。

背景：上游 `/status` 一直提供 `cool_remaining_sec`（冷却剩余秒数）与
`rate_limited_models`（被限流的模型清单），但管理端只取了 `cooling` 布尔值，
界面上只显示「冷却中」——用户既不知道要等多久，也不知道是哪个模型被限。

上游 2026-09-15 的改动让冷却时长**对齐上游明说的重置时刻**（不再靠固定基数
+ 指数退避猜），这个值的可信度进一步提高，值得展示出来。

本文件锁住：这两个字段要如实透传，且**缺省/异常输入不崩**（上游旧版本或
字段缺失时界面仍要正常）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]

from server.services import wb2api  # noqa: E402


class CoolRemainingPassthroughTest(unittest.TestCase):
    """cool_remaining_sec 应透传为正数秒；缺失/0/负数 → None（不显示倒计时）。"""

    def _merge(self, pool_item: dict) -> dict:
        accounts = [{'uid': 'u1'}]
        wb2api.merge_pool_status(accounts, {'accounts': [dict(pool_item, uid='u1')]})
        return accounts[0]

    def test_positive_seconds_passthrough(self) -> None:
        a = self._merge({'cooling': True, 'cool_remaining_sec': 1800})
        self.assertTrue(a['cooling'])
        self.assertEqual(a['cool_remaining_sec'], 1800)

    def test_missing_field_is_none(self) -> None:
        """上游旧版本没有这个字段 → None，界面不显示倒计时（而不是显示 0 分钟）。"""
        a = self._merge({'cooling': True})
        self.assertIsNone(a['cool_remaining_sec'])

    def test_zero_and_negative_are_none(self) -> None:
        """0/负数没有展示意义（已到期），统一成 None 交给界面判断。"""
        for v in (0, -5):
            a = self._merge({'cooling': True, 'cool_remaining_sec': v})
            self.assertIsNone(a['cool_remaining_sec'], f'{v} 应归一为 None')

    def test_garbage_is_none_not_crash(self) -> None:
        """上游字段类型异常时不能让整页崩掉。"""
        for v in ('abc', None, [], {}):
            a = self._merge({'cooling': True, 'cool_remaining_sec': v})
            self.assertIsNone(a['cool_remaining_sec'], f'{v!r} 应归一为 None')

    def test_float_seconds_truncated_to_int(self) -> None:
        a = self._merge({'cooling': True, 'cool_remaining_sec': 90.7})
        self.assertEqual(a['cool_remaining_sec'], 90)


class RateLimitedModelsPassthroughTest(unittest.TestCase):
    """被限流的模型清单要原样透传（多模型时各自恢复时刻不同）。"""

    def _merge(self, pool_item: dict) -> dict:
        accounts = [{'uid': 'u1'}]
        wb2api.merge_pool_status(accounts, {'accounts': [dict(pool_item, uid='u1')]})
        return accounts[0]

    def test_list_passthrough(self) -> None:
        models = [
            {'model': 'glm-5.2', 'until': '2026-09-15T10:00:00+08:00'},
            {'model': 'deepseek-v4', 'reset_at': '2026-09-15T11:00:00+08:00'},
        ]
        a = self._merge({'cooling': True, 'rate_limited_models': models})
        self.assertEqual(len(a['rate_limited_models']), 2)
        self.assertEqual(a['rate_limited_models'][0]['model'], 'glm-5.2')

    def test_missing_is_empty_list(self) -> None:
        """缺失时给空列表而非 None —— 前端直接 map 不会炸。"""
        a = self._merge({'cooling': True})
        self.assertEqual(a['rate_limited_models'], [])

    def test_garbage_is_empty_list(self) -> None:
        for v in ('x', None, 5, {}):
            a = self._merge({'cooling': True, 'rate_limited_models': v})
            self.assertEqual(a['rate_limited_models'], [], f'{v!r} 应归一为 []')


class DegradePassthroughTest(unittest.TestCase):
    """连败降权字段要透传（用户反馈：「降权统计这里根本不统计」）。

    上游 issue #114 把**连续 N 次不罚号的失败**的账号临时移出池，并把它计入
    `cooling`（其 `entry.healthy()` 把 until / breakerUntil / degradeUntil 三个
    截止取或）。于是 `cooling` 是个混数：既有「等一会儿就好」的限流退避，也有
    「这个号在持续失败」的降权 —— 而释放的 `degrade_until` / `consecutive_fails`
    我们此前**完全没取**，界面上两者无法区分，用户看不到降权的存在。

    本类锁住这两个字段如实透传，且缺省/异常输入不崩。
    """

    def _merge(self, pool_item: dict) -> dict:
        accounts = [{'uid': 'u1'}]
        wb2api.merge_pool_status(accounts, {'accounts': [dict(pool_item, uid='u1')]})
        return accounts[0]

    def test_degrade_fields_passthrough(self) -> None:
        a = self._merge({
            'cooling': True,
            'degrade_until': '2026-09-18T12:30:00Z',
            'consecutive_fails': 3,
        })
        self.assertEqual(a['degrade_until'], '2026-09-18T12:30:00Z')
        self.assertEqual(a['consecutive_fails'], 3)

    def test_absent_degrade_until_is_none(self) -> None:
        """未降权时上游**整个键都不出现**（*time.Time + omitempty 的指针语义）。

        这里必须得到 None，而不是 Go 零值时间那种 "0001-01-01T00:00:00Z"
        ——后者在 JS 里是真值，会让「是否降权」的判定永远为真（同类坑见
        `availabilityOf` 里 last_success 的注释）。
        """
        a = self._merge({'cooling': True})
        self.assertIsNone(a['degrade_until'])
        self.assertIsNone(a['consecutive_fails'])

    def test_garbage_degrade_fields_do_not_crash(self) -> None:
        """上游字段类型异常时不能让整页崩掉。"""
        for v in (123, [], {}, True):
            a = self._merge({'cooling': True, 'degrade_until': v})
            self.assertIsNone(a['degrade_until'], f'{v!r} 应归一为 None')

    def test_empty_string_degrade_until_is_none(self) -> None:
        a = self._merge({'cooling': True, 'degrade_until': ''})
        self.assertIsNone(a['degrade_until'])

    def test_frontend_distinguishes_degrade_from_cooling(self) -> None:
        """前端要能把两者分开 —— 这是本条反馈的落点。

        静态校验共享模块里确实有 isDegraded，且文案键真的存在于字典：
        只看后端透传而前端没接，等于字段白透。
        """
        root = Path(__file__).resolve().parents[2]
        shared = (root / 'web' / 'lib' / 'account-status.ts').read_text(encoding='utf-8')
        self.assertIn('isDegraded', shared, '共享分档模块没有降权判定')
        self.assertIn('accounts.badgeDegraded', shared, '降权没有独立文案键')

        zh = (root / 'web' / 'lib' / 'i18n' / 'locales' / 'zh-CN.json').read_text(
            encoding='utf-8')
        self.assertIn('badgeDegraded', zh, '字典缺少降权徽章文案')
        self.assertIn('degradedReason', zh, '字典缺少降权原因说明')

    def test_degrade_until_used_not_just_presence(self) -> None:
        """判定必须看**截止时间是否在未来**，不能只看字段存在。

        前端拿到的可能是几十秒前的快照：字段还在、窗口已过。只看存在会把
        已恢复的账号一直显示成降权中。
        """
        root = Path(__file__).resolve().parents[2]
        shared = (root / 'web' / 'lib' / 'account-status.ts').read_text(encoding='utf-8')
        # 取 isDegraded 函数体，确认它做了时间比较
        start = shared.index('export function isDegraded')
        body = shared[start:start + 400]
        self.assertIn('Date.parse', body, '没有解析时间')
        self.assertIn('Date.now()', body, '没有与当前时间比较')


class NotInPoolTest(unittest.TestCase):
    """账号没进上游池时必须能识别出来（用户报的「面板全绿却报没有健康账号」）。

    我们读的是 auths/ 目录下的**文件**，上游读的才是**池**。两者不总一致：
    上游 `LoadDir` 对解析失败的 auth 文件静默跳过（`Parse` 在 accessToken 为
    空时报错），那个文件永远进不了池、永远选不中。

    此前这种账号在面板上走兜底分支显示「● 在线」——于是出现「面板全绿、调用
    却报没有健康账号」的矛盾（用户实测反馈）。
    """

    def _merge_pool(self, pool_items: list[dict]) -> dict:
        accounts = [{'uid': 'u1'}]
        wb2api.merge_pool_status(accounts, {'accounts': pool_items})
        return accounts[0]

    def test_account_absent_from_pool_is_marked(self) -> None:
        """上游没返回它 → in_pool 为 False，供界面标出「未加载」。"""
        self.assertIs(self._merge_pool([])['in_pool'], False)

    def test_account_present_in_pool_is_marked(self) -> None:
        self.assertIs(self._merge_pool([{'uid': 'u1', 'credits': 5}])['in_pool'], True)

    def _read_auths(self, payload: dict) -> list[dict]:
        import json
        import tempfile
        from pathlib import Path

        from server import config

        tmp = Path(tempfile.mkdtemp())
        (tmp / 'auths').mkdir()
        orig = config.AUTH_DIR
        config.AUTH_DIR = tmp / 'auths'
        try:
            (config.AUTH_DIR / 'workbuddy-t.json').write_text(
                json.dumps(payload), encoding='utf-8')
            return wb2api.list_auth_accounts()
        finally:
            config.AUTH_DIR = orig

    def test_missing_token_flagged_as_invalid(self) -> None:
        """accessToken 为空 → 上游 Parse 必拒；本地如实给出原因。"""
        got = self._read_auths({
            'auth': {'accessToken': '', 'expiresAt': 4102444800,
                     'domain': 'www.codebuddy.cn'},
            'account': {'uid': 'uid-x', 'nickname': 'x'},
        })
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]['invalid_reason'], '空 accessToken 应给出原因')
        self.assertIn('accessToken', got[0]['invalid_reason'])

    def test_valid_token_has_no_invalid_reason(self) -> None:
        """正常账号不该被误标 —— 否则所有账号都会显示成「未加载」。"""
        got = self._read_auths({
            'auth': {'accessToken': 'at-ok', 'expiresAt': 4102444800,
                     'domain': 'www.codebuddy.cn'},
            'account': {'uid': 'uid-y', 'nickname': 'y'},
        })
        self.assertEqual(got[0]['invalid_reason'], '')


if __name__ == '__main__':
    unittest.main()


class RateLimitedModelsTest(unittest.TestCase):
    """模型级限流（6004）要能在面板上单独看出来（issue #43）。

    现场：用户发现某个模型报限流（腾讯 6004），**换个模型就能继续用**，但面板上
    那个账号显示「在线」，完全看不出「这个模型被限流了」——只能进容器翻
    state.json 才知道。

    根因是前端只把模型明细挂在「冷却中」那一档下，而 6004 是**模型级**的：
    账号本身健康、仍会被选中转发，所以 `cooling` 是 false，那一档根本走不到。

    上游为此单独给了一本台账（`rate_limited_models`，其注释写明用途就是让运维
    看到「账号 A 的模型 X 还在限额中，预计 Z 时间恢复」）。本测试锁住：
      1. 台账在未来 → 算作受限；
      2. 台账已过期 → **不算**（快照可能过时，字段还在但窗口已过）；
      3. 缺 until / 非字符串 → 保守不算（宁可不报，也别报一个说不清时间的）。
    """

    def _src(self) -> str:
        return (_ROOT / 'web' / 'lib' / 'account-status.ts').read_text(encoding='utf-8')

    def _helper_body(self) -> str:
        src = self._src()
        seg = src[src.index('export function rateLimitedModels'):]
        return seg[:seg.index('\n}')]

    def test_helper_exists(self) -> None:
        self.assertIn('export function rateLimitedModels', self._src(),
                      'account-status.ts 里没有 rateLimitedModels')

    def test_judged_by_deadline_not_field_presence(self) -> None:
        """**按截止时间判**，不是「字段存在就算受限」。

        前端拿到的可能是几十秒前的快照：字段还在、窗口早过了。只看字段就会把
        已经恢复的模型一直标成「受限」（与 isDegraded 同一口径的坑）。
        """
        body = self._helper_body()
        self.assertIn('modelLimitEnd', body, '没有取统一的恢复时刻')
        self.assertIn('> now', body, '没有「截止在未来」的比较')
        # 时间解析在 modelLimitEnd 里（issue #409：until 与 reset_at 取较晚者）。
        module = self._src()
        self.assertIn('Date.parse', module, '没用时间解析 —— 过期的条目也会被算成受限')
        self.assertIn('reset_at', module, '没有认上游真实 reset_at')

    def test_requires_parseable_time(self) -> None:
        """缺时间 / 非字符串 / 解析不出来 → 不算（宁可不报，也别报个说不清时间的）。"""
        module = self._src()
        self.assertIn("typeof v !== 'string'", module)
        self.assertIn('return at === -Infinity ? null : at', module)

    def test_reads_upstream_ledger_not_account_cooling(self) -> None:
        """读的是上游那本台账字段（rate_limited_models），不是账号级 cooling。

        6004 是**模型级**的：账号健康、仍会被选中转发，所以 `cooling` 是 false ——
        用它当判据永远判不出「某个模型被限流」（这正是 issue #43 的现场）。
        """
        body = self._helper_body()
        self.assertIn('rate_limited_models', body,
                      '没有读上游台账 —— 账号级状态判不出模型级限流')
        self.assertNotIn('a.cooling', body,
                         '用账号级 cooling 判模型级限流是错的：账号健康、只是某模型受限')

    def test_badge_is_separate_from_status(self) -> None:
        """模型受限徽章必须与状态徽章**并列**，不能替代它。

        账号确实在线（其它模型能用），把状态改成「冷却中」是错的；
        但完全不提又会让用户看不出某个模型不可用。
        """
        page = (_ROOT / 'web' / 'app' / '(main)' / 'accounts' / 'page.tsx').read_text(
            encoding='utf-8')
        self.assertIn('renderModelLimit', page, '账号页没有模型受限的展示')
        # 两者在同一个容器里并列渲染
        # 两处渲染（桌面表格 / 移动端卡片）都必须并排带上
        count = page.count('{renderModelLimit(a)}')
        self.assertGreaterEqual(count, 2,
                                f'模型受限徽章只在 {count} 处渲染（桌面与移动端都要有）')

    def test_only_shown_when_actually_limited(self) -> None:
        """没有受限模型时不能渲染任何东西（否则每行都挂个空徽章）。"""
        page = (_ROOT / 'web' / 'app' / '(main)' / 'accounts' / 'page.tsx').read_text(
            encoding='utf-8')
        seg = page[page.index('function renderModelLimit'):]
        seg = seg[:seg.index('\n  }')]
        self.assertIn('if (!limited.length) return null', seg,
                      '没有「无受限模型则不渲染」的保护')

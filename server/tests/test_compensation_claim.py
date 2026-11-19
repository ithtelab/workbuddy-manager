"""活动补偿领取（「用量管理」页手动领取的那 500 积分的自动化）。

## 背景

网页入口 https://www.workbuddy.cn/profile/plans-usage 上的「领取」按钮，前端
bundle 里调的接口是：

    POST /billing/meter/compensation-status    查询（有没有得领 / 领过没）
    POST /billing/meter/claim-compensation     领取

上游（workbuddy2api）的活跃上报里已经在调 claim-compensation，但它挂在「活跃上报」
这个开关下 —— 用户关掉活跃上报、或用较旧的上游，就领不到。补偿**有时限**，漏了就
真的没了，所以管理端自己做一条不依赖上游开关的路，并默认开启。

## 这个文件钉住的三件事

1. **路径不带 `/v2`**（2026-10-16 实测）：`/v2/billing/meter/claim-compensation` 与
   `/v2/billing/meter/compensation-status` 都是 404，非前缀形式才是 401（存在）。
   所以**不能**复用 `billing_paths()`（它给国内版拼的正是 `/v2`）——照抄会永远 404，
   而 404 的表现是「无可领补偿」，与「今天没得领」一模一样，属于静默失效。
2. **业务错误不等于故障**：没有可领 / 已经领过都是业务错误，必须报成 `nothing`，
   不能报成「失败」——否则界面会显示「N 个账号失败」，用户反复重试一个永不成功的动作。
3. **默认关闭、手动为主**：上游的活跃上报（默认开）每天已经在领同一笔补偿，面板这条
   的价值是「**马上领**」——公告带截止日期，用户点一下就有结果，不用等到整点。
   默认开会让每个账号每天多一次注定「无可领」的写请求，所以默认关。
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import dailyplan, realm as realm_mod, tencent  # noqa: E402


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    def __init__(self, payload, *, raise_on_post: Exception | None = None) -> None:
        self.payload = payload
        self.raise_on_post = raise_on_post
        self.calls: list[dict] = []

    async def post(self, url, **kwargs):
        self.calls.append({'url': url, **kwargs})
        if self.raise_on_post is not None:
            raise self.raise_on_post
        return _FakeResponse(self.payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patched(payload, *, raise_on_post=None):
    """打桩 `_account_http_client`，返回 (patch 上下文, 假客户端)。"""
    client = _FakeClient(payload, raise_on_post=raise_on_post)
    return mock.patch.object(tencent, '_account_http_client', return_value=client), client


AUTH = {'access_token': 'tok', 'uid': 'u1', 'enterprise_id': 'ent', 'domain': 'copilot.tencent.com'}


class PathShapeTest(unittest.TestCase):
    """路径前缀：这两个端点**不能**带 `/v2`（实测依据见模块注释）。"""

    def test_paths_have_no_v2_prefix(self) -> None:
        for path in (tencent.COMPENSATION_STATUS_PATH, tencent.COMPENSATION_CLAIM_PATH):
            self.assertFalse(path.startswith('/v2'), f'{path} 带了 /v2 前缀 —— 域名上只有非前缀形式')
            self.assertTrue(path.startswith('/billing/meter/'), path)

    def test_billing_paths_helper_is_not_used_for_these(self) -> None:
        """结构护栏：`billing_paths()` 给国内版拼的正是 `/v2`。

        直接断言「没调用它」比断言 URL 字符串更耐久 —— 将来有人「统一一下端点
        拼法」时，会在这里被拦下，而不是等到线上每次领取都 404（表现为无可领）。
        """
        src = (Path(__file__).resolve().parents[2]
               / 'server' / 'services' / 'tencent.py').read_text(encoding='utf-8')
        start = src.index('async def compensation_status')
        end = src.index('async def fetch_credits')
        segment = src[start:end]
        self.assertNotIn('billing_paths(', segment,
                         '补偿端点走了 billing_paths()（会给 CN 拼 /v2 → 404）')

    def test_cn_url_is_codebuddy_without_v2(self) -> None:
        self.assertEqual(realm_mod.billing_base(realm_mod.CN), 'https://www.codebuddy.cn')
        self.assertEqual(
            realm_mod.billing_base(realm_mod.CN) + tencent.COMPENSATION_CLAIM_PATH,
            'https://www.codebuddy.cn/billing/meter/claim-compensation',
        )


class ClaimCompensationTest(unittest.IsolatedAsyncioTestCase):
    async def test_claimed_with_credit(self) -> None:
        patch, client = _patched({'code': 0, 'data': {'credit': 500}})
        with patch:
            status, credit, message = await tencent.claim_compensation(AUTH, realm_mod.CN)
        self.assertEqual((status, credit), ('claimed', 500))
        self.assertIn('500', message)
        self.assertEqual(client.calls[0]['url'],
                         'https://www.codebuddy.cn/billing/meter/claim-compensation')
        self.assertEqual(client.calls[0]['json'], {})

    async def test_claimed_without_credit_field_is_not_a_failure(self) -> None:
        """回执里没有 credit 字段 ≠ 没领到（上游可能只回一个空 data）。"""
        patch, _ = _patched({'code': 0, 'data': {}})
        with patch:
            status, credit, message = await tencent.claim_compensation(AUTH, realm_mod.CN)
        self.assertEqual((status, credit), ('claimed', 0))
        self.assertIn('已领取', message)

    async def test_business_error_is_nothing_not_failure(self) -> None:
        """没有可领 / 已领过都是业务错误 —— 报成 failed 会让用户反复重试。"""
        patch, _ = _patched({'code': 40001, 'msg': 'no compensation'})
        with patch:
            status, _credit, message = await tencent.claim_compensation(AUTH, realm_mod.CN)
        self.assertEqual(status, 'nothing')
        self.assertIn('40001', message)

    async def test_transport_error_is_failed(self) -> None:
        patch, _ = _patched(None, raise_on_post=RuntimeError('boom'))
        with patch:
            status, _credit, message = await tencent.claim_compensation(AUTH, realm_mod.CN)
        self.assertEqual(status, 'failed')
        self.assertIn('异常', message)

    async def test_global_account_skips_without_request(self) -> None:
        """公告只面向国内版：国际版不打接口（也就不会吃 4xx）。"""
        patch, client = _patched({'code': 0, 'data': {'credit': 500}})
        with patch:
            status, credit, message = await tencent.claim_compensation(AUTH, realm_mod.GLOBAL)
        self.assertEqual((status, credit), ('skipped', 0))
        self.assertEqual(client.calls, [], '国际版不该发出请求')
        self.assertIn('国际版', message)


class CompensationStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_ok(self) -> None:
        data = {'claimed': False, 'credit_num': 500, 'button_text': '领取'}
        patch, client = _patched({'code': 0, 'data': data})
        with patch:
            ok, got, _msg = await tencent.compensation_status(AUTH, realm_mod.CN)
        self.assertTrue(ok)
        self.assertEqual(got, data)
        self.assertEqual(client.calls[0]['url'],
                         'https://www.codebuddy.cn/billing/meter/compensation-status')

    async def test_status_business_error(self) -> None:
        patch, _ = _patched({'code': 10001, 'msg': 'x'})
        with patch:
            ok, data, msg = await tencent.compensation_status(AUTH, realm_mod.CN)
        self.assertFalse(ok)
        self.assertEqual(data, {})
        self.assertIn('10001', msg)

    async def test_status_unexpected_shape(self) -> None:
        patch, _ = _patched({'code': 0, 'data': 'nope'})
        with patch:
            ok, _data, msg = await tencent.compensation_status(AUTH, realm_mod.CN)
        self.assertFalse(ok)
        self.assertIn('结构', msg)

    async def test_status_transport_error(self) -> None:
        patch, _ = _patched(None, raise_on_post=RuntimeError('boom'))
        with patch:
            ok, _data, msg = await tencent.compensation_status(AUTH, realm_mod.CN)
        self.assertFalse(ok)
        self.assertIn('异常', msg)


class AlreadyClaimedTest(unittest.TestCase):
    def test_claimed_flag(self) -> None:
        self.assertTrue(tencent.compensation_already_claimed({'claimed': True}))
        self.assertFalse(tencent.compensation_already_claimed({'claimed': False}))

    def test_claimed_at_or_text(self) -> None:
        self.assertTrue(tencent.compensation_already_claimed({'claimed_at': 1760000000}))
        self.assertTrue(tencent.compensation_already_claimed({'claimed_text': '已于 10 月 16 日领取'}))

    def test_empty_values_are_not_claimed(self) -> None:
        self.assertFalse(tencent.compensation_already_claimed({'claimed_at': '', 'claimed_text': ''}))

    def test_unknown_shape_is_not_claimed(self) -> None:
        """认不出结构时返回 False —— 那表示要继续尝试领取，而不是放过这次机会。"""
        for broken in ({}, None, 'x', [], {'anything': 1}):
            with self.subTest(broken=broken):
                self.assertFalse(tencent.compensation_already_claimed(broken))


class DailyPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._patches = [
            mock.patch.object(dailyplan.db, 'get_setting', return_value={}),
            mock.patch.object(dailyplan.db, 'set_setting'),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_task_order_and_defaults(self) -> None:
        self.assertEqual(dailyplan.TASKS, ('checkin', 'compensation', 'claim', 'full'))
        plan = dailyplan.get_plan()
        self.assertFalse(plan['compensation']['enabled'],
                         '默认关闭：**上游的活跃上报每天已经在领同一笔补偿**，'
                         '面板这条是手动兜底（点「立即执行」），天天自动跑只是多一次空请求')
        self.assertEqual(plan['compensation']['hour'], 10)
        self.assertFalse(plan['full']['enabled'], '有风险的做任务必须默认关闭')

    def test_set_plan_requires_the_new_task(self) -> None:
        body = {t: {'enabled': False, 'hour': 9} for t in dailyplan.TASKS}
        body['compensation']['enabled'] = True
        plan = dailyplan.set_plan(body)
        self.assertTrue(plan['compensation']['enabled'])
        incomplete = {t: {'enabled': False, 'hour': 9} for t in ('checkin', 'claim', 'full')}
        with self.assertRaises(TypeError):
            dailyplan.set_plan(incomplete)

    def test_run_task_reports_counts(self) -> None:
        # 同步类里必须 asyncio.run 包一层：直接写 async def 的话 unittest 不会
        # await，用例**静默不执行**（曾经就这么漏掉过两条断言）。
        from server.routers import accounts as accounts_router
        out = {'claimed': 2, 'credit': 1000, 'already': 1, 'nothing': 3, 'failed': 0}
        with mock.patch.object(accounts_router, 'claim_compensation_all',
                               new_callable=mock.AsyncMock, return_value=out) as call:
            ok, message = asyncio.run(dailyplan.run_task('compensation'))
        self.assertTrue(ok, '「无可领」是正常态，不该算失败')
        self.assertIn('2 个领到 +1000 积分', message)
        self.assertIn('1 个已领过', message)
        call.assert_awaited_once()

    def test_run_task_fails_when_accounts_failed(self) -> None:
        from server.routers import accounts as accounts_router
        out = {'claimed': 0, 'credit': 0, 'already': 0, 'nothing': 1, 'failed': 2}
        with mock.patch.object(accounts_router, 'claim_compensation_all',
                               new_callable=mock.AsyncMock, return_value=out):
            ok, message = asyncio.run(dailyplan.run_task('compensation'))
        self.assertFalse(ok)
        self.assertIn('2 个失败', message)


class RouterWiringTest(unittest.TestCase):
    def test_manual_run_accepts_compensation(self) -> None:
        """「立即执行」的允许清单必须包含新任务（它只此一份：dailyplan.TASKS）。"""
        from fastapi import HTTPException

        from server.routers import accounts as accounts_router
        with mock.patch.object(dailyplan, 'run_task', new_callable=mock.AsyncMock,
                               return_value=(True, 'ok')) as run, \
                mock.patch.object(dailyplan, 'mark_ran', return_value={}):
            out = asyncio.run(
                accounts_router.run_daily_task_now({'task': 'compensation'}, user={'role': 'admin'}))
        self.assertTrue(out['ok'])
        run.assert_awaited_once_with('compensation')
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                accounts_router.run_daily_task_now({'task': 'nope'}, user={'role': 'admin'}))
        self.assertEqual(ctx.exception.status_code, 400)


class FrontendWiringTest(unittest.TestCase):
    """界面要有这一行，且文案键在五语字典里都在（否则切语言会露出键名）。"""

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[2]

    def test_task_type_and_table(self) -> None:
        types = (self.root / 'web' / 'lib' / 'types.ts').read_text(encoding='utf-8')
        self.assertIn("'compensation'", types)
        card = (self.root / 'web' / 'components' / 'common' / 'accounts'
                / 'RefreshPlanCard.tsx').read_text(encoding='utf-8')
        self.assertIn("key: 'compensation'", card)
        self.assertIn('accounts.dailyCompensation', card)
        self.assertIn('accounts.dailyCompensationNote', card)

    def test_copy_exists_in_every_locale(self) -> None:
        for loc in ('zh-CN', 'zh-TW', 'en', 'ja', 'ko'):
            data = json.loads((self.root / 'web' / 'lib' / 'i18n' / 'locales'
                               / f'{loc}.json').read_text(encoding='utf-8'))
            for key in ('dailyCompensation', 'dailyCompensationNote'):
                self.assertTrue(data['accounts'].get(key), f'{loc}.accounts.{key} 缺失')


if __name__ == '__main__':
    unittest.main()

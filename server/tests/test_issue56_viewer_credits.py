"""只读账号看到的积分口径（issue #56）。

## 报告的现象

同一批账号，**管理员登录**看到 2,071 / 6,420 / 9,353 / 3,322（带「缓存 52s 前」
标注），**只读账号登录**看到 2,070 / **0** / **0** / 3,323（没有标注）。两个账号
的积分直接变成 0，还被当成「余额耗尽」用红色渲染。

## 根因

`/api/accounts/refresh-credits` 整个是 `require_admin`：

  · 管理员页面加载 → 调得通（`force=false`）→ 界面用**实时值**；
  · 只读账号页面加载 → **403**，而前端那段 `.catch { /* 静默失败 */ }` 把它吞了
    → 界面回退到上游 `/status` 里的**快照值**（上游上次调度该账号时记下的，
    可能滞后数小时、也可能仍是 0）→ 显示 0 且标红。

也就是说：不是「积分查错了」，而是**只读账号根本没走到实时查询那条路**，界面上
没有任何提示。

## 修法（这里钉住的）

  · `force=false`（页面加载那条路，命中缓存优先）**任何已登录用户都能调** ——
    账号余额是只读信息，只读账号应当看到同一份数字；
  · `force=true`（显式点「刷新积分」，会强制对所有账号发起真实查询）
    **仍然只有管理员** —— 那是「主动触发外部调用」，不该由只读账号驱动；
  · 前端把快照值与实时值**分开标注**，且快照值不再用红色（「快照说 0」不等于
    「确实没积分」）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException  # noqa: E402

from server.routers import accounts  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_PAGE = _ROOT / 'web' / 'app' / '(main)' / 'accounts' / 'page.tsx'


def _call(role: str, *, force: bool) -> dict:
    """直接调端点函数，把 user 传进去（依赖注入由框架负责，这里只验角色逻辑）。"""
    with mock.patch.object(accounts.wb2api, 'list_auth_accounts',
                              lambda auth_dir=None: []):
        return asyncio.run(accounts.refresh_all_credits(force=force, user={'role': role}))


class RefreshCreditsPermissionTest(unittest.TestCase):
    """权限拆分：只读能用缓存路径，强制刷新仍限管理员。"""

    def test_viewer_can_use_cached_path(self) -> None:
        """只读账号 + force=false（页面加载）→ 放行。

        这条是 #56 的核心：此前它 403，界面静默回退到上游快照，于是同一个
        账号在两种角色下显示不同的积分。
        """
        out = _call('viewer', force=False)
        self.assertIn('credits', out)

    def test_viewer_cannot_force_refresh(self) -> None:
        """只读账号不能强制刷新（那是主动触发外部调用的动作）。"""
        with self.assertRaises(HTTPException) as ctx:
            _call('viewer', force=True)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn('管理员', str(ctx.exception.detail))

    def test_admin_can_force_refresh(self) -> None:
        self.assertIn('credits', _call('admin', force=True))

    def test_admin_can_use_cached_path(self) -> None:
        self.assertIn('credits', _call('admin', force=False))

    def test_unknown_role_treated_as_non_admin(self) -> None:
        """角色字段缺失/异常时按非管理员处理（不能因为字段怪就放行强制刷新）。"""
        for role in ('', 'guest', None):
            with self.subTest(role=role):
                with self.assertRaises(HTTPException):
                    with mock.patch.object(accounts.wb2api, 'list_auth_accounts',
                              lambda auth_dir=None: []):
                        asyncio.run(accounts.refresh_all_credits(
                            force=True, user={'role': role}))


class CreditsRenderHonestyTest(unittest.TestCase):
    """前端：快照值必须被单独标注，且不当作「余额耗尽」报警。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = _PAGE.read_text(encoding='utf-8')

    def test_snapshot_has_its_own_badge(self) -> None:
        self.assertIn("t('accounts.snapshot')", self.src)
        self.assertIn("t('accounts.snapshotTitle')", self.src)

    def test_snapshot_value_not_rendered_as_error(self) -> None:
        """快照值走 muted 色，**不进红色分支** —— 否则 0 会被当成故障。"""
        self.assertIn('fromSnapshot\n      ?', self.src)
        self.assertIn("'text-muted-foreground'", self.src)

    def test_live_path_unchanged(self) -> None:
        """有实时值时行为不变：仍然区分「实时 / 缓存 x 秒前」。"""
        self.assertIn("t('accounts.live')", self.src)
        self.assertIn("t('accounts.cacheAge'", self.src)

    def test_countdown_only_for_live_values(self) -> None:
        """积分到期倒计时只对实时值显示（快照没有 expiries 可算）。"""
        self.assertIn('fromSnapshot ? undefined : meta?.expiries', self.src)

    def test_phrases_in_all_locales(self) -> None:
        import json
        for loc in ('zh-CN', 'zh-TW', 'en', 'ja', 'ko'):
            d = json.loads((_ROOT / 'web' / 'lib' / 'i18n' / 'locales' / f'{loc}.json')
                           .read_text(encoding='utf-8'))['accounts']
            for key in ('snapshot', 'snapshotTitle'):
                self.assertTrue(d.get(key), f'{loc} 缺 accounts.{key}')


if __name__ == '__main__':
    unittest.main()

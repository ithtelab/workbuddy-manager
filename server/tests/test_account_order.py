"""账号自定义顺序（拖拽排序）的存储与重排。

钉住四件事：

  1. **归一化**：去重、去空白、拒掉非字符串 —— 顺序里混进重复 uid 会让某一项
     永远不动（名次表取第一次出现的位置），而界面上完全看不出原因。
  2. **按分组隔离**：一个分组 = 一套账号池，两个分组的账号不重叠；共用一份顺序
     会在切换分组时把对方排好的顺序打乱。
  3. **未登记的账号排在最后**：新加的号一定不在旧顺序里，排最前会把它顶到列表
     顶部、把用户排好的顺序整屏往下挤。
  4. **它是展示顺序**：只写 settings，不碰 auths 目录里的账号文件。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import accounts as accounts_router  # noqa: E402
from server.services import accountorder  # noqa: E402


class _Store:
    def __init__(self) -> None:
        self.store: dict = {}

    def get_setting(self, key: str, default=None):
        return self.store.get(key, default)

    def set_setting(self, key: str, value) -> None:
        self.store[key] = value


def _patch_db():
    store = _Store()
    return store, mock.patch.multiple(
        accountorder.db,
        get_setting=store.get_setting,
        set_setting=store.set_setting,
    )


class _Row:
    def __init__(self, uid: str) -> None:
        self.uid = uid


class NormalizeTest(unittest.TestCase):
    def test_drops_empty_and_duplicates_keeping_first(self) -> None:
        self.assertEqual(
            accountorder.normalize([' u1 ', 'u2', 'u1', '', '   ']),
            ['u1', 'u2'],
        )

    def test_rejects_non_list(self) -> None:
        for bad in ('u1', 3, {'a': 1}, True):
            with self.assertRaises(ValueError):
                accountorder.normalize(bad)

    def test_rejects_non_string_items(self) -> None:
        with self.assertRaises(ValueError):
            accountorder.normalize(['u1', 42])

    def test_none_is_empty(self) -> None:
        self.assertEqual(accountorder.normalize(None), [])

    def test_caps_entries(self) -> None:
        many = [f'u{i}' for i in range(accountorder.MAX_ENTRIES + 50)]
        self.assertEqual(len(accountorder.normalize(many)), accountorder.MAX_ENTRIES)


class SaveLoadTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        store, patcher = _patch_db()
        with patcher:
            self.assertEqual(accountorder.load(1), [])
            saved = accountorder.save(1, ['u3', 'u1', 'u2'])
            self.assertEqual(saved, ['u3', 'u1', 'u2'])
            self.assertEqual(accountorder.load(1), ['u3', 'u1', 'u2'])
        self.assertIn('accounts_order:1', store.store)

    def test_groups_are_isolated(self) -> None:
        _, patcher = _patch_db()
        with patcher:
            accountorder.save(1, ['a', 'b'])
            accountorder.save(2, ['b', 'a'])
            self.assertEqual(accountorder.load(1), ['a', 'b'])
            self.assertEqual(accountorder.load(2), ['b', 'a'])

    def test_default_group_uses_zero_key(self) -> None:
        # 默认分组的 group id 是 None（upstreamsvc.DEFAULT_ID），必须落到同一个键上，
        # 否则「默认分组」每次读写都像换了一个分组。
        store, patcher = _patch_db()
        with patcher:
            accountorder.save(None, ['x'])
            self.assertEqual(accountorder.load(0), ['x'])
            self.assertEqual(accountorder.load(None), ['x'])

    def test_broken_stored_value_loads_as_empty(self) -> None:
        store, patcher = _patch_db()
        store.store['accounts_order:1'] = 'not-a-list'
        with patcher:
            self.assertEqual(accountorder.load(1), [])


class ApplyTest(unittest.TestCase):
    def test_known_first_unknown_last(self) -> None:
        rows = [_Row(uid) for uid in ('a', 'b', 'c', 'd')]
        got = [r.uid for r in accountorder.apply(rows, ['c', 'a'])]
        self.assertEqual(got, ['c', 'a', 'b', 'd'])

    def test_unknown_keep_original_relative_order(self) -> None:
        rows = [_Row(uid) for uid in ('x', 'y', 'z')]
        got = [r.uid for r in accountorder.apply(rows, [])]
        self.assertEqual(got, ['x', 'y', 'z'])

    def test_does_not_mutate_input(self) -> None:
        rows = [_Row('a'), _Row('b')]
        accountorder.apply(rows, ['b', 'a'])
        self.assertEqual([r.uid for r in rows], ['a', 'b'])

    def test_duplicate_rank_uses_first_occurrence(self) -> None:
        self.assertEqual(accountorder.rank(['a', 'b', 'a']), {'a': 0, 'b': 1})


class EndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.patcher = _patch_db()
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_save_then_get(self) -> None:
        with mock.patch.object(accounts_router.security, 'audit') as audit:
            out = accounts_router.save_account_order(
                {'order': ['u2', 'u1']}, upstream_id=None, user={'username': 'admin'})
        self.assertEqual(out['order'], ['u2', 'u1'])
        audit.assert_called_once()
        self.assertEqual(
            accounts_router.get_account_order(upstream_id=None, user={'username': 'u'})['order'],
            ['u2', 'u1'],
        )

    def test_bad_payload_is_400(self) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            accounts_router.save_account_order(
                {'order': 'u1'}, upstream_id=None, user={'username': 'admin'})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_order_clears(self) -> None:
        with mock.patch.object(accounts_router.security, 'audit'):
            accounts_router.save_account_order(
                {'order': ['u1']}, upstream_id=None, user={'username': 'admin'})
            out = accounts_router.save_account_order(
                {}, upstream_id=None, user={'username': 'admin'})
        self.assertEqual(out['order'], [])


if __name__ == '__main__':
    unittest.main()

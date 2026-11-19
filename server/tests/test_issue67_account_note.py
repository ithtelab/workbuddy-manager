"""账号备注（issue #67）。

## 需求现场

用户用手机号邀请朋友注册，账号列表里全是 `昵称-1` 之类认不出是谁的名字；想删一个号
时不知道该删哪个。他要的是「给每个账号加一个自己记得住的备注」。

## 设计取舍（这里钉住的几条）

  · **存在本端、按 uid 关联**，不写进上游的账号文件——那是上游按自己 schema 读写的
    文件（它会原子回写），塞自定义字段会被覆盖或超出 schema；
  · 正因按 uid 存：**临时停用（改文件名）之后备注还在**——这条单独有用例，因为
    按文件名存是最自然的写法，也是最容易悄悄丢掉备注的写法；
  · **空串 = 删除**（界面留空保存即清除），不留一行空备注；
  · 超长**截断**而不是拒绝：备注是给人看的短文本，粘多了不该整句丢掉、更不该报错；
  · 只有管理员能改（`require_admin`），但**只读账号能看到备注**——它跟昵称一样是
    列表上的说明文字（在列表接口里一次带出）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException  # noqa: E402

from server import config, db  # noqa: E402
from server.routers import accounts as A  # noqa: E402

UID = 'note0001-0000-0000-0000-000000000001'


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')


def _write_auth(d: Path, uid: str, nickname: str, name: str | None = None) -> str:
    exp = int(time.time()) + 86400 * 30
    token = f"{_b64({'alg': 'none'})}.{_b64({'iat': int(time.time()), 'exp': exp, 'uid': uid})}.sig"
    fname = name or f'workbuddy-{uid}.json'
    (d / fname).write_text(json.dumps({
        'account': {'uid': uid, 'nickname': nickname, 'enterpriseId': 'e'},
        'auth': {'accessToken': token, 'refreshToken': 'r', 'expiresAt': exp,
                 'domain': 'copilot.tencent.com', 'realm': 'cn'},
    }, ensure_ascii=False), encoding='utf-8')
    return fname


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._db, self._auth = config.DB_PATH, config.AUTH_DIR
        config.DB_PATH = root / 'manager.db'
        config.AUTH_DIR = root / 'auths'
        config.AUTH_DIR.mkdir(parents=True, exist_ok=True)
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        # 必须先关连接再删目录：Windows 上打开着的 sqlite 文件删不掉
        # （PermissionError: 另一个程序正在使用此文件）。
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH, config.AUTH_DIR = self._db, self._auth
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


class NoteStorageTest(_DbCase):
    """存储层：按 uid 覆盖式写入、可删除。"""

    def test_set_and_read(self) -> None:
        db.set_account_note('u1', '张叔叔')
        self.assertEqual(db.account_notes(), {'u1': '张叔叔'})

    def test_overwrite_keeps_one_row(self) -> None:
        db.set_account_note('u1', '旧备注')
        db.set_account_note('u1', '新备注')
        self.assertEqual(db.account_notes(), {'u1': '新备注'})

    def test_delete(self) -> None:
        db.set_account_note('u1', 'x')
        db.delete_account_note('u1')
        self.assertEqual(db.account_notes(), {})

    def test_delete_missing_is_noop(self) -> None:
        db.delete_account_note('nope')          # 不抛

    def test_multiple_accounts_independent(self) -> None:
        db.set_account_note('u1', 'A')
        db.set_account_note('u2', 'B')
        db.delete_account_note('u1')
        self.assertEqual(db.account_notes(), {'u2': 'B'})


class NoteEndpointTest(_DbCase):
    """接口层：写入、清除、截断、错误处理。"""

    def setUp(self) -> None:
        super().setUp()
        self.fname = _write_auth(config.AUTH_DIR, UID, '小号')

    def _set(self, note: str, filename: str | None = None) -> dict:
        return asyncio.run(A.account_set_note(
            filename or self.fname, {'note': note}, user={'role': 'admin'}))

    def test_write_and_read_back(self) -> None:
        out = self._set('张叔叔（高中同学）')
        self.assertTrue(out['ok'])
        self.assertEqual(out['uid'], UID)
        self.assertEqual(db.account_notes(), {UID: '张叔叔（高中同学）'})

    def test_blank_clears(self) -> None:
        self._set('临时')
        for blank in ('', '   ', '\n'):
            with self.subTest(blank=repr(blank)):
                self._set(blank)
                self.assertEqual(db.account_notes(), {}, '留空保存应该删掉备注')

    def test_strips_whitespace(self) -> None:
        self._set('  备用号  ')
        self.assertEqual(db.account_notes(), {UID: '备用号'})

    def test_overlong_is_truncated_not_rejected(self) -> None:
        out = self._set('很长' * 100)
        self.assertEqual(len(out['note']), 100)
        self.assertEqual(db.account_notes()[UID], '很长' * 50)

    def test_missing_file_404(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._set('x', 'workbuddy-nosuch.json')
        self.assertEqual(ctx.exception.status_code, 404)

    def test_missing_note_key_is_treated_as_clear(self) -> None:
        """body 里没有 note 字段 = 清空（不是报错、也不是保持原值）。"""
        self._set('先写一个')
        out = asyncio.run(A.account_set_note(self.fname, {}, user={'role': 'admin'}))
        self.assertEqual(out['note'], '')
        self.assertEqual(db.account_notes(), {})

    def test_survives_rename_disable(self) -> None:
        """**临时停用（改文件名）之后备注还在** —— 按 uid 存的意义就在这里。

        按文件名存是最自然的写法，也正是在这一步悄悄丢掉备注的写法：停用后文件
        变成 `xxx.json.disabled`，按文件名查就查不到了。
        """
        self._set('张叔叔')
        from server.services import wb2api
        wb2api.set_account_disabled(self.fname, True)      # 改名为 .disabled
        disabled_name = self.fname + '.disabled'
        # 用停用后的文件名接口也能改到**同一个账号**的备注
        out = self._set('张叔叔（已停用）', disabled_name)
        self.assertEqual(out['uid'], UID)
        self.assertEqual(db.account_notes(), {UID: '张叔叔（已停用）'})


class NoteRouteContractTest(unittest.TestCase):
    """路由自身的契约：**只有管理员能改**，且 body 必须走 requestBody。

    为什么单测不够：上面的用例直接调端点函数，绕过了 FastAPI 的依赖注入与参数绑定
    —— 把 `require_admin` 漏掉、或者把 `Body(...)` 写成普通参数，那些用例**照样绿**。
    审查时正是这么查出来的：新加的 `PUT` 不在既有的「写接口 query 参数」闸门覆盖
    范围内（那个闸门当时只查 post），所以这里单独钉住。
    """

    def test_requires_admin(self) -> None:
        from server.routers import accounts as A
        route = next((r for r in A.router.routes if getattr(r, 'path', '') == '/api/accounts/{filename}/note'), None)
        self.assertIsNotNone(route, '没找到备注端点')
        names = set()
        stack = list(route.dependant.dependencies)
        while stack:
            d = stack.pop()
            names.add(getattr(d.call, '__name__', str(d.call)))
            stack.extend(d.dependencies)
        self.assertIn('require_admin', names,
                      '备注端点没有要求管理员 —— 任何登录用户都能改别人的备注')
        self.assertIn('current_user', names, '没要求登录')

    def test_body_is_declared_as_request_body(self) -> None:
        """`note` 必须从 JSON body 取：写成普通参数会被当成 query，静默失效。"""
        import os
        import tempfile
        os.environ.setdefault('WB_DATA_DIR', tempfile.mkdtemp())
        from server.main import app
        op = app.openapi()['paths']['/api/accounts/{filename}/note']['put']
        self.assertIn('requestBody', op, 'PUT 没声明 requestBody —— 前端发的 note 会读不到')


class NoteNormalisationTest(_DbCase):
    """备注文本的归一化：换行/连续空白收起，避免在单行展示里看起来像坏数据。"""

    def test_whitespace_collapsed(self) -> None:
        fname = _write_auth(config.AUTH_DIR, UID, '小号')
        raw = '张叔叔' + chr(10) + chr(10) + '高中同学   备用'
        out = asyncio.run(A.account_set_note(fname, {'note': raw}, user={'role': 'admin'}))
        self.assertEqual(out['note'], '张叔叔 高中同学 备用',
                         '换行与连续空格应被收起：备注是单行展示的')

    def test_newline_only_note_is_treated_as_empty(self) -> None:
        fname = _write_auth(config.AUTH_DIR, UID, '小号')
        asyncio.run(A.account_set_note(fname, {'note': '写过的'}, user={'role': 'admin'}))
        blank = chr(10) + '  ' + chr(10)
        out = asyncio.run(A.account_set_note(fname, {'note': blank}, user={'role': 'admin'}))
        self.assertEqual(out['note'], '')
        self.assertEqual(db.account_notes(), {}, '只有空白的备注应该等于清除')
        self.assertEqual(out['note'], '')
        self.assertEqual(db.account_notes(), {}, '只有空白的备注应该等于清除')


class NoteInListTest(_DbCase):
    """备注随账号列表一次带出（只读账号也能看到）。"""

    def test_note_included_and_defaults_to_empty(self) -> None:
        _write_auth(config.AUTH_DIR, UID, '小号')
        other = 'note0002-0000-0000-0000-000000000002'
        _write_auth(config.AUTH_DIR, other, '另一个号')
        db.set_account_note(UID, '张叔叔')

        async def _fake_status(*, base_url=None, api_key=None) -> dict:
            return {'connected': False}

        with mock.patch.object(A.wb2api, 'get_status', _fake_status):
            out = asyncio.run(A.list_accounts(user={'role': 'viewer'}))

        notes = {a['uid']: a['note'] for a in out['accounts']}
        self.assertEqual(notes[UID], '张叔叔')
        self.assertEqual(notes[other], '', '没有备注的账号给空串，而不是缺字段')


if __name__ == '__main__':
    unittest.main()

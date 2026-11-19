"""API 密钥的版本隔离（国内版 / 国际版分开）。

需求原话：「我生成国内 key 就只能获取国内的模型和调用国内的，反之国际就是
国际的，这俩完全区分开」。

此前密钥只有一份**模型白名单**，需要管理员手工往里填 `global:` 前缀的模型名
才能起到隔离作用——没填就等于两版都能调；新建密钥时也不会提示，实际部署里
几乎总是「隔离没生效」。现在密钥带自己的**版本归属**（realm），由网关强制：

  * 限定国际版的密钥，只能调 `global:` 前缀的模型；
  * 限定国内版的密钥，不能调 `global:` 前缀的模型；
  * 未限定（存量密钥）保持原行为——不能因为升级就把线上正在用的密钥限死。

判定依据必须与实际路由**同源**：上游按模型名的 `global:` 前缀选账号池，所以
「这次走哪个版本」由 model 决定，与「界面当前切到哪版」无关——后者只是浏览
状态，拿它做鉴权会和真实流量对不上。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc  # noqa: E402
from server.routers import gateway  # noqa: E402


class KeyRealmTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _row(self, **kw) -> dict:
        """建一把密钥并读回（走 resolve，与网关同一路径）。"""
        created = keysvc.create_key('t', **kw)
        return keysvc.resolve(created['key'])


class RealmScopingTest(KeyRealmTestBase):
    """调用侧：密钥版本与模型版本必须一致。"""

    def test_global_key_rejects_cn_model(self) -> None:
        row = self._row(realm='global')
        reason = keysvc.validate(row, '1.2.3.4', 'glm-5.2')
        self.assertIsNotNone(reason, '国际版密钥竟然能调国内版模型')
        self.assertIn('国际版', reason)

    def test_global_key_accepts_global_model(self) -> None:
        row = self._row(realm='global')
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'global:gpt-5.6-sol'))

    def test_cn_key_rejects_global_model(self) -> None:
        row = self._row(realm='cn')
        reason = keysvc.validate(row, '1.2.3.4', 'global:gpt-5.6-sol')
        self.assertIsNotNone(reason, '国内版密钥竟然能调国际版模型')
        self.assertIn('国内版', reason)

    def test_cn_key_accepts_bare_and_cn_prefixed(self) -> None:
        """国内版模型有两种写法：裸名（存量客户端）与 cn: 前缀。"""
        row = self._row(realm='cn')
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'glm-5.2'))
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'cn:glm-5.2'))

    def test_missing_model_rejected_for_scoped_key(self) -> None:
        """不带 model 会走上游默认模型（国内版）——必须拒绝。

        不拒的话，限定国际版的密钥只要不发 model 就能打到国内池，
        隔离形同虚设（与模型白名单当初被绕过的形态完全一样）。
        """
        row = self._row(realm='global')
        for bad in (None, '', '   '):
            self.assertIsNotNone(
                keysvc.validate(row, '1.2.3.4', bad),
                f'不带 model（{bad!r}）竟然绕过了版本隔离',
            )

    def test_unscoped_key_allows_both(self) -> None:
        """存量密钥（未限定版本）保持原行为，不被升级悄悄限死。"""
        row = self._row()
        self.assertEqual(row['realm'], '')
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'glm-5.2'))
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'global:gpt-5.6-sol'))

    def test_case_insensitive_prefix(self) -> None:
        """前缀判定与 db.realm_of_model 同口径（大小写不敏感）。"""
        row = self._row(realm='global')
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'GLOBAL:gpt-5.6-sol'))
        row_cn = self._row(realm='cn')
        self.assertIsNotNone(keysvc.validate(row_cn, '1.2.3.4', 'Global:gpt-5.6-sol'))

    def test_bogus_realm_value_normalized_to_unscoped(self) -> None:
        """非法版本值归一化成「不限制」，而不是拒绝——旧前端不带该字段也不报错。"""
        row = self._row(realm='bogus')
        self.assertEqual(row['realm'], '')
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'glm-5.2'))

    def test_model_allowlist_still_applies_within_realm(self) -> None:
        """版本归属与模型白名单是**两道**检查，都要过。"""
        row = self._row(realm='global', models=['global:gpt-5.6-sol'])
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'global:gpt-5.6-sol'))
        # 同为国际版但不在白名单内 → 白名单拦
        self.assertIsNotNone(keysvc.validate(row, '1.2.3.4', 'global:gpt-5.4'))


class RealmScopingUpdateTest(KeyRealmTestBase):
    """修改密钥的版本归属。"""

    def test_update_sets_realm(self) -> None:
        created = keysvc.create_key('t')
        row = keysvc.resolve(created['key'])
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'global:x'))
        keysvc.update_key(created['id'], {'realm': 'cn'})
        row = keysvc.resolve(created['key'])
        self.assertEqual(row['realm'], 'cn')
        self.assertIsNotNone(keysvc.validate(row, '1.2.3.4', 'global:x'))

    def test_update_can_clear_realm_back_to_unscoped(self) -> None:
        """显式清空 = 改回「不限制」，不能被真值判断吃掉。"""
        created = keysvc.create_key('t', realm='global')
        keysvc.update_key(created['id'], {'realm': ''})
        row = keysvc.resolve(created['key'])
        self.assertEqual(row['realm'], '')
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'glm-5.2'))


class ModelListScopingTest(KeyRealmTestBase):
    """/v1/models 按密钥版本裁剪：限定版本的密钥看不到另一版本。"""

    PAYLOAD = {
        'object': 'list',
        'data': [
            {'id': 'cn:glm-5.2'},
            {'id': 'glm-5.1'},
            {'id': 'global:gpt-5.6-sol'},
            {'id': 'global:deepseek-v4.1-flash'},
        ],
    }

    def _ids(self, key: dict | None) -> list[str]:
        out = gateway._scope_models(self.PAYLOAD, key)
        return [m['id'] for m in out['data']]

    def test_global_key_sees_only_global(self) -> None:
        ids = self._ids({'realm': 'global'})
        self.assertEqual(ids, ['global:gpt-5.6-sol', 'global:deepseek-v4.1-flash'])

    def test_cn_key_sees_only_cn(self) -> None:
        ids = self._ids({'realm': 'cn'})
        self.assertEqual(ids, ['cn:glm-5.2', 'glm-5.1'])

    def test_unscoped_key_sees_everything(self) -> None:
        self.assertEqual(len(self._ids({'realm': ''})), 4)

    def test_scope_models_survives_unexpected_payload(self) -> None:
        """结构不是预期的 {data:[...]} 时原样透传，不改动上游响应。"""
        for bad in ({'data': 'x'}, {'data': None}, {}, 'raw', None, [1, 2]):
            self.assertEqual(gateway._scope_models(bad, {'realm': 'global'}), bad)

    def test_model_list_request_not_blocked_by_realm(self) -> None:
        """模型发现请求（不带 model）不该被版本隔离拦掉。

        否则限定版本的密钥连「我有哪些模型」都问不到——列表已经按版本裁剪过了，
        真正的隔离由调用时的模型名把关。
        """
        row = self._row(realm='global')
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', None, is_model_list=True))
        # 但普通对话请求仍然要拦
        self.assertIsNotNone(keysvc.validate(row, '1.2.3.4', None, is_model_list=False))


class CreateKeyUnlimitedRealmUiTest(unittest.TestCase):
    """新建密钥时要能**显式**选「不限制」（issue #191）。

    后端一直支持 `realm=''`（= 两版都能调，存量密钥的形态），缺的是入口：新建弹窗
    里那个选择器被刻意藏了起来（跟随当前页面版本），于是想建一把两版都能调的密钥
    只能「先建出来再编辑」。修法是在新建态补一个显式开关，**默认关** —— 既满足
    #191，也保留「不给国内版/国际版二选一」这条（选错是静默的，只有调用时才暴露）。
    """

    def _src(self) -> str:
        return (Path(__file__).resolve().parents[2]
                / 'web' / 'app' / '(main)' / 'keys' / 'page.tsx').read_text(encoding='utf-8')

    def test_create_dialog_offers_explicit_unlimited(self) -> None:
        src = self._src()
        self.assertIn('key-realm-unlimited', src, '新建弹窗里没有「不限制」开关')

    def test_payload_keeps_explicit_unlimited(self) -> None:
        """用户在新建议窗里勾了「不限制」就必须照传 ''。

        原来写的是 `editing ? form.realm : realm`（新建一律用页面版本）—— 那样
        勾了开关也会被覆盖回当前版本，用户看到的是「我明明选了不限制，建出来还是有
        版本的密钥」，而错误是静默的（直到调用才报「仅限某版本」）。

        断言写成整条表达式而非整行：格式化（prettier 折行）不该让这条守卫失效。
        """
        src = self._src()
        self.assertIn("editing || form.realm === ''", src,
                      '新建时用户显式选择的「不限制」被页面版本覆盖了')

    def test_default_still_follows_page_realm(self) -> None:
        """默认（没开开关）仍跟随当前页面版本 —— 这条不能被 #191 顺手改掉。"""
        src = self._src()
        start = src.index('key-realm-unlimited')
        seg = src[start:start + 900]
        self.assertIn('onCheckedChange', seg, '「不限制」开关没有回写 form.realm')
        self.assertIn("on ? '' : realm", seg, '关掉开关时没有回到当前页面版本')
        self.assertIn("t('keys.realmFollowsPage')", src)


if __name__ == '__main__':
    unittest.main()

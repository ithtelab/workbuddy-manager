"""`/v1/models` 的裁剪必须与调用侧的放行**同一口径**（issue #46）。

## 为什么需要这一条

`/v1/models` 要回答的是「**这把密钥能用什么模型**」——客户端普遍拿它当模型
选择器。此前它只裁了**版本**那一维，模型白名单那一维没裁（`keysvc.validate`
里两处检查都带 `not is_model_list`，但只有版本那维在响应侧补了裁剪）。
于是同一个接口两维口径不一致：一维回答「你能用什么」，另一维回答的却是
「这个版本有什么」。用户看到的是「能选、一选就失败」——挑白名单外的模型发
对话请求，400「模型 xxx 不在密钥白名单内」。

## 这里钉住的

**不变量**：列表里出现的每一个 id，用同一把密钥去调用都必须被放行。
这是「列表 = 你能用的东西」的形式化表述，也是两处口径一旦漂移就会红的判据
（报告者建议的第 3 点）。

覆盖的边界：
  · 白名单为空 → 该版本全部（与调用侧「不限制」一致，行为不变）；
  · 白名单带 `cn:` 前缀（存量密钥形态）；
  · 白名单跨版本混写；
  · **别名**——白名单可以写下游熟悉的别名（鉴权判请求里的名字，模型映射发生在
    鉴权之后），别名不在上游清单里；只按字面裁会把这类密钥的列表**裁成空**，
    比不裁更糟。故别名要按别名本身补进列表。
  · 拼错的名字 → 列表为空（这是**正确**的：那个名字确实调不通）。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import db, keysvc  # noqa: E402
from server.routers import gateway as G  # noqa: E402
from server.routers import keys as keys_router  # noqa: E402
from server.services import modelcatalog  # noqa: E402

# 上游 /v1/models 的形状：国内版裸名，国际版带 `global:` 前缀
PAYLOAD = {'object': 'list', 'data': [
    {'id': 'glm-5.2'},
    {'id': 'deepseek-v4.1-flash'},
    {'id': 'global:deepseek-v4.1-flash'},
    {'id': 'global:gpt-5.6-sol'},
]}


ALIAS = 'claude-fable-5'


def _key(**kw) -> dict:
    """一把**能通过其它所有校验**的密钥，只让白名单/版本参与判定。"""
    base = {'enabled': True, 'expires_at': 0, 'quota': 0, 'quota_credit': 0,
            'used_tokens': 0, 'used_credit': 0, 'ip_allowlist': [], 'max_ips': 0,
            'id': 1, 'realm': '', 'models': []}
    base.update(kw)
    return base


def _listed(key: dict, model_map: dict | None = None) -> list[str]:
    with mock.patch.object(db, 'get_setting',
                           lambda k, d=None: (model_map or {}) if k == 'model_map' else d):
        out = G._scope_models(PAYLOAD, key)
    return [m['id'] for m in out['data']]


class ListMatchesCallAdmissionTest(unittest.TestCase):
    """核心不变量：**列表里的每个 id，调用都必须被放行**。"""

    def _assert_invariant(self, key: dict, model_map: dict | None = None) -> list[str]:
        listed = _listed(key, model_map)
        mp = model_map or {}
        for mid in listed:
            # 调用路径把**映射后**的名字交给鉴权判版本（issue #47），这里必须同口径
            # 地传——否则等于用一条真实调用不使用的规则去判列表，测出来的差异是假的。
            reason = keysvc.validate(key, '1.2.3.4', mid, mapped_model=mp.get(mid, mid))
            self.assertIsNone(
                reason,
                f'列表给出的 {mid!r} 实际调用被拒（{reason}）——'
                '这就是 issue #46 的「能选但一选就失败」',
            )
        return listed

    def test_reporter_case(self) -> None:
        """报告者的场景：限定国际版 + 白名单只留一个。"""
        listed = self._assert_invariant(
            _key(realm='global', models=['global:deepseek-v4.1-flash']))
        self.assertEqual(listed, ['global:deepseek-v4.1-flash'],
                         '白名单外的模型仍被列出（用户选中就 400）')

    def test_cross_realm_whitelist(self) -> None:
        listed = self._assert_invariant(
            _key(models=['glm-5.2', 'global:gpt-5.6-sol']))
        self.assertEqual(listed, ['glm-5.2', 'global:gpt-5.6-sol'])

    def test_empty_whitelist_keeps_whole_realm(self) -> None:
        """白名单为空 = 不限制 → 该版本全部（与调用侧语义一致，行为不变）。"""
        listed = self._assert_invariant(_key(realm='global'))
        self.assertEqual(listed, ['global:deepseek-v4.1-flash', 'global:gpt-5.6-sol'])

    def test_unlimited_key_sees_both_realms(self) -> None:
        """版本也不限（realm=''）= 两版都能调 → 四个都在，这是既有行为。

        别把它误当成「没裁干净」：未限定版本的密钥调用 `glm-5.2` 与
        `global:gpt-5.6-sol` 都会被放行（`validate` 只在 key 限定了版本时才比
        版本），所以列表给全部才是对的。
        """
        listed = self._assert_invariant(_key())
        self.assertEqual(listed, ['glm-5.2', 'deepseek-v4.1-flash',
                                  'global:deepseek-v4.1-flash', 'global:gpt-5.6-sol'])

    def test_legacy_cn_prefix_in_whitelist(self) -> None:
        """存量密钥的白名单可能带 `cn:` 前缀，两种写法都要认。"""
        listed = self._assert_invariant(_key(models=['cn:glm-5.2']))
        self.assertEqual(listed, ['glm-5.2'])

    def test_alias_whitelist_not_cut_to_empty(self) -> None:
        """白名单填**别名**时不能裁成空列表——别名确实能调用。

        别名不在上游清单里（它是设置页「模型映射」给下游用的名字）。只按清单
        字面裁会让这类密钥的列表变成空，客户端显示「没有可用模型」，而它其实是
        能正常调用的。补进列表时 `id` 必须是**别名**（发真名会被白名单拒掉）。
        """
        listed = self._assert_invariant(
            _key(models=['gpt-4o-mini']), model_map={'gpt-4o-mini': 'glm-5.2'})
        self.assertEqual(listed, ['gpt-4o-mini'],
                         '别名没被补进列表（或补成了真名）')

    def test_typo_whitelist_yields_empty(self) -> None:
        """拼错的名字 → 空列表。这是**正确**的：那个名字确实调不通。

        （报告者的第 2 点：空列表本身不好看，但比「列出一堆调不通的模型」
        诚实；根本的解法是在密钥编辑处做命中校验，见 KeysWhitelistHintTest。）
        """
        self.assertEqual(self._assert_invariant(_key(models=['glm-5.9'])), [])

    def test_alias_without_mapping_ignored(self) -> None:
        """没配映射的「别名」就是拼错的名字，不该凭空出现。"""
        self.assertEqual(self._assert_invariant(_key(models=['nope']), model_map={}), [])

    def test_cross_realm_alias_stays_consistent(self) -> None:
        """别名**跨版本**时列表与调用仍要一致（issue #46 + #47 的交叉点）。

        限定国际版的密钥 + 白名单只写别名，别名指向国际版模型：列表给出别名
        （客户端要发的就是它），调用也必须放行——两个 issue 的修法在这里会师：
        列表按**目标**判版本、鉴权按**映射后**的名字判版本，白名单两边都判请求名。
        任一处判据挪错，这条就会红。
        """
        listed = self._assert_invariant(
            _key(realm='global', models=[ALIAS]),
            model_map={ALIAS: 'global:deepseek-v4.1-flash'})
        self.assertEqual(listed, [ALIAS])

    def test_cross_realm_alias_hidden_from_other_realm(self) -> None:
        """指向国际版的别名，不该出现在限定国内版的密钥的列表里。"""
        listed = self._assert_invariant(
            _key(realm='cn', models=[ALIAS]),
            model_map={ALIAS: 'global:deepseek-v4.1-flash'})
        self.assertEqual(listed, [], '国内版密钥的列表里出现了国际版别名')


class ShapeSafetyTest(unittest.TestCase):
    """认不出的结构一律原样透传——不因为我们认不出就去改动上游响应。"""

    def test_non_dict_payload_untouched(self) -> None:
        for payload in ([], 'x', None, 5):
            self.assertEqual(G._scope_models(payload, _key(models=['glm-5.2'])), payload)

    def test_missing_data_untouched(self) -> None:
        payload = {'object': 'list'}
        self.assertEqual(G._scope_models(payload, _key(models=['glm-5.2'])), payload)

    def test_non_dict_items_dropped_not_crashed(self) -> None:
        out = G._scope_models({'data': [None, 'x', {'id': 'glm-5.2'}]},
                              _key(models=['glm-5.2']))
        self.assertEqual([m['id'] for m in out['data']], ['glm-5.2'])

    def test_extra_keys_preserved(self) -> None:
        """裁剪只动 data，其余字段（object 等）原样保留。"""
        out = G._scope_models({'object': 'list', 'data': [{'id': 'glm-5.2'}]},
                              _key(models=['glm-5.2']))
        self.assertEqual(out['object'], 'list')


class SharedCriterionTest(unittest.TestCase):
    """两处必须**共用同一份判据**（报告者建议的第 1 点）。

    各写一遍迟早漂移，而漂移的表现正是 issue #46 报的现象。这里钉住
    `validate` 确实走 `model_allowed`，而不是自己又写了一份比对。
    """

    def test_validate_delegates_to_shared_predicate(self) -> None:
        src = (Path(__file__).resolve().parents[1] / 'keysvc.py').read_text(encoding='utf-8')
        seg = src[src.index('def validate('):]
        seg = seg[:seg.index('\ndef ', seg.index('def validate(') + 10)]
        self.assertIn('model_allowed(key, model)', seg,
                      'validate 没走共用判据 —— 两份规则会漂移')

    def test_presence_guard_still_first(self) -> None:
        """「未指定 model」与「不在白名单」仍是**两条**不同的报错。

        合成一条会让「请求根本没带 model」被说成「模型不在白名单内」，
        用户照着一个不存在的名字去查。
        """
        key = _key(models=['glm-5.2'])
        missing = keysvc.validate(key, '1.2.3.4', None)
        self.assertIn('未指定 model', str(missing))
        not_allowed = keysvc.validate(key, '1.2.3.4', 'glm-5.9')
        self.assertIn('不在密钥白名单内', str(not_allowed))

    def test_predicate_semantics(self) -> None:
        """判据本身：空白名单放行一切；`cn:` 归一化；`global:` 不归一化。"""
        self.assertTrue(keysvc.model_allowed(_key(), 'anything'))
        self.assertTrue(keysvc.model_allowed(_key(models=['cn:glm-5.2']), 'glm-5.2'))
        self.assertTrue(keysvc.model_allowed(_key(models=['glm-5.2']), 'cn:glm-5.2'))
        # global: 决定路由，两个版本的同名模型不是一回事
        self.assertFalse(keysvc.model_allowed(_key(models=['global:glm-5.2']), 'glm-5.2'))
        self.assertFalse(keysvc.model_allowed(_key(models=['glm-5.2']), 'global:glm-5.2'))
        # 畸形输入不放行
        self.assertFalse(keysvc.model_allowed(_key(models=['glm-5.2']), None))
        self.assertFalse(keysvc.model_allowed(_key(models=['glm-5.2']), '   '))


class CatalogShrinkTest(unittest.TestCase):
    """issue #239：上游清单里的已下架模型要按模型中心的实时目录裁掉。

    上游 `/v1/models` 会列出一批腾讯早已收缩掉的旧模型（cn 域 52 个，
    模型中心只有 20 个），下游拿它当选择器，选中后才 503。模型中心直连
    腾讯的目录是当前可用的权威清单；取不到时原样透传，绝不能把列表清空。
    """

    def _scope(self, payload, key, mapping=None):
        with mock.patch.object(db, 'get_setting',
                               lambda k, d=None: (mapping or {}) if k == 'model_map' else d):
            return G._scope_models(payload, key)

    def test_drops_ids_missing_from_tencent_catalog(self) -> None:
        payload = {'object': 'list', 'data': [
            {'id': 'glm-5.2'}, {'id': 'deepseek-v4.1-flash'},
            {'id': 'global:gpt-5.6-sol'},
        ]}
        key = _key(realm='cn')
        scoped = self._scope(payload, key)
        out = G._filter_catalog_models(scoped, key, 'cn', {'glm-5.2'})
        self.assertEqual([m['id'] for m in out['data']], ['glm-5.2'],
                         'cn 清单里仍留着模型中心没有的旧模型')

    def test_global_prefix_is_compared_bare(self) -> None:
        """模型中心给裸名，上游国际版给 `global:` 前缀，前缀不能导致误删。"""
        payload = {'data': [{'id': 'global:gpt-5.6-sol'}]}
        key = _key(realm='global')
        out = G._filter_catalog_models(payload, key, 'global', {'gpt-5.6-sol'})
        self.assertEqual([m['id'] for m in out['data']], ['global:gpt-5.6-sol'])

    def test_alias_entry_survives(self) -> None:
        """白名单里的别名指向仍在目录里的模型时，别名条目不能被裁掉。"""
        mapping = {'gpt-4o-mini': 'glm-5.2'}
        key = _key(models=['gpt-4o-mini'])
        scoped = self._scope({'data': [{'id': 'glm-5.2'}]}, key, mapping)
        self.assertEqual([m['id'] for m in scoped['data']], ['gpt-4o-mini'])
        with mock.patch.object(db, 'get_setting',
                               lambda k, d=None: mapping if k == 'model_map' else d):
            out = G._filter_catalog_models(scoped, key, 'cn', {'glm-5.2'})
        self.assertEqual([m['id'] for m in out['data']], ['gpt-4o-mini'],
                         '别名指向的模型仍可用，别名不该被删')

    def test_alias_to_dead_model_is_dropped(self) -> None:
        """别名指向的模型已下架 → 别名同样不可用，应删。"""
        mapping = {'dead-alias': 'old-model'}
        key = _key(models=['dead-alias'])
        scoped = self._scope({'data': [{'id': 'old-model'}, {'id': 'glm-5.2'}]},
                             key, mapping)
        with mock.patch.object(db, 'get_setting',
                               lambda k, d=None: mapping if k == 'model_map' else d):
            out = G._filter_catalog_models(scoped, key, 'cn', {'glm-5.2'})
        self.assertEqual([m['id'] for m in out['data']], [])

    def test_upstream_fallback_source_skips_filter(self) -> None:
        """目录来源是上游回退（同一份旧表）时不做裁剪。"""
        payload = {'data': [{'id': 'old-model'}]}
        key = _key(realm='cn')
        cat = {'source': 'upstream', 'models': [{'id': 'glm-5.2'}]}
        with mock.patch.object(G.modelcatalog, 'catalog',
                               mock.AsyncMock(return_value=cat)):
            out = asyncio.run(G._drop_models_outside_catalog(payload, key))
        self.assertEqual([m['id'] for m in out['data']], ['old-model'])

    def test_catalog_failure_keeps_list(self) -> None:
        """目录取不到/超时 → 原样返回，不能把列表清空。"""
        payload = {'data': [{'id': 'old-model'}]}
        key = _key(realm='cn')

        async def _boom(realm):
            raise TimeoutError('catalog timeout')

        with mock.patch.object(G.modelcatalog, 'catalog', _boom):
            out = asyncio.run(G._drop_models_outside_catalog(payload, key))
        self.assertEqual([m['id'] for m in out['data']], ['old-model'])


if __name__ == '__main__':
    unittest.main()


class WhitelistHintTest(unittest.TestCase):
    """编辑密钥时的「这些名字找不到模型」提示（issue #46 的可选做法 2）。

    白名单是自由文本框，填错不会报错，只在下游表现为「模型列表是空的」——
    而空列表看不出原因（少个连字符？填成了显示名？）。所以在编辑处当场点出来。

    两条硬要求：
      · 判据与调用侧**同一份**（`_bare_model`），别名也算合法条目；
      · **拿不到清单时不许猜**——那时一切都会被判成"找不到"，是假警报，
        比不提示更糟（用户会去改一个本来正确的名字）。
    """

    def _cached(self, ids: dict[str, set[str] | None]):
        return mock.patch.object(modelcatalog, 'cached_ids', lambda r: ids.get(r))

    def test_unknown_names_reported(self) -> None:
        with self._cached({'cn': {'glm-5.2', 'deepseek-v4.1-flash'}, 'global': {'global:gpt-5.6-sol'}}):
            out = keys_router.check_models(
                keys_router.WhitelistCheckIn(models=['glm-5.2', 'glm-5.9'], realm=''), user={})
        self.assertTrue(out['checked'])
        self.assertEqual(out['unknown'], ['glm-5.9'])

    def test_alias_is_known(self) -> None:
        """别名是合法条目（鉴权判请求名、映射在其后），不该被判成"找不到"。"""
        with self._cached({'cn': {'glm-5.2'}}), \
             mock.patch.object(db, 'get_setting', lambda k, d=None: {'gpt-4o-mini': 'glm-5.2'}):
            out = keys_router.check_models(
                keys_router.WhitelistCheckIn(models=['gpt-4o-mini'], realm=''), user={})
        self.assertEqual(out['unknown'], [])

    def test_cn_prefix_normalised(self) -> None:
        """存量密钥的 `cn:glm-5.2` 写法要认（与调用侧同一判据）。"""
        with self._cached({'cn': {'glm-5.2'}}):
            out = keys_router.check_models(
                keys_router.WhitelistCheckIn(models=['cn:glm-5.2'], realm='cn'), user={})
        self.assertEqual(out['unknown'], [])

    def test_no_catalog_means_no_guess(self) -> None:
        """拿不到清单 → checked=false 且**不列任何名字**，绝不给假警报。"""
        with self._cached({'cn': None, 'global': None}):
            out = keys_router.check_models(
                keys_router.WhitelistCheckIn(models=['glm-5.2', 'whatever'], realm=''), user={})
        self.assertFalse(out['checked'])
        self.assertEqual(out['unknown'], [], '拿不到清单却列出了"找不到" → 假警报')
        self.assertTrue(out.get('reason'), '没给出「为什么没校验」')

    def test_one_realm_missing_does_not_flag_its_entries(self) -> None:
        """**只有一个版本可用时，另一版的条目原样放过**（关键：不给假警报）。

        两个版本的清单是分开取的。拿国内版清单去判 `global:xxx` 必然判成
        「找不到」——而它只是**那个版本还没缓存**。这种假警报会让用户去改一个
        本来正确的名字，比不提示更糟。
        """
        with self._cached({'cn': {'glm-5.2'}, 'global': None}):
            out = keys_router.check_models(
                keys_router.WhitelistCheckIn(
                    models=['glm-5.2', 'zzz', 'global:gpt-5.6-sol'], realm=''), user={})
        self.assertTrue(out['checked'])
        self.assertEqual(out['unknown'], ['zzz'],
                         'global 清单不可用时把它的条目判成「找不到」 → 假警报')
        self.assertNotIn('global:gpt-5.6-sol', out['unknown'])

    def test_empty_whitelist_short_circuits(self) -> None:
        out = keys_router.check_models(
            keys_router.WhitelistCheckIn(models=[], realm='cn'), user={})
        self.assertTrue(out['checked'])
        self.assertEqual(out['unknown'], [])

    def test_predicate_judges_each_name_against_its_own_realm(self) -> None:
        """每个名字只跟自己版本的清单比。"""
        both = {'cn': {'glm-5.2'}, 'global': {'global:gpt-5.6-sol'}}
        self.assertEqual(keysvc.unknown_whitelist_entries(['glm-5.2'], both, []), [])
        self.assertEqual(
            keysvc.unknown_whitelist_entries(['global:gpt-5.6-sol'], both, []), [])
        self.assertEqual(
            keysvc.unknown_whitelist_entries(['global:glm-5.2'], both, []),
            ['global:glm-5.2'],
            '`global:glm-5.2` 是国际版模型，不该拿国内版的 `glm-5.2` 判它认得')

    def test_predicate_skips_names_whose_realm_is_unavailable(self) -> None:
        self.assertEqual(
            keysvc.unknown_whitelist_entries(['global:x'], {'cn': {'glm-5.2'},
                                                            'global': None}, []),
            [], 'global 清单不可用却判了它的条目')

    def test_catalog_bare_ids_vs_prefixed_whitelist(self) -> None:
        """目录里的 id 是**裸名**，白名单常带 `global:` 前缀——两边都要去前缀再比。

        这是实测踩到的假警报：模型目录缓存里的 id 已经被 `_strip_realm_prefix`
        去了前缀（目录本身按版本分开取，"国际版目录"里的 `gpt-5.6-sol` 就是国际版
        那个），而白名单里写的是上游路由要的 `global:gpt-5.6-sol`。只去一边，
        正确的名字会被报成「找不到」，用户就去改一个本来对的名字。
        """
        self.assertEqual(
            keysvc.unknown_whitelist_entries(
                ['global:gpt-5.6-sol'], {'cn': {'glm-5.2'}, 'global': {'gpt-5.6-sol'}}, []),
            [], '带前缀的白名单 vs 裸名的目录 → 假警报（正确名字被判成找不到）')

    def test_still_flags_genuinely_wrong_global_name(self) -> None:
        """修正前缀比对后，真拼错的国际版名字仍要被点出（别把漏报当修好）。"""
        self.assertEqual(
            keysvc.unknown_whitelist_entries(
                ['global:nope'], {'cn': {'glm-5.2'}, 'global': {'gpt-5.6-sol'}}, []),
            ['global:nope'])

    def test_prefix_forms_equivalent_for_cn(self) -> None:
        """`cn:glm-5.2` 与 `glm-5.2` 等价（存量密钥两种写法）。"""
        both = {'cn': {'glm-5.2'}, 'global': set()}
        self.assertEqual(keysvc.unknown_whitelist_entries(['cn:glm-5.2'], both, []), [])
        self.assertEqual(keysvc.unknown_whitelist_entries(['glm-5.2'], both, []), [])

    def test_predicate_accepts_aliases(self) -> None:
        self.assertEqual(
            keysvc.unknown_whitelist_entries(['gpt-4o-mini'],
                                             {'cn': {'glm-5.2'}, 'global': set()},
                                             {'gpt-4o-mini'}), [])


class CachedIdsTest(unittest.TestCase):
    """`cached_ids` 只读缓存、**不发网络**（保存密钥不该等上游往返）。"""

    def setUp(self) -> None:
        self._saved = dict(modelcatalog._cache)
        modelcatalog._cache.clear()

    def tearDown(self) -> None:
        modelcatalog._cache.clear()
        modelcatalog._cache.update(self._saved)

    def test_no_cache_returns_none(self) -> None:
        self.assertIsNone(modelcatalog.cached_ids('cn'))

    def test_cached_list_returned(self) -> None:
        modelcatalog._cache['cn'] = {'at': 0, 'ttl': 300,
                                     'payload': {'models': [{'id': 'glm-5.2'}]}}
        self.assertEqual(modelcatalog.cached_ids('cn'), {'glm-5.2'})

    def test_empty_catalog_returns_none(self) -> None:
        """空清单**也**算拿不到：用它校验会把所有名字判成"找不到"。"""
        modelcatalog._cache['cn'] = {'at': 0, 'ttl': 300, 'payload': {'models': []}}
        self.assertIsNone(modelcatalog.cached_ids('cn'))

    def test_malformed_entries_skipped(self) -> None:
        modelcatalog._cache['cn'] = {'at': 0, 'ttl': 300, 'payload': {
            'models': [None, 'x', {'noid': 1}, {'id': 'ok'}]}}
        self.assertEqual(modelcatalog.cached_ids('cn'), {'ok'})

    def test_does_not_fetch(self) -> None:
        """必须**不触发**取数：这里把 catalog 换成会抛异常的桩。"""
        with mock.patch.object(modelcatalog, 'catalog',
                               side_effect=AssertionError('不该取数')):
            self.assertIsNone(modelcatalog.cached_ids('global'))


class HintFrontendWiringTest(unittest.TestCase):
    """前端要真的显示这个提示，且「查不了」不能显示成「全部正确」。"""

    def _component(self) -> str:
        return (Path(__file__).resolve().parents[2]
                / 'web/components/common/keys/ModelWhitelistInput.tsx'
                ).read_text(encoding='utf-8')

    def _page(self) -> str:
        return (Path(__file__).resolve().parents[2]
                / 'web/app/(main)/keys/page.tsx').read_text(encoding='utf-8')

    def test_hint_rendered(self) -> None:
        src = self._page()
        self.assertIn('unknownModels', src)
        self.assertIn("t('keys.modelsUnknown'", src)

    def test_null_is_not_rendered_as_ok(self) -> None:
        """`null`（未查/查不了）与 `[]`（查过、都对）必须区分。

        混为一谈会把「暂时查不了」显示成「全部正确」——用户以为没问题。
        """
        src = self._page()
        self.assertIn('unknownModels !== null && unknownModels.length > 0', src,
                      'null 与空数组没区分开')
        self.assertIn('setUnknownModels(r.checked ? r.unknown : null)', src,
                      'checked=false 时没退回 null')

    def test_editing_invalidates_previous_result(self) -> None:
        """改了内容要作废上次结论，否则显示的是**过期**的「都对」。

        #141 之后白名单换成组件（`ModelWhitelistInput`：可勾选的清单 + 手输），
        但这条不变量没变：凡是把用户改动写回 `form.models` 的地方，都要顺手把上次
        校验结果清掉。
        """
        src = self._page()
        start = src.index('<ModelWhitelistInput')
        seg = src[start:src.index('onBlur', start)]
        self.assertIn('setUnknownModels(null)', seg, '改了白名单却留着上次的校验结果')
        self.assertIn('models: next', seg, '组件回传的值没写回 form.models')
        # 打字过程中也要作废：输入框里的草稿要失焦才提交，只清 onChange 的话
        # 「改到一半」这一段里提示还是上一版的值（浏览器验收实测踩到，见
        # dev/verify_whitelist_hint.py 的第 3 步）。
        self.assertIn('onEdit=', seg, '打字时不作废旧结论')
        comp = self._component()
        self.assertIn('onEdit?.()', comp, '组件没有把「正在编辑」告诉父组件')

    def test_all_locales_define_the_key(self) -> None:
        import json
        base = Path(__file__).resolve().parents[2] / 'web/lib/i18n/locales'
        for f in sorted(base.glob('*.json')):
            data = json.loads(f.read_text(encoding='utf-8'))
            val = (data.get('keys') or {}).get('modelsUnknown')
            self.assertTrue(val and '{names}' in str(val),
                            f'{f.name} 缺 keys.modelsUnknown 或没带 {{names}} 占位符')


if __name__ == '__main__':
    unittest.main()

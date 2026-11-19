"""balance_realm 的口径测试。

只测纯函数：不建库、不发请求，所以能跟着 `python -m unittest` 直接跑。
"""
import unittest

from server.services import balance_realm as br


def _status(*rows):
    return {'connected': True,
            'accounts': [{'uid': str(i), 'realm': realm, 'credits': credits}
                         for i, (realm, credits) in enumerate(rows)]}


class NormalizeTest(unittest.TestCase):
    def test_scope_normalized(self):
        self.assertEqual(br.normalize_scope(' CN '), 'cn')
        self.assertEqual(br.normalize_scope('Global'), 'global')
        self.assertEqual(br.normalize_scope('auto'), 'auto')
        for bad in ('', None, 'us', 'cnn', 123):
            self.assertEqual(br.normalize_scope(bad), 'auto')

    def test_realm_normalized(self):
        self.assertEqual(br.normalize_realm('global'), 'global')
        self.assertEqual(br.normalize_realm(' GLOBAL '), 'global')
        for bad in ('', None, 'us', '欧洲'):
            self.assertEqual(br.normalize_realm(bad), '')


class ResolveScopeTest(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(
            br.resolve_scope(key_realm='cn', has_limit=True, last_realm='global',
                             requested='global'), 'global')

    def test_limit_forces_merged(self):
        # 有额度上限时，realm 再怎么限定也不拆池
        self.assertEqual(
            br.resolve_scope(key_realm='cn', has_limit=True, last_realm='cn'),
            'merged')

    def test_key_realm_beats_last_call(self):
        self.assertEqual(
            br.resolve_scope(key_realm='cn', last_realm='global'), 'cn')

    def test_falls_back_to_last_call(self):
        self.assertEqual(br.resolve_scope(key_realm='', last_realm='global'),
                         'global')

    def test_empty_everything_is_merged(self):
        self.assertEqual(br.resolve_scope(), 'merged')
        self.assertEqual(br.resolve_scope(key_realm='', last_realm=None), 'merged')


class RealmUidsTest(unittest.TestCase):
    def test_uids_per_realm(self):
        st = _status(('cn', 1), ('global', 2), ('cn', 3))
        self.assertEqual(br.realm_uids(st, 'cn'), {'0', '2'})
        self.assertEqual(br.realm_uids(st, 'global'), {'1'})

    def test_missing_uid_skipped(self):
        st = {'accounts': [{'realm': 'cn', 'credits': 1},
                           {'realm': 'cn', 'uid': 'x', 'credits': 2}]}
        self.assertEqual(br.realm_uids(st, 'cn'), {'x'})

    def test_bad_input(self):
        self.assertEqual(br.realm_uids(None, 'cn'), set())
        self.assertEqual(br.realm_uids({'accounts': []}, 'cn'), set())
        self.assertEqual(br.realm_uids(_status(('cn', 1)), 'us'), set())


class RealmCreditsTest(unittest.TestCase):
    def test_sums_per_realm(self):
        st = _status(('cn', 100), ('cn', 50.5), ('global', 7))
        self.assertEqual(br.realm_credits(st, 'cn'), 150.5)
        self.assertEqual(br.realm_credits(st, 'global'), 7.0)

    def test_missing_realm_is_none(self):
        st = _status(('cn', 10))
        self.assertIsNone(br.realm_credits(st, 'global'))

    def test_bad_input_is_none(self):
        self.assertIsNone(br.realm_credits(None, 'cn'))
        self.assertIsNone(br.realm_credits({}, 'cn'))
        self.assertIsNone(br.realm_credits({'accounts': []}, 'cn'))
        self.assertIsNone(br.realm_credits(_status(('cn', 1)), ''))

    def test_dirty_credits_ignored(self):
        st = {'accounts': [{'realm': 'cn', 'credits': 'abc'},
                           {'realm': 'cn', 'credits': 5},
                           {'realm': 'cn'}]}
        self.assertEqual(br.realm_credits(st, 'cn'), 5.0)

    def test_breakdown(self):
        st = _status(('cn', 3), ('global', 4))
        self.assertEqual(br.realm_breakdown(st), {'cn': 3.0, 'global': 4.0})


if __name__ == '__main__':
    unittest.main()

"""keysvc 的 provider 感知版本门测试（C2 修复的单元侧）。

覆盖（架构文档 B05 验收标准）：
  * global 密钥 + qoder(global) 上游 → 调无前缀 qoder 模型**放行**（版本门跳过）；
  * qoder 上游 + 版本不匹配 → 在 resolve_for_key 层被拒（503，R06 不回落）；
  * codebuddy 存量路径：cn 密钥调 global 模型**仍被版本门拦**（风险表 #3 的回归钉）；
  * `_bound_provider` 的边界：未绑定 / 绑定不存在的上游 → codebuddy。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, upstreamsvc  # noqa: E402


class BoundProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_unbound_and_missing_upstream_fall_back_to_codebuddy(self) -> None:
        self.assertEqual(keysvc._bound_provider({}), 'codebuddy')
        self.assertEqual(keysvc._bound_provider({'upstream_id': None}), 'codebuddy')
        self.assertEqual(keysvc._bound_provider({'upstream_id': 9999}), 'codebuddy',
                         '不存在的 uid 返回 codebuddy（该请求稍后被 resolve_for_key 打回）')

    def test_bound_qoder_detected(self) -> None:
        up = upstreamsvc.create_upstream('Q', 'http://127.0.0.1:8963',
                                         api_key='tok', provider='qoder')
        self.assertEqual(keysvc._bound_provider({'upstream_id': up['id']}), 'qoder')
        up2 = upstreamsvc.create_upstream('A', 'http://a.example')
        self.assertEqual(keysvc._bound_provider({'upstream_id': up2['id']}),
                         'codebuddy')


class ValidateRealmGateTest(unittest.TestCase):
    """validate 的版本门在 qoder 密钥上跳过、在 codebuddy 密钥上照常。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        db._conn = None
        db.connect()
        self.qoder_up = upstreamsvc.create_upstream(
            'Q-global', 'http://127.0.0.1:8964', api_key='tok',
            provider='qoder', realm='global')
        self.cb_up = upstreamsvc.create_upstream('A', 'http://a.example')

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_qoder_key_skips_model_name_realm_gate(self) -> None:
        key = keysvc.create_key('g', realm='global', upstream_id=self.qoder_up['id'])
        # 无前缀 qoder 模型名：若版本门没跳过，会按「无前缀=cn」误杀
        self.assertIsNone(keysvc.validate(key, '1.2.3.4', 'qmodel'),
                          'global 密钥 + qoder 上游调 qmodel 必须放行（C2）')

    def test_codebuddy_key_still_gated(self) -> None:
        """风险表 #3：跳过条件写错（如误用 !=）会让存量密钥越权——用例钉死。"""
        key = keysvc.create_key('c', realm='cn', upstream_id=self.cb_up['id'])
        reason = keysvc.validate(key, '1.2.3.4', 'global:gpt-5.6-sol')
        self.assertIsNotNone(reason, 'cn 密钥调 global 模型必须仍被版本门拦')
        self.assertEqual(getattr(reason, 'code'), 'realm_mismatch')

    def test_unmatched_realm_fails_at_resolve_layer(self) -> None:
        """不匹配场景不在 validate 层拦（版本门已跳过），在 resolve 层 503。"""
        key = keysvc.create_key('c', realm='cn', upstream_id=self.qoder_up['id'])
        self.assertIsNone(keysvc.validate(key, '1.2.3.4', 'qmodel'))
        with self.assertRaises(upstreamsvc.UpstreamUnavailable):
            upstreamsvc.resolve_for_key({'upstream_id': self.qoder_up['id'],
                                         'realm': key['realm']})


if __name__ == '__main__':
    unittest.main()

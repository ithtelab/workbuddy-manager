"""JSON account import accepts supported exports and isolates bad files."""
from __future__ import annotations

import json
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config, security
from server.routers import accounts


class AccountUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth_dir = Path(self.tmp.name) / 'auths'
        self.auth_dir.mkdir()
        self.group = {'id': None, 'name': '默认分组', 'auth_dir': str(self.auth_dir),
                      'is_default': True, 'base_url': 'http://127.0.0.1'}
        self.config_patch = mock.patch.object(config, 'AUTH_DIR', self.auth_dir)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.group_patch = mock.patch.object(accounts.upstreamsvc, 'default_upstream',
                                             return_value=self.group)
        self.group_patch.start()
        self.addCleanup(self.group_patch.stop)
        self.reload_patch = mock.patch.object(accounts.reload, 'request_reload_or_restart')
        self.reload = self.reload_patch.start()
        self.addCleanup(self.reload_patch.stop)
        app = FastAPI()
        app.include_router(accounts.router)
        app.dependency_overrides[security.require_session_admin] = lambda: {
            'username': 'test', 'role': 'admin'}
        self.client = TestClient(app)

    def test_imports_canonical_and_flat_exports(self) -> None:
        canonical = {
            'account': {'uid': 'canonical-1', 'nickname': 'Canonical'},
            'auth': {'accessToken': 'at-1', 'refreshToken': 'rt-1',
                     'expiresAt': 4102444800, 'domain': 'www.codebuddy.cn', 'realm': 'cn'},
            'device_token': 'device-1',
        }
        flat = [{'uid': 'flat-1', 'access_token': 'at-2', 'refresh_token': 'rt-2',
                 'expires_at': 4102444800, 'nickname': 'Flat'}]
        response = self.client.post(
            '/api/accounts/upload',
            files=[
                ('files', ('canonical.json', json.dumps(canonical), 'application/json')),
                ('files', ('flat.json', json.dumps(flat), 'application/json')),
            ],
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['uploaded']), 2)
        self.assertEqual(body['failed'], [])
        saved = json.loads((self.auth_dir / 'workbuddy-canonical-1.json').read_text())
        self.assertEqual(saved['auth']['accessToken'], 'at-1')
        self.assertEqual(saved['device_token'], 'device-1')
        self.assertEqual(json.loads((self.auth_dir / 'workbuddy-flat-1.json').read_text())
                         ['account']['nickname'], 'Flat')
        self.assertEqual(self.reload.call_count, 2)

    def test_bad_file_does_not_block_valid_file(self) -> None:
        response = self.client.post(
            '/api/accounts/upload',
            files=[
                ('files', ('bad.json', '{"account": {}}', 'application/json')),
                ('files', ('good.json', json.dumps({
                    'uid': 'good-1', 'access_token': 'at', 'expires_at': 4102444800,
                }), 'application/json')),
            ],
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([item['uid'] for item in body['uploaded']], ['good-1'])
        self.assertEqual(body['rejected'][0]['file'], 'bad.json')
        self.assertTrue((self.auth_dir / 'workbuddy-good-1.json').exists())

    def test_duplicate_uid_is_reported_as_update(self) -> None:
        self.client.post('/api/accounts/upload', files=[
            ('files', ('first.json', json.dumps({
                'uid': 'same-1', 'access_token': 'old', 'expires_at': 4102444800,
            }), 'application/json')),
        ])
        response = self.client.post('/api/accounts/upload', files=[
            ('files', ('second.json', json.dumps({
                'uid': 'same-1', 'access_token': 'new', 'expires_at': 4102444800,
            }), 'application/json')),
        ])
        self.assertEqual(response.json()['rejected'][0]['uid'], 'same-1')
        self.assertEqual(json.loads((self.auth_dir / 'workbuddy-same-1.json').read_text())
                         ['auth']['accessToken'], 'old')

        response = self.client.post('/api/accounts/upload?overwrite=true', files=[
            ('files', ('second.json', json.dumps({
                'uid': 'same-1', 'access_token': 'new', 'expires_at': 4102444800,
            }), 'application/json')),
        ])
        self.assertEqual(response.json()['overwritten'][0]['uid'], 'same-1')
        self.assertEqual(json.loads((self.auth_dir / 'workbuddy-same-1.json').read_text())
                         ['auth']['accessToken'], 'new')

    def test_imports_multiple_accounts_from_one_json_array(self) -> None:
        response = self.client.post('/api/accounts/upload', files=[
            ('files', ('accounts.json', json.dumps([
                {'uid': 'array-1', 'access_token': 'at-1', 'expires_at': 4102444800},
                {'uid': 'array-2', 'access_token': 'at-2', 'expires_at': 4102444800},
            ]), 'application/json')),
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['uid'] for item in response.json()['uploaded']], ['array-1', 'array-2'])

    def test_imports_flat_export_without_expiry(self) -> None:
        response = self.client.post('/api/accounts/upload', files=[
            ('files', ('flat-no-expiry.json', json.dumps({
                'uid': 'no-expiry', 'access_token': 'at', 'refresh_token': 'rt',
                'created_at': 4102444800,
            }), 'application/json')),
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['added'][0]['uid'], 'no-expiry')
        saved = json.loads((self.auth_dir / 'workbuddy-no-expiry.json').read_text())
        self.assertEqual(saved['auth']['expiresAt'], 0)

    def test_imports_expires_in_as_duration(self) -> None:
        with mock.patch.object(accounts.time, 'time', return_value=1_700_000_000):
            response = self.client.post('/api/accounts/upload', files=[
                ('files', ('flat-duration.json', json.dumps({
                    'uid': 'duration-1', 'access_token': 'at', 'expiresIn': 3600,
                }), 'application/json')),
            ])
        self.assertEqual(response.status_code, 200)
        saved = json.loads((self.auth_dir / 'workbuddy-duration-1.json').read_text())
        self.assertEqual(saved['auth']['expiresAt'], 1_700_003_600)

    def test_rejects_path_like_uid_without_writing_outside_auth_dir(self) -> None:
        response = self.client.post('/api/accounts/upload', files=[
            ('files', ('unsafe.json', json.dumps({
                'uid': '../x', 'access_token': 'at', 'expires_at': 4102444800,
            }), 'application/json')),
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['uploaded'], [])
        self.assertEqual(response.json()['rejected'][0]['file'], 'unsafe.json')
        self.assertFalse((self.auth_dir.parent / 'x.json').exists())

    def test_rejects_file_over_two_megabytes_without_writing(self) -> None:
        content = json.dumps({'uid': 'large', 'access_token': 'at',
                              'expires_at': 4102444800}).encode() + b' ' * (2 * 1024 * 1024)
        response = self.client.post('/api/accounts/upload', files=[
            ('files', ('large.json', content, 'application/json')),
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['failed'][0]['message'], '文件不能超过 2 MB')
        self.assertFalse((self.auth_dir / 'workbuddy-large.json').exists())

    def test_invalid_json_does_not_write_any_file(self) -> None:
        response = self.client.post('/api/accounts/upload', files=[
            ('files', ('invalid.json', b'{not json', 'application/json')),
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['failed'][0]['message'], '文件格式无效')
        self.assertEqual(list(self.auth_dir.glob('*')), [])

    def test_unknown_proxy_is_rejected_before_writing(self) -> None:
        response = self.client.post('/api/accounts/upload', files=[
            ('files', ('proxy.json', json.dumps({
                'uid': 'proxy-1', 'access_token': 'at', 'expires_at': 4102444800,
                'proxy': 'missing',
            }), 'application/json')),
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['rejected'][0]['message'], '账号线路无效')
        self.assertFalse((self.auth_dir / 'workbuddy-proxy-1.json').exists())

    def test_known_proxy_is_saved(self) -> None:
        with mock.patch.object(config, 'proxy_routes', return_value={'route-a': 'http://proxy.local:8080'}):
            response = self.client.post('/api/accounts/upload', files=[
                ('files', ('proxy.json', json.dumps({
                    'uid': 'proxy-2', 'access_token': 'at', 'expires_at': 4102444800,
                    'proxy': 'route-a',
                }), 'application/json')),
            ])
        self.assertEqual(response.json()['added'][0]['uid'], 'proxy-2')
        self.assertEqual(json.loads((self.auth_dir / 'workbuddy-proxy-2.json').read_text())['proxy'], 'route-a')

    def test_entry_failure_message_does_not_disclose_exception_path(self) -> None:
        with mock.patch.object(accounts.tencent, 'write_auth_file',
                               side_effect=OSError('C:\\private\\auths\\secret.json')):
            response = self.client.post('/api/accounts/upload', files=[
                ('files', ('error.json', json.dumps({
                    'uid': 'error-1', 'access_token': 'at', 'expires_at': 4102444800,
                }), 'application/json')),
            ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['failed'][0]['message'], '账号导入失败')
        self.assertNotIn('private', response.text)

    def test_exports_each_account_as_a_reimportable_json_file(self) -> None:
        enabled = {
            'account': {'uid': 'export-1', 'nickname': 'One'},
            'auth': {'accessToken': 'at-1', 'expiresAt': 4102444800},
        }
        disabled = {
            'account': {'uid': 'export-2', 'nickname': 'Two'},
            'auth': {'accessToken': 'at-2', 'expiresAt': 4102444800},
        }
        (self.auth_dir / 'workbuddy-export-1.json').write_text(
            json.dumps(enabled), encoding='utf-8')
        (self.auth_dir / 'workbuddy-export-2.json.disabled').write_text(
            json.dumps(disabled), encoding='utf-8')
        (self.auth_dir / 'ignore.txt').write_text('ignore', encoding='utf-8')

        response = self.client.get('/api/accounts/export')

        self.assertEqual(response.status_code, 200)
        self.assertIn('application/zip', response.headers['content-type'])
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(sorted(archive.namelist()), [
                'workbuddy-export-1.json', 'workbuddy-export-2.json',
            ])
            self.assertEqual(json.loads(archive.read('workbuddy-export-1.json')), enabled)
            self.assertEqual(json.loads(archive.read('workbuddy-export-2.json')), disabled)

    def test_export_requires_an_account_directory(self) -> None:
        group = {**self.group, 'auth_dir': ''}
        with mock.patch.object(accounts.upstreamsvc, 'default_upstream', return_value=group):
            response = self.client.get('/api/accounts/export')

        self.assertEqual(response.status_code, 409)


class ImportExportAuditTest(AccountUploadTest):
    """导入/导出都必须**留痕**，导出还必须禁止缓存（维护者复核补）。

    两边都是「把账号凭据搬来搬去」：不审计的话，事后看不出谁在什么时候拷走了
    哪些号；不禁止缓存的话，一个装着全部 token 的压缩包可能躺在中间层或浏览器
    缓存里。这两条是给这个功能上的最小护栏。
    """

    def test_export_writes_an_audit_entry(self) -> None:
        (self.auth_dir / 'workbuddy-audit-1.json').write_text(json.dumps({
            'account': {'uid': 'audit-1', 'nickname': 'A'},
            'auth': {'accessToken': 'at', 'expiresAt': 4102444800},
        }), encoding='utf-8')
        rows: list[tuple] = []
        with mock.patch.object(security, 'audit',
                               side_effect=lambda *a, **k: rows.append((a, k))):
            response = self.client.get('/api/accounts/export')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(rows, '导出没有留下审计记录')
        args, kwargs = rows[0]
        self.assertEqual(args[1], 'accounts.export')
        self.assertIn('1 个账号文件', args[3])
        self.assertIn('来源', args[3])
        self.assertIn('ip', kwargs, '导出审计应记录结构化来源 IP')

    def test_bulk_export_emits_warning(self) -> None:
        for i in range(5):
            (self.auth_dir / f'workbuddy-bulk-{i}.json').write_text(json.dumps({
                'account': {'uid': f'bulk-{i}', 'nickname': f'B{i}'},
                'auth': {'accessToken': 'at', 'expiresAt': 4102444800},
            }), encoding='utf-8')
        with mock.patch.object(accounts.logger, 'warning') as warning:
            response = self.client.get('/api/accounts/export')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any('批量导出账号凭据' in str(c)
                            for c in warning.call_args_list),
                        '批量导出没有告警')

    def test_export_response_is_not_cacheable(self) -> None:
        response = self.client.get('/api/accounts/export')
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers.get('cache-control', ''),
                      '装着账号凭据的压缩包被允许缓存了')

    def test_import_writes_an_audit_entry(self) -> None:
        payload = {
            'account': {'uid': 'audit-2', 'nickname': 'B'},
            'auth': {'accessToken': 'at-2', 'expiresAt': 4102444800},
        }
        rows: list[tuple] = []
        with mock.patch.object(security, 'audit',
                               side_effect=lambda *a, **k: rows.append((a, k))):
            response = self.client.post(
                '/api/accounts/upload',
                files=[('files', ('a.json', json.dumps(payload).encode(), 'application/json'))])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(rows, '导入没有留下审计记录')
        self.assertIn('accounts.import', str(rows[0][0]))


class AccountImportHardeningTest(AccountUploadTest):
    """三处收口：非 JSON 的提示、覆盖导入的线路保留、导出包不重复条目。

    前两条都来自现场：用户传 PDF / SVG / 导出包 ZIP 得到一句「文件内容无效」，
    不知道该传什么；覆盖导入一份没写 proxy 的文件会把账号的出口线路**静默清掉**。
    """

    def test_non_json_name_reports_actionable_message(self) -> None:
        """非 .json 的名字要给**可操作**的原因，而不是一句「文件内容无效」。

        以前这条检查的 ValueError 落进兜底分支被统一换成「文件内容无效」——
        既不说明支持什么，也不提「导出包是 ZIP、要先解压」。
        """
        for name in ('result.pdf', '1.svg', 'accounts.zip'):
            with self.subTest(name=name):
                response = self.client.post('/api/accounts/upload', files=[
                    ('files', (name, b'not json at all', 'application/octet-stream')),
                ])
                self.assertEqual(response.status_code, 200)
                message = response.json()['failed'][0]['message']
                self.assertIn('.json', message, '没有说明支持什么格式')
                self.assertIn('解压', message, '没有说明 ZIP 导出包要先解压')
                self.assertEqual(list(self.auth_dir.glob('*')), [],
                                 '非 JSON 文件不该在账号目录留下任何东西')

    def test_overwrite_keeps_proxy_and_token_when_omitted(self) -> None:
        """覆盖导入一份**没写** proxy / device_token 的文件：已有绑定必须保住。

        这两者都是「文件没提就不动」的语义（与 device_token 对齐）：之前 proxy
        会被静默清掉，账号出口随之无声改变——正是这个功能最忌讳的一类变更。
        """
        with mock.patch.object(config, 'proxy_routes',
                               return_value={'route-a': 'http://proxy.local:8080'}):
            first = {
                'account': {'uid': 'keep-1', 'nickname': '旧'},
                'auth': {'accessToken': 'at-old', 'refreshToken': 'rt-old',
                         'expiresAt': 4102444800},
                'device_token': 'dt-old',
                'proxy': 'route-a',
            }
            r1 = self.client.post('/api/accounts/upload', files=[
                ('files', ('a.json', json.dumps(first).encode(), 'application/json'))])
            self.assertEqual(r1.json()['added'][0]['uid'], 'keep-1')

            second = {
                'account': {'uid': 'keep-1', 'nickname': '新'},
                'auth': {'accessToken': 'at-new', 'refreshToken': 'rt-new',
                         'expiresAt': 4102444801},
            }
            r2 = self.client.post('/api/accounts/upload?overwrite=true', files=[
                ('files', ('b.json', json.dumps(second).encode(), 'application/json'))])
            self.assertEqual(r2.json()['overwritten'][0]['uid'], 'keep-1')

        saved = json.loads((self.auth_dir / 'workbuddy-keep-1.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['auth']['accessToken'], 'at-new')
        self.assertEqual(saved.get('device_token'), 'dt-old', 'device_token 被覆盖导入弄丢了')
        self.assertEqual(saved.get('proxy'), 'route-a',
                         'proxy 被覆盖导入静默清掉了 —— 账号出口会无声改变')

    def test_export_skips_duplicate_output_names(self) -> None:
        """`.json` 与 `.json.disabled` 同时存在（手工拷贝/异常状态）时不许出重复条目。

        两个文件会折成同一个导出名（`.disabled` 要被剥掉才能重新导入），zip 里
        出现**重复条目**时解压方行为依工具而异（Python 打包自己都会告警）。
        保留先扫到的 enabled 那份——上游实际加载的就是它。
        """
        enabled = {'account': {'uid': 'dup-1', 'nickname': '启用'},
                   'auth': {'accessToken': 'at-e', 'expiresAt': 4102444800}}
        disabled = {'account': {'uid': 'dup-1', 'nickname': '停用'},
                    'auth': {'accessToken': 'at-d', 'expiresAt': 4102444800}}
        (self.auth_dir / 'workbuddy-dup-1.json').write_text(
            json.dumps(enabled), encoding='utf-8')
        (self.auth_dir / 'workbuddy-dup-1.json.disabled').write_text(
            json.dumps(disabled), encoding='utf-8')

        response = self.client.get('/api/accounts/export')

        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)), f'出现重复条目：{names}')
            self.assertEqual(names, ['workbuddy-dup-1.json'])
            self.assertEqual(json.loads(archive.read('workbuddy-dup-1.json')), enabled)


if __name__ == '__main__':
    unittest.main()

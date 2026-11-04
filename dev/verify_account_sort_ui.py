"""Mocked account sorting and note workflow regression. Requires Playwright."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1]
shots = Path(os.environ.get('WB_SHOTS', str(Path(tempfile.gettempdir())/'wb-account-sort')))
shots.mkdir(parents=True, exist_ok=True)
now = int(time.time())
accounts = []
for i in range(23):
    accounts.append(dict(uid=f'u{i:02}', file=f'workbuddy-u{i:02}.json', nickname=f'账号{i:02}',
        realm='cn', expires_at=now+(i+1)*86400, remain_seconds=(i+1)*86400,
        is_expired=False, in_pool=True, healthy=True, credits=23-i,
        added_at=None if i == 0 else now+i, note=''))
notes_saved = []


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args): pass


server = ThreadingHTTPServer(('127.0.0.1', 7963), functools.partial(Quiet, directory=str(root/'web/out')))
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('WB_CHROMIUM'))
        context = browser.new_context(viewport={'width':1440, 'height':1000}, locale='zh-CN')
        context.add_init_script("localStorage.setItem('workbuddy-manager:locale','zh-CN');localStorage.setItem('theme','dark');localStorage.setItem('workbuddy-manager:dock-tip-dismissed','true')")

        def api(route):
            path = urlparse(route.request.url).path
            data = {}
            if path == '/api/me': data = dict(username='fixture', role='admin')
            elif path == '/api/upstreams': data = dict(items=[dict(id=1,name='测试分组',auth_dir='/auths',enabled=True,is_default=True)])
            elif path == '/api/accounts': data = dict(total=len(accounts),accounts=accounts,manageable=True)
            elif path == '/api/status': data = dict(connected=True,accounts=accounts,total=len(accounts),healthy=len(accounts),disabled=0)
            elif path == '/api/accounts/refresh-credits': data = dict(total=len(accounts),succeeded=len(accounts),failed=[],credits={a['uid']:a['credits'] for a in accounts},meta={})
            elif path == '/api/account-risk': data = dict(items=[],failed_groups=[],total=0)
            elif path.endswith('/note'):
                assert route.request.method == 'PUT'
                notes_saved.append(path)
                account = next(a for a in accounts if a['file'] in path)
                account['note'] = route.request.post_data_json['note']
                data = dict(ok=True,note=account['note'])
            route.fulfill(status=200,content_type='application/json',body=json.dumps(data))

        context.route('**/api/**', api)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto('http://127.0.0.1:7963/accounts', wait_until='networkidle')

        def choose(label):
            page.get_by_role('combobox',name='排序方式').click()
            page.get_by_role('option',name=label,exact=True).click()

        def first_is(name): expect(page.locator('tbody tr').first).to_contain_text(name)
        def toggle(): page.get_by_role('button',name='点击切换排序方向',exact=False).click()

        first_is('账号00')
        choose('按有效期排序'); first_is('账号00')
        toggle(); first_is('账号22')
        choose('按积分排序'); first_is('账号22')
        toggle(); first_is('账号00')
        choose('按添加时间排序'); first_is('账号22')
        toggle(); first_is('账号01')
        expect(page.get_by_text('账号00',exact=True)).to_have_count(0)  # Unknown addition stays after the known 22.
        toggle(); first_is('账号22')
        page.screenshot(path=str(shots/'added-desc-desktop.png'))

        row = page.locator('tbody tr').first
        row.locator('button[title="添加备注"]').click()
        dialog = page.get_by_role('dialog',name='账号备注')
        dialog.get_by_role('textbox').fill('新账号备注')
        dialog.get_by_role('button',name='保存',exact=True).click()
        expect(dialog).to_be_hidden()
        assert notes_saved == ['/api/accounts/workbuddy-u22.json/note'], notes_saved
        first_is('新账号备注')

        page.set_viewport_size({'width':390,'height':900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        choose('按有效期排序')
        toggle()
        expect(page.get_by_role('button',name='当前降序，点击切换排序方向')).to_be_visible()
        page.screenshot(path=str(shots/'sort-mobile.png'))
        choose('默认顺序')
        expect(page.get_by_role('button',name='点击切换排序方向',exact=False)).to_have_count(0)
        assert not errors, errors
        browser.close()
    print('PASS: expiry/credits/added asc+desc, newest first, unknown last, correct-account note save, mobile layout, default order, no runtime errors; mocked APIs only')
finally:
    server.shutdown()
    server.server_close()

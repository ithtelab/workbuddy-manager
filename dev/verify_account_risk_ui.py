"""Account-risk UI regression with mocked APIs only; requires Playwright.

Serve web/out at 127.0.0.1:7960, then run this script.
WB_BASE, WB_SHOTS and WB_CHROMIUM override the server, screenshots and browser.
"""
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

base = os.environ.get('WB_BASE', 'http://127.0.0.1:7960')
shots = Path(os.environ.get('WB_SHOTS', str(Path(tempfile.gettempdir()) / 'wb-risk-ui')))
shots.mkdir(parents=True, exist_ok=True)
now = int(time.time())
common = dict(realm='cn', expires_at=now+86400*30, remain_seconds=86400*30,
              is_expired=False, in_pool=True, disabled=False, success_count=2, credits=100)
accounts = [dict(common, uid='good', nickname='正常账号', file='workbuddy-good.json', healthy=True),
            dict(common, uid='paused', nickname='保护暂停账号', file='workbuddy-paused.json', healthy=False,
                 manual_disabled=False, refresh_risk=dict(until=now+300,last_failure_at=now,failures=3,needs_review=True,paused=True)),
            dict(common,uid='soft',nickname='11140软冷却账号',file='workbuddy-soft.json',healthy=False,cooling=True),
            dict(common,uid='manual',nickname='人工停用账号',file='workbuddy-manual.json',healthy=False,manual_disabled=True,manual_reason='account-risk: operator text'),
            dict(common,uid='auto',nickname='自动到期账号',file='workbuddy-auto.json',healthy=False,refresh_risk=dict(until=now+300,last_failure_at=now,failures=1,needs_review=False,paused=True))]
state = dict(ack=False, unknown=False, role='admin', resumed=False)

with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=os.environ.get('WB_CHROMIUM'))
    context = browser.new_context(viewport={'width':1440,'height':1000}, locale='zh-CN')
    context.add_init_script("localStorage.setItem('workbuddy-manager:locale','zh-CN');localStorage.setItem('theme','dark');localStorage.setItem('workbuddy-manager:dock-tip-dismissed','true')")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    def api(route):
        path = urlparse(route.request.url).path
        data = {}
        if path == '/api/me': data = dict(username='regression', role=state['role'])
        elif path == '/api/upstreams': data = dict(items=[dict(id=1,name='测试分组',auth_dir='/auths',enabled=True,is_default=True)])
        elif path == '/api/accounts': data = dict(total=len(accounts), accounts=accounts, manageable=True)
        elif path == '/api/upstream/status': data = dict(connected=True, accounts=accounts, total=2, healthy=1, disabled=1)
        elif path == '/api/accounts/refresh-credits': data = dict(total=2, succeeded=2, failed=[], credits={'good':100,'paused':100}, meta={})
        elif path == '/api/stats/daily': data = []
        elif path == '/api/stats/credits': data = dict(today={},week={},total={},daily=[])
        elif path == '/api/accounts/admin-api-status': data = dict(enabled=True)
        elif path.startswith('/api/account-risk/'):
            assert route.request.method == 'POST'
            if path.endswith('/resume'):
                state['resumed'] = True
                accounts[1]['refresh_risk'].update(until=0,needs_review=False,paused=False)
            else:
                state['ack'] = True
            data = dict(ok=True)
        elif path == '/api/account-risk':
            warning = dict(id='paused-event',uid='paused',nickname='保护暂停账号',group='测试分组',realm='cn',
                           operation='token_refresh',http_status=429,ts=now,historical=False,
                           acknowledged=state['ack'],paused=None if state['unknown'] else not state['resumed'],action_error='', pause_until=now+300,needs_review=not state['resumed'],failures=3)
            history = dict(warning,id='history-event',uid='good',nickname='正常账号',ts=now-86400,
                           historical=True,paused=False,acknowledged=False)
            data = dict(items=[warning] + ([history] if not state['ack'] or 'include_acknowledged=true' in route.request.url else []),failed_groups=['测试分组'] if state['unknown'] else [],total=2)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(data))
    context.route('**/api/**', api)
    page.goto(base+'/dashboard', wait_until='networkidle')
    panel = page.get_by_role('region', name='账号保护预警')
    panel.wait_for()
    assert '暂停·需人工复核' in panel.inner_text()
    assert '历史记录（未据此自动暂停）' in panel.inner_text()
    assert '429 本身不能证明账号被封' in panel.inner_text()
    for width in (1440, 390):
        page.set_viewport_size({'width':width,'height':900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(shots/f'risk-dashboard-{width}.png'))
    page.get_by_role('button',name='确认当前预警',exact=True).click()
    page.get_by_role('button',name='确认当前预警',exact=True).wait_for(state='hidden')
    assert '暂停·需人工复核' in panel.inner_text(), 'Acknowledging must not resume the account'
    assert '历史记录（未据此自动暂停）' not in panel.inner_text()
    panel.get_by_role('checkbox',name='查看已确认的历史预警').check()
    page.wait_for_timeout(300)
    assert '历史记录（未据此自动暂停）' in panel.inner_text()
    page.reload(wait_until='networkidle')
    assert '暂停·需人工复核' in panel.inner_text(), 'An acknowledged active pause must survive reload'
    page.set_viewport_size({'width':1440,'height':1000})
    page.goto(base+'/accounts',wait_until='networkidle')
    page.locator('tbody tr').first.wait_for()
    assert page.get_by_role('region', name='账号保护预警').count() == 1
    good = page.locator('tbody tr').filter(has_text='正常账号')
    assert '在线' in good.inner_text(), 'Healthy account must remain usable'
    assert '冷却中' in page.locator('tbody tr').filter(has_text='11140软冷却账号').inner_text()
    assert '已停用' in page.locator('tbody tr').filter(has_text='人工停用账号').inner_text()
    assert '刷新限流暂停' in page.locator('tbody tr').filter(has_text='自动到期账号').inner_text()
    page.screenshot(path=str(shots/'risk-accounts.png'))
    state['unknown'] = True
    page.reload(wait_until='networkidle')
    panel = page.get_by_role('region', name='账号保护预警')
    panel.locator('summary').click()
    assert '暂停状态待确认' in panel.inner_text()
    assert '暂时无法确认' in panel.inner_text()
    state['unknown'] = False
    state['role'] = 'viewer'
    page.reload(wait_until='networkidle')
    assert page.get_by_role('button',name='已复核，解除保护暂停').count() == 0
    state['role'] = 'admin'
    page.reload(wait_until='networkidle')
    page.get_by_role('button',name='已复核，解除保护暂停').click()
    page.wait_for_timeout(300)
    assert state['resumed']
    assert not errors, errors
    browser.close()
    print('PASS: persistent risk warnings on dashboard/accounts, acknowledgement does not resume, historical/active/unknown states, healthy account unaffected, desktop/mobile layout, no browser errors')

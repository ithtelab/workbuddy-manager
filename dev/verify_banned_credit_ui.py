"""封禁提示、重登队列与积分可用性的浏览器回归（仅模拟 API）。

先运行 web 开发服务或在 7960 端口托管静态导出，再执行本脚本。
需要 Python Playwright 与 Chromium；WB_BASE、WB_SHOTS、WB_CHROMIUM 可覆盖默认值。
"""
import json
import os
import time
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

BASE = os.environ.get('WB_BASE', 'http://127.0.0.1:7960')
ROOT = Path(os.environ.get('WB_SHOTS', str(Path(tempfile.gettempdir()) / 'wb-banned-credit-ui')))
ROOT.mkdir(parents=True, exist_ok=True)
now = int(time.time())
common = dict(expires_at=now+86400*20, remain_seconds=86400*20, is_expired=False,
              realm='cn', in_pool=True, success_count=3, disabled=False)
accounts = [dict(common, uid='ok', file='workbuddy-ok.json', nickname='正常账号', healthy=True, credits=100),
            dict(common, uid='ban', file='workbuddy-ban.json', nickname='封禁账号', healthy=False, credits=96066,
                 disabled=True, disabled_reason='account banned by upstream (request illegal, code 11140, http 403), re-login required'),
            dict(common, uid='session', file='workbuddy-session.json', nickname='会话失效账号', credits=20,
                 healthy=False, disabled=True, disabled_reason='12153 session dead'),
            dict(common, uid='cool', file='workbuddy-cool.json', nickname='冷却账号', healthy=False, credits=10, cooling=True)]

with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=os.environ.get('WB_CHROMIUM'))
    page = browser.new_page(viewport={'width':1440,'height':1000}, locale='zh-CN')
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('workbuddy-manager:locale','zh-CN');localStorage.setItem('theme','dark');localStorage.setItem('workbuddy-manager:dock-tip-dismissed','true')")
    def api(route):
        path = urlparse(route.request.url).path
        data = {}
        if path == '/api/me': data = dict(username='regression',role='admin')
        elif path == '/api/upstreams': data = dict(items=[dict(id=1,name='默认分组',auth_dir='/app/auths',is_default=True,enabled=True)])
        elif path == '/api/accounts': data = dict(total=4, accounts=accounts, manageable=True)
        elif path == '/api/upstream/status': data = dict(connected=True,accounts=accounts,total=4,healthy=1,disabled=2,cooling=1)
        elif path == '/api/accounts/refresh-credits': data = dict(total=4,succeeded=4,failed=[],credits={a['uid']:a['credits'] for a in accounts},meta={})
        elif path == '/api/accounts/admin-api-status': data = dict(enabled=True)
        elif path == '/api/stats/credits': data = dict(days=7,today={},week={},total={},daily=[])
        elif path == '/api/stats/daily': data = []
        elif path == '/api/model-catalog': data = dict(models=[])
        route.fulfill(status=200, content_type='application/json', body=json.dumps(data))
    page.route('**/api/**',api)
    page.goto(BASE+'/dashboard',wait_until='networkidle')
    card = page.get_by_text('可用积分',exact=True).locator('../..')
    card.wait_for()
    # 冷却账号的 10 分计入可用（#381）；封禁 96066 + 会话失效 20 仍不可用。
    assert '110' in card.inner_text(),card.inner_text()
    assert '不可用积分: 96,086' in card.inner_text(),card.inner_text()
    page.screenshot(path=str(ROOT/'dashboard-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert card.bounding_box()['width'] < 390
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile page overflow'
    page.screenshot(path=str(ROOT/'dashboard-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1000})
    page.goto(BASE+'/accounts',wait_until='networkidle')
    row = page.locator('tbody tr').filter(has_text='封禁账号')
    assert '账号被封禁' in row.inner_text(),row.inner_text()
    row.get_by_text('● 账号被封禁').hover()
    page.get_by_role('tooltip').filter(has_text='重新登录无法解除').wait_for()
    assert 're-login required' not in page.get_by_role('tooltip').inner_text()
    page.mouse.move(0,0)
    assert row.get_by_role('button',name='上游因账号异常行为限制使用；重新登录无法解除，请向腾讯提交反馈申诉。').is_disabled()
    page.screenshot(path=str(ROOT/'accounts-banned.png'))
    page.get_by_role('button',name='重登队列').click()
    dialog = page.get_by_role('dialog')
    dialog.wait_for()
    assert '会话失效账号' in dialog.inner_text()
    assert '封禁账号' not in dialog.inner_text()
    page.screenshot(path=str(ROOT/'relogin-queue.png'))
    assert not errors,errors
    print('PASS: available=110, unavailable=96086; desktop/mobile layout, ban badge, appeal hint, disabled enable button, session-only relogin queue; no browser errors')
    browser.close()

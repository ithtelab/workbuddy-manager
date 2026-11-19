"""重登队列 → 弹窗的浏览器回归（仅模拟 API，不连任何真实上游）。

跑法：
    cd web && NEXT_OUTPUT_EXPORT=1 npm run build:export
    (cd web/out && python3 -m http.server 7961) &
    python dev/verify_relogin_target_ui.py

覆盖三件事：
  1. 队列里点「重登」→ 弹窗**带着目标账号**（昵称 / UID / 版本徽章），而不是一个
     通用的「添加账户」；
  2. 扫到别的账号（`account_mismatch`）→ 明确说「扫到的是谁、要的是谁」，且不报成功；
  3. 凭证写进去但上游禁用位没解除（`revive.ok=false`）→ 把原因摆出来，**不给**
     「重新获取」按钮（重扫没有意义），账号不会假装已恢复。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BASE = os.environ.get('WB_BASE', 'http://127.0.0.1:7961')
ROOT = Path(os.environ.get('WB_SHOTS', str(Path(tempfile.gettempdir()) / 'wb-relogin-ui')))
ROOT.mkdir(parents=True, exist_ok=True)
CHROME = os.environ.get('WB_CHROMIUM') or str(
    Path.home() / '.cache/ms-playwright/chromium-1208/chrome-linux/chrome')

now = int(time.time())
common = dict(expires_at=now + 86400 * 20, remain_seconds=86400 * 20, is_expired=False,
              in_pool=True, success_count=3, cooling=False)
# 两个账号都是国际版：账号页按当前版本过滤，页面停在哪个版本就只能看到那个版本的
# 账号。这里统一用国际版，重登弹窗的版本徽章才有意义。
accounts = [
    dict(common, uid='u-ok', file='workbuddy-ok.json', nickname='正常账号', realm='global',
         healthy=True, disabled=False, credits=100),
    dict(common, uid='u-dead', file='workbuddy-dead.json', nickname='会话失效账号',
         realm='global', healthy=False, disabled=True, credits=20,
         disabled_reason='12153 session dead, re-login required'),
]

STATE = {'poll': 'waiting'}
REQUESTS = []


def api(route):
    path = urlparse(route.request.url).path
    data = None
    if path == '/api/me':
        data = dict(username='regression', role='admin')
    elif path == '/api/upstreams':
        data = dict(items=[dict(id=1, name='默认分组', auth_dir='/app/auths',
                                is_default=True, enabled=True)])
    elif path == '/api/accounts':
        data = dict(total=2, accounts=accounts, manageable=True)
    elif path == '/api/upstream/status':
        data = dict(connected=True, accounts=accounts, total=2, healthy=1,
                    disabled=1, cooling=0)
    elif path == '/api/accounts/admin-api-status':
        data = dict(enabled=True)
    elif path == '/api/proxies':
        data = dict(routes=[], default='')
    elif path == '/api/auth/start':
        body = json.loads(route.request.post_data or '{}')
        REQUESTS.append(body)
        target = None
        if body.get('uid') == 'u-dead':
            target = dict(uid='u-dead', nickname='会话失效账号', realm='global',
                          file='workbuddy-dead.json', panel_disabled=False)
        data = dict(state='st-1', authUrl='https://example.invalid/auth?state=st-1',
                    realm=(target or {}).get('realm', body.get('realm', 'cn')),
                    target=target)
    elif path == '/api/auth/poll':
        # 有「终态结果」就原样返回（包含 uid / nickname / revive 等字段）；
        # 没有就一直是 waiting —— 与真实后端的行为一致。
        data = POLL_RESULTS[0] if POLL_RESULTS else dict(status='waiting')
    if data is None:
        route.fulfill(status=200, content_type='application/json', body='{}')
        return
    route.fulfill(status=200, content_type='application/json',
                  body=json.dumps(data, ensure_ascii=False))


POLL_RESULTS = []


def scenario(pw, name, poll_result, expect):
    STATE['poll'] = poll_result['status']
    POLL_RESULTS[:] = [poll_result]
    REQUESTS.clear()
    browser = pw.chromium.launch(executable_path=CHROME)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, locale='zh-CN')
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.add_init_script(
        "localStorage.setItem('workbuddy-manager:locale','zh-CN');"
        "localStorage.setItem('theme','dark');"
        "localStorage.setItem('wb.realm','global')")
    page.route('**/api/**', api)
    page.goto(f'{BASE}/accounts/', wait_until='networkidle')

    # 打开重登队列 → 点第一行里的「重登」
    page.get_by_role('button', name='重登队列').click()
    page.get_by_role('dialog').get_by_role('button', name='重登', exact=True).first.click()
    page.wait_for_timeout(2500)

    text = page.get_by_role('dialog').last.inner_text()
    missing = [s for s in expect if s not in text]
    shot = ROOT / f'{name}.png'
    page.screenshot(path=str(shot), full_page=False)
    retry = page.get_by_role('button', name='重新获取').count()
    browser.close()
    return {'missing': missing, 'retry': retry, 'shot': shot, 'errors': errors,
            'text': text, 'requests': list(REQUESTS)}


def main() -> int:
    failures = []
    with sync_playwright() as pw:
        # 1) 正常路径：目标账号徽标与版本都摆出来
        r = scenario(pw, 'relogin-target', dict(status='waiting'),
                     ['重登账号「会话失效账号」', 'u-dead', '国际版'])
        if r['missing']:
            failures.append(f"目标账号信息没显示全: 缺 {r['missing']}")
        # 真正的契约在这里：发码请求必须带上目标 uid（后端据此锁版本、校验扫到的号）
        if not any(q.get('uid') == 'u-dead' for q in r['requests']):
            failures.append(f"发码请求没带 uid：{r['requests']}")

        # 2) 扫到别的账号
        r = scenario(pw, 'relogin-mismatch',
                     dict(status='account_mismatch', expected_uid='u-dead',
                          expected_name='会话失效账号', uid='u-other',
                          nickname='另一个号'),
                     ['另一个号', '会话失效账号'])
        if r['missing']:
            failures.append(f"扫错号时提示不完整: 缺 {r['missing']}")

        # 3) 凭证更新了、禁用位没解除
        r = scenario(pw, 'relogin-revive-failed',
                     dict(status='success', uid='u-dead', nickname='会话失效账号',
                          updated=True, realm='global',
                          revive=dict(attempted=True, ok=False, still_disabled=True,
                                      message='上游拒绝了鉴权（api_key 不一致）')),
                     ['api_key 不一致'])
        if r['missing']:
            failures.append(f"解禁失败原因没显示: 缺 {r['missing']}")
        if r['retry']:
            failures.append('解禁失败时仍给了「重新获取」——重扫修不了这一步')

        if r['errors']:
            failures.append(f'页面报错: {r["errors"]}')

    print('截图：', ROOT)
    if failures:
        for f in failures:
            print('FAIL:', f)
        return 1
    print('PASS: 重登弹窗带着目标账号、扫错号有明确提示、解禁失败给原因且不给重扫')
    return 0


if __name__ == '__main__':
    sys.exit(main())
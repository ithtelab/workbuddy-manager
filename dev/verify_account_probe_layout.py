"""账号状态与模型探测的浏览器回归验收（仅使用模拟 API）。

先运行 cd web && npm run dev -- --hostname 127.0.0.1 --port 7960，随后：
    python dev/verify_account_probe_layout.py

需要 Python playwright 与 Chromium。WB_BASE / WB_SHOTS 可覆盖地址与截图目录。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BASE = os.environ.get('WB_BASE', 'http://127.0.0.1:7960')
SHOTS = Path(os.environ.get('WB_SHOTS', str(Path(tempfile.gettempdir()) / 'wb-account-probe-layout')))
NOW = int(time.time())
MODELS = ['deepseek-v4.1-flash', 'global:very-long-model-name-' + 'x' * 80] + [f'model-{i:02}' for i in range(22)]
MODEL_NAMES = dict(zip(MODELS[:6], ('Auto', 'Hy3', 'A', 'B', 'C', 'D')))
STATUSES = ('ok', 'rate', 'credit', 'auth', 'no_model', 'error')
CREDIT_META = {
    f'ui-{i}': dict(cached=i % 2 == 1, cache_age=24 if i % 2 == 1 else None,
                   expiries=[dict(amount=amount, at=NOW + seconds)])
    for i, (amount, seconds) in enumerate(((47, 8 * 86400 + 3600), (788, 20 * 86400 + 3600),
                                         (14, 6 * 3600), (431, 86400 + 3600)))
}
ACCOUNTS = [dict(
    file=f'workbuddy-ui-{i}.json', uid=f'ui-{i}', nickname=f'测试账号 {i:02}',
    enterprise_id='', expires_at=NOW + 27 * 86400, remain_seconds=27 * 86400,
    is_expired=False, source='file', realm='cn', in_pool=True, disabled=False,
    healthy=i > 1, cooling=i < 2, cool_remaining_sec=6480 if i == 0 else 1560,
    credits=3242, success_count=5, err_total=0,
    rate_limited_models=[dict(model=m, until='2000-01-01 12:00:00' if i == 5 else '2099-01-01 12:00:00',
                             reason='11102 model not available' if m == 'model-00' else '6004 model rate limit')
                         for m in MODELS[:({0: 1, 1: 2, 2: 3, 3: 24, 4: 0}.get(i, 1))]],
) for i in range(43)]

# Range 矩形检查实际文字是否被父单元格裁切，而非只检查 textContent。
VISIBLE_BADGES = """() => [...document.querySelectorAll('tbody tr')].flatMap(row => {
  const cell = row.cells[2];
  if (!cell) return [];
  const bounds = cell.getBoundingClientRect();
  return [...cell.querySelectorAll('[data-slot="badge"],button')].flatMap(badge => {
    const range = document.createRange(); range.selectNodeContents(badge);
    return [...range.getClientRects()].filter(r => r.width && r.height).flatMap(r =>
      r.left < bounds.left - 1 || r.right > bounds.right + 1 ||
      r.top < bounds.top - 1 || r.bottom > bounds.bottom + 1
        ? [badge.textContent] : []);
  });
})"""

MULTILINE_STATUS = """root => [...root.querySelectorAll('tbody td > span')].flatMap(badge => {
  const range = document.createRange(); range.selectNodeContents(badge);
  const lines = new Set([...range.getClientRects()].filter(r => r.width && r.height).map(r => Math.round(r.top)));
  return lines.size > 1 ? [badge.textContent] : [];
})"""

CREDIT_POSITIONS = """() => [...document.querySelectorAll('span[title]')]
  .filter(e => /^\\d[\\d,.]*\\s*·/.test(e.textContent.trim()) && e.getBoundingClientRect().width > 0)
  .map(hint => {
    const balance = hint.parentElement.querySelector('span[title]');
    const h = hint.getBoundingClientRect(), b = balance.getBoundingClientRect();
    return {text: hint.textContent, below: h.top >= b.bottom, aligned: Math.abs(h.left - b.left) < 1};
  })"""


def main() -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    requests = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('WB_CHROMIUM'))
        page = browser.new_page(viewport={'width': 1440, 'height': 900}, locale='zh-CN')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script("localStorage.setItem('workbuddy-manager:locale','zh-CN'); localStorage.setItem('theme','dark'); localStorage.setItem('workbuddy-manager:dock-tip-dismissed','true')")

        def api(route):
            path = urlparse(route.request.url).path
            data = {}
            if path == '/api/me':
                data = dict(username='ui-test', role='admin')
            elif path == '/api/accounts':
                data = dict(total=len(ACCOUNTS), accounts=ACCOUNTS, manageable=True)
            elif path == '/api/status':
                data = dict(connected=True, accounts=ACCOUNTS, total=43, healthy=41, cooling=2)
            elif path == '/api/upstreams':
                data = dict(items=[])
            elif path == '/api/proxies':
                data = dict(routes=['test-route'], default='')
            elif path == '/api/accounts/refresh-credits':
                data = dict(total=43, succeeded=43, failed=[],
                            credits={f'ui-{i}': 4683 - i * 150 for i in range(5)}, meta=CREDIT_META)
            elif path == '/api/model-catalog':
                data = dict(models=[dict(id=m, name=MODEL_NAMES.get(m, m)) for m in MODELS])
            elif path == '/api/accounts/probe-models':
                body = route.request.post_data_json
                requests.append(body)
                results = []
                for file in body['accounts']:
                    for model in body['models']:
                        status = STATUSES[MODELS.index(model) % len(STATUSES)]
                        results.append(dict(file=file, model=model, status=status, message='mock ' + status,
                                            gateway_synced=status == 'ok', gateway_sync_skipped=status != 'ok'))
                data = dict(results=results, summary={s: sum(r['status'] == s for r in results) for s in STATUSES},
                            requests=len(results))
            route.fulfill(status=200, content_type='application/json', body=json.dumps(data))

        page.route('**/api/**', api)
        page.goto(BASE + '/accounts', wait_until='networkidle')
        page.locator('tbody tr').first.wait_for()
        for width in (768, 1024, 1280, 1440, 1920):
            page.set_viewport_size(dict(width=width, height=900))
            assert not page.evaluate(VISIBLE_BADGES), f'{width}px: 状态文字被裁切'
            assert '小时' in page.locator('tbody tr').first.locator('td').nth(2).inner_text()
            assert page.locator('[data-slot="table-container"]').first.evaluate('(e) => e.scrollWidth <= e.clientWidth + 1')
            positions = page.evaluate(CREDIT_POSITIONS)
            assert len(positions) == 4 and all(p['below'] and p['aligned'] for p in positions), f'{width}px: 到期提示位置不一致: {positions}'
        page.set_viewport_size(dict(width=390, height=844))
        positions = page.evaluate(CREDIT_POSITIONS)
        assert len(positions) == 4 and all(p['below'] and p['aligned'] for p in positions), positions
        page.set_viewport_size(dict(width=1440, height=900))
        rows = page.locator('tbody tr')
        for index, label in enumerate(('模型受限', '模型受限 +1', '模型受限 +2', '模型受限 +23')):
            assert rows.nth(index).get_by_role('button', name=label, exact=True).count() == 1
        for index in (4, 5):
            assert rows.nth(index).get_by_role('button', name='模型受限', exact=False).count() == 0
        assert rows.nth(2).bounding_box()['height'] == rows.nth(3).bounding_box()['height'], '模型多不能撑高账号行'
        page.screenshot(path=str(SHOTS / 'status.png'))
        print('PASS: 768/1024/1280/1440/1920px 状态文字完整、小时单位可见、表格无横向溢出')
        print('PASS: 实时/缓存积分及普通/临期/紧急到期提示，在桌面和手机宽度下统一位于余额下方并左对齐')

        rows.nth(3).get_by_role('button', name='模型受限 +23', exact=True).click()
        details = page.get_by_role('dialog')
        assert details.get_by_role('heading', name='受限模型（24 个）').count() == 1
        assert details.locator('li').count() == 24
        assert MODELS[1] in details.inner_text()
        assert '该账号没有这个模型' in details.inner_text()
        assert '预计' in details.inner_text() and '恢复' in details.inner_text()
        for width in (1440, 390):
            page.set_viewport_size(dict(width=width, height=844))
            details.locator('li').last.scroll_into_view_if_needed()
            assert details.evaluate('(e) => e.scrollWidth <= e.clientWidth + 1')
            rect = details.get_by_role('button', name='关闭', exact=True).bounding_box()
            assert rect and rect['y'] + rect['height'] <= 844
            page.screenshot(path=str(SHOTS / f'limits-{width}.png'))
        details.get_by_role('button', name='关闭', exact=True).click()
        details.wait_for(state='detached')
        page.set_viewport_size(dict(width=1440, height=900))
        print('PASS: 1/2/3/24 个受限模型显示正确、0 个及已过期不显示、详情完整且列表行不增高')

        page.get_by_role('button', name='模型探测', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.wait_for()
        # 两个列表用标题定位，避免依赖按钮在整个页面中的序号。
        account_list = dialog.locator('div[class*="sm:grid-cols-2"] > div').nth(0)
        model_list = dialog.locator('div[class*="sm:grid-cols-2"] > div').nth(1)
        model_list.locator('input').first.check()
        dialog.get_by_role('button', name='开始探测', exact=True).click()
        dialog.get_by_role('button', name='开始探测', exact=True).wait_for()
        assert len(requests) == 3  # 20 + 20 + 3，保留现有分批行为。
        assert dialog.locator('thead th').count() == 2
        account_list.get_by_role('button', name='清空', exact=True).click()
        account_list.locator('input').first.check()
        model_list.get_by_role('button', name='全选', exact=True).click()
        assert '已完成 0/24 项' in dialog.inner_text(), '新选择不能沿用上一次的进度'
        assert dialog.locator('thead th').count() == 2, '新选择不应改变旧结果的列'
        assert dialog.locator('tbody tr').count() == 43, '新选择不应改变旧结果的行'
        assert len(requests) == 3, '改变选择不应发出探测请求'
        button = model_list.get_by_role('button', name='清空', exact=True)
        assert button.evaluate("e => parseFloat(getComputedStyle(e).borderTopWidth) > 0 && !!e.querySelector('svg')")
        page.screenshot(path=str(SHOTS / 'reselect.png'))
        print('PASS: 全账号单模型探测 → 清空账号 → 单账号 → 全选模型，旧结果保持稳定')

        dialog.get_by_role('button', name='开始探测', exact=True).click()
        dialog.get_by_role('button', name='开始探测', exact=True).wait_for()
        assert dialog.locator('thead th').count() == len(MODELS) + 1
        assert dialog.locator('tbody tr').count() == 1
        assert len(requests) == 6  # 24 个模型分为 10 + 10 + 4。
        assert set(dialog.locator('tbody td > span').all_text_contents()) == {
            '可用', '模型限流', '额度不足', '凭证失效', '无此模型', '错误',
        }
        scroller = dialog.locator('table').locator('..')
        assert scroller.evaluate('(e) => e.scrollWidth > e.clientWidth'), '宽结果应在表格内部横向滚动'
        for width in (1440, 768, 390):
            page.set_viewport_size(dict(width=width, height=844))
            bounds = dialog.bounding_box()
            assert bounds and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width + 1
            assert dialog.evaluate('(e) => e.scrollWidth <= e.clientWidth + 1'), '弹窗整体不得横向滚动'
            dialog.locator('tbody tr').first.scroll_into_view_if_needed()
            wrapped = dialog.evaluate(MULTILINE_STATUS)
            assert not wrapped, f'{width}px: 探测状态不应换行成竖排: {wrapped}'
            for name in ('取消', '开始探测'):
                rect = dialog.get_by_role('button', name=name, exact=True).bounding_box()
                assert rect and rect['y'] >= 0 and rect['y'] + rect['height'] <= 844
            page.screenshot(path=str(SHOTS / f'wide-results-{width}.png'))
        assert not errors, errors
        browser.close()
        print('PASS: 多模型结果内部滚动、手机与桌面弹窗及操作按钮保持可见；无运行时错误')
        print('PASS: Auto / Hy3 等短表头下，六种探测结果在桌面和手机宽度均为横向单行')
        print(f'ALL CHECKS PASSED; screenshots: {SHOTS}')


if __name__ == '__main__':
    main()

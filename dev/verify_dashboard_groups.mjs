/**
 * 首页统计覆盖全部分组的浏览器断言（配合 verify_dashboard_groups_ui.py）。
 *
 * 三个数互不相等，断言才有分辨力：默认分组 8 个号 + 乙组 3 个号 →
 * 只报默认分组 = 8（修前）、全部聚合 = 11（正确）、把只转发的分组也算进分母
 * 则范围说明会写成 3。
 *
 * 顺带验「查看全部账号」入口（快照只画前 9 条）：11 条时要出现并能跳到账号页，
 * 只剩 8 条时（乙组取不到）必须消失——否则用户点进去只看到 8 条会以为页面骗他。
 */
const BASE = process.env.WB_BASE;
const PASS = process.env.WB_PASS;
const OUT = process.env.WB_SHOTS;
const GROUP_ID = process.env.WB_GROUP_ID;

import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

async function loadPlaywright() {
  const candidates = [
    ...(process.env.WB_PLAYWRIGHT ? [process.env.WB_PLAYWRIGHT] : []),
    path.join(process.env.TEMP || '/tmp', 'wb-i18n-verify', 'node_modules', 'playwright-core', 'index.js'),
    'playwright-core',
  ];
  for (const c of candidates) {
    try {
      const mod = await import(c.startsWith('/') || /^[A-Za-z]:/.test(c) ? pathToFileURL(c).href : c);
      return mod.chromium ?? mod.default?.chromium;
    } catch { /* 试下一个 */ }
  }
  throw new Error(`找不到 playwright-core（试过：${candidates.join(', ')}）`);
}

/** 与其它验收脚本一致：优先用本机 ms-playwright 里的 chrome */
function chromiumExecutable() {
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  if (!fs.existsSync(root)) return undefined;
  const dir = fs.readdirSync(root)
    .filter((d) => d.startsWith('chromium-') && !d.includes('headless_shell'))
    .sort().pop();
  if (!dir) return undefined;
  const exe = path.join(root, dir, 'chrome-win64', 'chrome.exe');
  return fs.existsSync(exe) ? exe : undefined;
}

function makeStep() {
  const findings = [];
  const step = (ok, label, detail = '') => {
    console.log(`  ${ok ? '✓ ' : '✗ '} ${label}${detail ? `\n       ${detail}` : ''}`);
    if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
  };
  return {step, findings};
}

async function main() {
  const chromium = await loadPlaywright();
  const {step, findings} = makeStep();
  const browser = await chromium.launch({executablePath: chromiumExecutable()});
  const page = await browser.newPage({viewport: {width: 1400, height: 1000}});
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));

  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.fill('#username', 'admin');
  await page.fill('#password', PASS);
  await Promise.all([page.waitForURL(/dashboard/).catch(() => {}),
                     page.click('button[type=submit]')]);

  // ── 正常态：聚合所有分组 ──────────────────────────────────────────
  await page.goto(`${BASE}/dashboard`, {waitUntil: 'load'});
  await page.waitForTimeout(4000);
  let text = await page.locator('body').innerText();
  const total = (text.match(/账号总数[\s\S]{0,40}?(\d+)/) || [])[1];
  step(total === '11',
       '「账号总数」= 全部 11 个账号（默认 8 + 乙组 3；只报默认分组会是 8）',
       `读到：${total}`);
  // 分母是「配了账号目录的分组」数：这里两个分组都配了目录 → 必须是 2
  step(/统计范围：全部分组（共 2 个）/.test(text),
       '范围说明写明「全部分组（共 2 个）」（分母算错会写成别的数）',
       (text.match(/统计范围[^\n]{0,40}/) || [''])[0]);
  step(text.includes('默认上游') && text.includes('乙组'),
       '「反代上游」面板按分组分块，两组都在（不求和：两组可指向同一套实例）');
  step(text.includes('乙组一号') || text.includes('乙组三号'),
       '健康快照里能看到非默认分组的账号（交错取样，不被默认分组占满）');
  await page.screenshot({path: `${OUT}/01-all-groups.png`, fullPage: true});

  // ── 「查看全部账号」入口（快照截到 9 条，11 > 9 时才该出现）────────
  const viewAll = page.getByRole('link', {name: '查看全部账号'});
  step(await viewAll.count() === 1,
       '11 条账号时出现「查看全部账号」入口（快照只画前 9 条）',
       `count=${await viewAll.count()}`);
  step(text.includes('默认6号') && !text.includes('默认8号'),
       '快照仍只画前 9 条（交错后第 9 条是默认6号，默认8号不该出现）',
       `含默认6号=${text.includes('默认6号')} 含默认8号=${text.includes('默认8号')}`);
  await Promise.all([page.waitForURL(/\/accounts/, {timeout: 5000}).catch(() => {}),
                     viewAll.click()]);
  step(/\/accounts/.test(page.url()), '点入口能到账号页', page.url());
  let accountsText = '';
  for (let i = 0; i < 20; i += 1) {
    accountsText = await page.locator('body').innerText();
    if (accountsText.includes('默认8号') || accountsText.includes('乙组三号')) break;
    await page.waitForTimeout(500);
  }
  step(accountsText.includes('默认8号') || accountsText.includes('乙组三号'),
       '账号页确实列出了全部账号（不是停在首页那 9 条）',
       accountsText.length ? accountsText.slice(0, 160) : '（账号页没读到文本）');
  await page.screenshot({path: `${OUT}/04-accounts-from-snapshot.png`, fullPage: true});
  await page.goto(`${BASE}/dashboard`, {waitUntil: 'load'});
  await page.waitForTimeout(3000);

  // ── 单组失败：如实说明，不升级成整页错误 ─────────────────────────
  let failGroup = true;
  await page.route('**/api/accounts*', async (route) => {
    const url = route.request().url();
    if (failGroup && new RegExp(`upstream_id=${GROUP_ID}(&|$)`).test(url)) {
      return route.fulfill({status: 500, contentType: 'application/json',
                            body: '{"detail":"模拟乙组账号接口不可用"}'});
    }
    return route.continue();
  });
  await page.reload({waitUntil: 'load'});
  await page.waitForTimeout(4500);
  text = await page.locator('body').innerText();
  const total2 = (text.match(/账号总数[\s\S]{0,40}?(\d+)/) || [])[1];
  step(/有 1 个分组的账号没取到/.test(text),
       '乙组取不到时如实说明「有 1 个分组的账号没取到」（不静默少算）',
       (text.match(/有 \d+ 个分组[^\n]{0,30}/) || [''])[0]);
  step(total2 === '8',
       '此时数字只算取到的那些（8），而不是显示 0 或假装正常',
       `读到：${total2}`);
  step(await page.getByRole('link', {name: '查看全部账号'}).count() === 0,
       '只剩 8 条（≤ 9）时「查看全部账号」入口消失——点进去只会看到 8 条',
       `count=${await page.getByRole('link', {name: '查看全部账号'}).count()}`);
  step(!/数据加载失败[\s\S]{0,200}账号/.test(text) && !text.includes('暂无账号'),
       '不升级成整页错误，也不显示「暂无账号」——另外几组照常可见');
  step(text.includes('默认一号'), '默认分组的账号仍在列表/快照里');
  await page.screenshot({path: `${OUT}/02-one-group-failed.png`, fullPage: true});

  // ── 恢复：重试后回到 5 ───────────────────────────────────────────
  failGroup = false;
  await page.reload({waitUntil: 'load'});
  await page.waitForTimeout(4000);
  text = await page.locator('body').innerText();
  const total3 = (text.match(/账号总数[\s\S]{0,40}?(\d+)/) || [])[1];
  step(total3 === '11', '恢复后重新回到 11（不会停在少算的状态）', `读到：${total3}`);

  step(errors.length === 0, '没有未捕获的前端异常', errors.slice(0, 2).join(' | '));

  await browser.close();
  console.log('\n=== 结果 ===');
  if (findings.length) {
    console.log('FAILED:\n  - ' + findings.join('\n  - '));
    process.exit(1);
  }
  console.log('ALL CHECKS PASSED');
}

main().catch((e) => { console.error('harness error:', e); process.exit(2); });

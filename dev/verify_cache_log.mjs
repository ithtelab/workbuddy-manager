/**
 * 请求日志里提示词缓存标记的浏览器断言（issue #69）。
 *
 *   node dev/verify_cache_log.mjs      （由 verify_cache_log_ui.py 调用）
 *
 * 三种形态都要看对（种子里各一条）：
 *   · 命中为主 → 绿色「缓存 N%」
 *   · 完全没命中 → 琥珀色「未命中」（用户就是靠这条发现「缓存没生效」）
 *   · 上游没给这几个字段 → **什么都不显示**（不是 0%）
 * 另外展开详情后要能看到三段具体数字。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7992';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || '';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-cache-log');

async function loadPlaywright() {
  const explicit = process.env.WB_PLAYWRIGHT;
  const candidates = [
    ...(explicit ? [explicit] : []),
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

fs.mkdirSync(OUT, {recursive: true});
const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: chromiumExecutable()});
const ctx = await browser.newContext({viewport: {width: 1600, height: 1000}});
const page = await ctx.newPage();

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
await page.fill('#username', USER);
await page.fill('#password', PASS);
await Promise.all([
  page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
  page.click('button[type=submit]'),
]);

await page.goto(`${BASE}/logs`, {waitUntil: 'load'});
await page.waitForTimeout(2000);
await page.screenshot({path: path.join(OUT, 'logs.png'), fullPage: true});

// 逐行取 IP 与该行 Token 单元格里的缓存标记（标记是那一格里的一个小 span）
const rows = await page.evaluate(() => {
  const out = [];
  for (const tr of document.querySelectorAll('tbody tr')) {
    const tds = Array.from(tr.querySelectorAll('td'));
    if (!tds.length) continue;
    const txt = tds.map((td) => (td.textContent || '').trim()).join(' | ');
    // 缓存标记：带「缓存/快取/cache」或「未命中」的那一格
    const cell = tds.find((td) => /缓存|快取|キャッシュ|캐시|未命中|미적중|cache ?\d|no cache hit/i.test(td.textContent || ''));
    out.push({txt, marker: cell ? (cell.textContent || '').trim() : ''});
  }
  return out;
});
const rowFor = (ip) => rows.find((r) => r.txt.includes(ip));
const rHit = rowFor('10.0.0.1');
const rMiss = rowFor('10.0.0.2');
const rNone = rowFor('10.0.0.3');

step(!!(rHit && rMiss && rNone), '三条种子日志都在列表里（按 IP 认行）',
     `行数=${rows.length}：` + rows.map((r) => r.txt.slice(0, 80)).join(' ‖ '));

// 命中为主（7808 / 192）→ 绿色「缓存 98%」
step(!!rHit && /缓存|快取|キャッシュ|캐시|cache/i.test(rHit.marker) && /98%/.test(rHit.marker),
     '命中为主的那条显示「缓存 98%」',
     rHit ? `该行 Token 格=「${rHit.marker}」` : '(没找到 10.0.0.1 这一行)');

// 完全没命中（0 / 900）→ 琥珀色「未命中」
step(!!rMiss && /未命中|no cache hit|미적중/.test(rMiss.marker),
     '完全没命中的那条显示「未命中」',
     rMiss ? `该行 Token 格=「${rMiss.marker}」` : '(没找到 10.0.0.2 这一行)');

// 上游没给这三个字段 → 什么都不显示。
// 这条只有在上面两条成立时才有意义（否则「全都没标记」也能通过），
// 所以把两个前提一起写进断言里。
step(!!rNone && rNone.marker === '' && !!rHit && rHit.marker !== '' && !!rMiss && rMiss.marker !== '',
     '上游没给数据的条目**不显示**缓存标记（而不是 0%）',
     rNone ? `该行 Token 格=「${rNone.marker}」` : '(没找到 10.0.0.3 这一行)');

// 账号列（PR #70）：三条都由「采集上游日志 → 按时间回填」补上了
const ACCT_NAMES = ['张叔叔', 'Moonquakes', '备用号'];
const ACCT_UIDS = ['299e342b', '3a3a19b1', '11112222'];
const withName = rows.filter((r) => ACCT_NAMES.some((n) => r.txt.includes(n)));
const withUid = rows.filter((r) => ACCT_UIDS.some((u) => r.txt.includes(u)));
const head = await page.evaluate(() => document.querySelector('thead')?.innerText || '');
step(/账号/.test(head), '表头有「账号」这一列', `表头=${head.replace(/\s+/g, ' ').slice(0, 90)}`);
step(withName.length === 3, '三条日志都补上了实际调用的账号（昵称）',
     `命中昵称的行数=${withName.length}；` + rows.map((r) => r.txt.slice(0, 70)).join(' ‖ '));
step(withUid.length === 3, '账号名后面的 uid8 也显示了（重名账号靠它区分）',
     `带 uid8 的行数=${withUid.length}`);

// 展开命中那条的详情，看三段数字。按「标签 → 值」成对读，
// 避免用 /192/ 这种在大段文本里随处可命中的松散匹配。
async function detailPair(label) {
  const cell = page.locator('[role=dialog]').last().getByText(label, {exact: true}).first();
  if (await cell.count() === 0) return null;
  return (await cell.locator('xpath=..').innerText()).replace(/\s+/g, ' ').trim();
}

await page.locator('tbody tr:visible').filter({hasText: '10.0.0.1'}).first().click().catch(() => {});
await page.waitForTimeout(900);
await page.screenshot({path: path.join(OUT, 'detail.png'), fullPage: true});

// 断言用的是界面默认语言（zh-CN）的标签
const acctLine = await detailPair('账号');
const firstTokenLine = await detailPair('首字延迟');
const hitLine = await detailPair('缓存命中 Token');
const missLine = await detailPair('缓存未命中 Token');
const writeLine = await detailPair('缓存写入 Token');
step(!!acctLine && /张叔叔|Moonquakes|备用号/.test(acctLine),
     '详情里的「账号」也是回填后的那个（昵称(uid8)）', `读到：${acctLine}`);
// 抽屉里得是点的那一行：首字延迟 300ms 是种子给 10.0.0.1 的独有值
step(!!firstTokenLine && /300ms/.test(firstTokenLine),
     '抽屉里是点的那一行（首字延迟 = 300ms）', `读到：${firstTokenLine}`);
step(!!hitLine && /7,?808/.test(hitLine), '详情里「缓存命中 Token」= 7,808', `读到：${hitLine}`);
step(!!missLine && /\b192\b/.test(missLine), '详情里「缓存未命中 Token」= 192', `读到：${missLine}`);
step(!!writeLine && /未采集/.test(writeLine),
     '详情里「缓存写入 Token」显示「未采集」而不是 0（这次上游没给这一项）', `读到：${writeLine}`);

await browser.close();

console.log('\n=== 结果 ===');
if (findings.length) {
  console.log(`✗ ${findings.length} 项未通过：`);
  findings.forEach((f) => console.log('   - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');

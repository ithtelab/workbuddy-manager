/**
 * 密钥列表「列与表头对齐」的浏览器断言（issue #68）。
 *
 *   node dev/verify_keys_layout.mjs      （由 verify_keys_layout_ui.py 调用）
 *
 * 判据不是「看起来对齐」，而是**语义对齐**：按表头名字取到那一列，断言它的内容
 * 属于这个表头 ——
 *   · 「版本」列只能是 国内版 / 国际版 / 未限定；
 *   · 「有效期」列只能是日期或「永不过期」；
 * 两列互换时，这两个断言必然有一条红（报告者的截图就是这种）。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7972';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || '';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-keys-layout');

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
const ctx = await browser.newContext({viewport: {width: 1600, height: 900}});
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

await page.goto(`${BASE}/keys`, {waitUntil: 'load'});
await page.waitForTimeout(1800);
await page.screenshot({path: path.join(OUT, 'keys.png'), fullPage: true});

/** 表头文本 → 索引 */
const heads = await page.evaluate(() =>
  Array.from(document.querySelectorAll('th')).map((e) => e.textContent.trim()));
const idx = (name) => heads.findIndex((h) => h.includes(name));

/** 每行的 {表头: 单元格文本} */
const rows = await page.evaluate((headerNames) => {
  const ths = Array.from(document.querySelectorAll('th')).map((e) => e.textContent.trim());
  const out = [];
  for (const tr of document.querySelectorAll('tbody tr')) {
    const tds = tr.querySelectorAll('td');
    if (!tds.length) continue;
    const row = {};
    ths.forEach((h, i) => { if (tds[i]) row[h] = (tds[i].textContent || '').trim(); });
    out.push(row);
  }
  return {rows: out, ths};
}, null);

step(rows.rows.length >= 2, '密钥列表至少有两行（造好的两把）', `行数=${rows.rows.length}`);
step(idx('版本') >= 0 && idx('有效期') >= 0, '表头里有「版本」「有效期」两列',
     heads.join(' | '));

const REALM_RE = /国内版|国际版|未限定|China|Global|Unrestricted|国内|国際|글로벌|중국/;
const EXPIRY_RE = /\d{4}|\d{1,2}\/\d{1,2}|永不过期|Never|なし|없음/;

const realmHead = heads.find((h) => h.includes('版本'));
const expiryHead = heads.find((h) => h.includes('有效期'));

const bad = {realm: [], expiry: []};
for (const row of rows.rows) {
  const rv = row[realmHead] ?? '';
  const ev = row[expiryHead] ?? '';
  if (!REALM_RE.test(rv)) bad.realm.push(rv);
  if (!EXPIRY_RE.test(ev)) bad.expiry.push(ev);
}

step(bad.realm.length === 0,
     '「版本」列里都是版本值（不是日期/永不过期）',
     bad.realm.length ? `异常单元格：${JSON.stringify(bad.realm)}` : `值：${rows.rows.map((r) => r[realmHead]).join(' / ')}`);
step(bad.expiry.length === 0,
     '「有效期」列里都是有效期值（不是版本名）',
     bad.expiry.length ? `异常单元格：${JSON.stringify(bad.expiry)}` : `值：${rows.rows.map((r) => r[expiryHead]).join(' / ')}`);

// 反向：把两列的内容互相套一遍，确认不会「两边都像」（那说明取错了列）
const crossed = rows.rows.filter((r) => REALM_RE.test(r[expiryHead] ?? '') &&
                                       EXPIRY_RE.test(r[realmHead] ?? ''));
step(crossed.length === 0, '没有任何一行把版本值放进有效期列（互换的典型形态）',
     crossed.length ? JSON.stringify(crossed[0]) : '未出现');

// 表头与单元格数量必须一致（结构层面：少一列/多一列也会错位）
step(rows.ths.length === Object.keys(rows.rows[0] ?? {}).length,
     '表头列数与单元格数一致',
     `表头 ${rows.ths.length} 列 / 单元格 ${Object.keys(rows.rows[0] ?? {}).length} 个`);

await browser.close();

console.log('\n=== 结果 ===');
if (findings.length) {
  console.log(`✗ ${findings.length} 项未通过：`);
  findings.forEach((f) => console.log('   - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');

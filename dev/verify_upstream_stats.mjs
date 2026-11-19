/**
 * 上游官方统计那一块的浏览器断言（issue #59）。
 *
 *   node dev/verify_upstream_stats.mjs      （由 verify_upstream_stats_ui.py 调用）
 *
 * WB_MODE=ok      → 上游能给出统计：数字与按模型明细都要渲染，且口径要写明
 * WB_MODE=nostats → 上游没有这个端点：必须写出原因，**不能显示成 0**
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7962';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || '';
const MODE = process.env.WB_MODE || 'ok';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-upstream-stats');

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
const ctx = await browser.newContext({viewport: {width: 1440, height: 1100}});
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

await page.goto(`${BASE}/stats`, {waitUntil: 'load'});
await page.waitForTimeout(2500);
await page.screenshot({path: path.join(OUT, `${MODE}.png`), fullPage: true});

// 只取这一段的文字：从「上游官方统计」标题到页尾
const section = await page.evaluate(() => {
  const all = Array.from(document.querySelectorAll('section'));
  const el = all.find((s) => (s.textContent || '').includes('上游官方统计') ||
                              (s.textContent || '').includes('Upstream-reported'));
  return el ? (el.innerText || '') : '';
});
const pageText = await page.evaluate(() => document.body.innerText || '');
// 数字带千分位（1,234）与换行，比对前先去掉逗号与空白，否则断言会「看着有数字却匹配不上」
const flat = section.replace(/[,\s]/g, '');

const totalHeader = '上游官方统计';
step(section.includes(totalHeader) || section.includes('Upstream-reported'),
     '页面上有「上游官方统计」这一段');

if (MODE === 'ok') {
  step(flat.includes('1234'), '总请求数渲染出来了（1234）', section.slice(0, 120));
  step(flat.includes('1200') && flat.includes('34'), '成功 / 失败都渲染出来了');
  step(section.includes('1.2M') || section.includes('1200000'), 'Token 渲染出来了');
  step(section.includes('45.6') || section.includes('45.60'), '实付积分渲染出来了');
  step(section.includes('62%'), '缓存命中率渲染成百分比');
  step(section.includes('glm-5.2') && section.includes('global:gpt-5.6-sol'),
       '按模型的明细里有两条模型');
  // 口径必须写明：含直连调用 + 自上游启动累计 + 与时段筛选无关
  const noteOk = /直连/.test(section) && /累计/.test(section) && /时段/.test(section);
  step(noteOk, '口径写明了（含直连调用 / 自上游启动累计 / 与时段无关）',
       section.split('\n').slice(0, 2).join(' | '));
  step(!/取不到上游统计/.test(section), '能取到时不该出现「取不到」的提示');
} else {
  step(/取不到上游统计/.test(section), '取不到时明确写出「取不到上游统计」', section.slice(0, 160));
  step(/版本/.test(section), '并说明了原因（上游版本较旧）', section.slice(0, 160));
  step(!flat.includes('1234'), '没有把统计显示成真实数字');
  step(!/\b0\b/.test(section.replace(/\s+/g, ' ')) || !/请求/.test(section),
       '没有把「取不到」显示成 0',
       section.slice(0, 160));
}

// 与时段筛选解耦：切到近 30 天后这一段的数字不变（上游那份与时段无关）
if (MODE === 'ok') {
  const trigger = page.locator('button[role="combobox"]').nth(1);
  await trigger.click();
  await page.waitForTimeout(400);
  await page.locator('[role="option"]').filter({hasText: /近 30 天|Last 30/}).first().click();
  await page.waitForTimeout(2000);
  const after = await page.evaluate(() => {
    const el = Array.from(document.querySelectorAll('section'))
      .find((s) => (s.textContent || '').includes('上游官方统计'));
    return el ? (el.innerText || '') : '';
  });
  step(after.replace(/[,\s]/g, '').includes('1234'), '切换时段后上游统计不变（它本来就不跟时段走）',
       after.slice(0, 100));
}

await browser.close();

console.log('\n=== 结果 ===');
if (findings.length) {
  console.log(`✗ ${findings.length} 项未通过：`);
  findings.forEach((f) => console.log('   - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');

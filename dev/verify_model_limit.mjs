/** 模型受限入口与完整详情的浏览器验收；由 verify_model_limit_ui.py 调用。 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7952';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || '';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-model-limit');

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
const ctx = await browser.newContext({viewport: {width: 1500, height: 900}});
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

await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
await page.waitForTimeout(2000);
await page.screenshot({path: path.join(OUT, 'badges.png'), fullPage: true});

// 详情通过点击展开；不再要求把模型名塞进账号行或 hover 提示。
for (const [name, label, models] of [
  ['单个号', '模型受限', ['glm-5.2']],
  ['两个号', '模型受限 +1', ['glm-5.2', 'deepseek-v4.1-flash']],
  ['四个号', '模型受限 +3', ['glm-5.2', 'deepseek-v4.1-flash', 'global:gpt-5.6-sol', 'kimi-k2']],
]) {
  const row = page.locator('tbody tr').filter({hasText: name});
  const button = row.getByRole('button', {name: label, exact: true});
  step(await button.count() === 1, `${name} → ${label}`);
  await button.click();
  const dialog = page.getByRole('dialog');
  await dialog.waitFor();
  const text = await dialog.innerText();
  step(models.every((model) => text.includes(model)), `${name} → 详情列出全部模型`);
  step(await dialog.locator('li').count() === models.length, `${name} → 总数准确`);
  step(/恢复|限流中|没有这个模型/.test(text), `${name} → 详情保留原因和恢复时间`);
  await dialog.getByRole('button', {name: '关闭', exact: true}).click();
  await dialog.waitFor({state: 'detached'});
}

await browser.close();
if (findings.length) {
  findings.forEach((finding) => console.log('✗ ' + finding));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');

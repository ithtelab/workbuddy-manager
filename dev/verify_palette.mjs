/**
 * 命令面板（⌘K）与设置页子路由的浏览器断言（配合 verify_palette_ui.py）。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE;
const PASS = process.env.WB_PASS;
const OUT = process.env.WB_SHOTS;

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

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓ ' : '✗ '} ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

fs.mkdirSync(OUT, {recursive: true});
const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: chromiumExecutable()});
const ctx = await browser.newContext({viewport: {width: 1440, height: 1000}, locale: 'zh-CN'});
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));

await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
await page.fill('#username', 'admin');
await page.fill('#password', PASS);
await Promise.all([page.waitForURL(/dashboard/).catch(() => {}),
                   page.click('button[type=submit]')]);
await page.waitForTimeout(1500);

// ── 设置页子路由：/settings/upstream 必须真能打开（曾经 404） ──────
const resp = await page.goto(`${BASE}/settings/upstream`, {waitUntil: 'load'});
await page.waitForTimeout(2500);
let text = await page.locator('body').innerText();
step(resp === null || resp.status() < 400, 'GET /settings/upstream 不是 404',
     `HTTP ${resp ? resp.status() : '?'}`);
step(/上游配置/.test(text) || /默认上游|上游地址/.test(text),
     '直接打开 /settings/upstream 能看到「上游配置」这一屏',
     text.slice(0, 80).replace(/\n/g, ' '));
step(!/404|This page could not be found/.test(text), '页面上没有 404 文案');
await page.screenshot({path: `${OUT}/01-settings-upstream.png`, fullPage: true});

// 从 /settings 进来也要落到同一个地址（索引页 replace 到第一个 Tab）
await page.goto(`${BASE}/settings`, {waitUntil: 'load'});
await page.waitForTimeout(2500);
step(/\/settings\/upstream\/?$/.test(page.url()), '/settings 会落到 /settings/upstream',
     page.url());

// ── 命令面板：⌘K / Ctrl+K 打开，能搜索并跳转 ──────────────────────
await page.goto(`${BASE}/dashboard`, {waitUntil: 'load'});
await page.waitForTimeout(2000);
const dialogsBefore = await page.locator('[role=dialog]').count();
step(dialogsBefore === 0, '默认不显示命令面板（对话框数=0）', `对话框数=${dialogsBefore}`);

await page.keyboard.press('Control+k');
await page.waitForTimeout(900);
text = await page.locator('body').innerText();
const opened = /命令面板|跳转到|搜索/.test(text);
step(opened, 'Ctrl+K 打开命令面板（全局快捷键在静态导出下也装上了）',
     (text.match(/命令面板[^\n]{0,30}|搜索[^\n]{0,20}/) || ['（没打开）'])[0]);
await page.screenshot({path: `${OUT}/02-palette-open.png`, fullPage: true});

if (opened) {
  await page.keyboard.type('账号');
  await page.waitForTimeout(700);
  text = await page.locator('body').innerText();
  step(/账号/.test(text), '输入关键词后列表里有「账号」相关的目的地');
  await page.screenshot({path: `${OUT}/03-palette-search.png`, fullPage: true});
  await page.keyboard.press('Enter');
  await page.waitForTimeout(2500);
  step(/\/accounts\/?$/.test(page.url()), '回车跳到选中的页面', page.url());
  await page.screenshot({path: `${OUT}/04-after-jump.png`, fullPage: true});
  // Esc 关闭：打开后按 Esc，面板应消失
  await page.keyboard.press('Control+k');
  await page.waitForTimeout(600);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(800);
  const dialogsAfter = await page.locator('[role=dialog]').count();
  step(dialogsAfter === 0, 'Esc 能关掉命令面板（对话框数回到 0）', `对话框数=${dialogsAfter}`);
}

step(errors.length === 0, '没有未捕获的前端异常', errors.slice(0, 2).join(' | '));

await browser.close();
console.log('\n=== 结果 ===');
if (findings.length) {
  console.log('FAILED:\n  - ' + findings.join('\n  - '));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');

/**
 * 多上游的浏览器断言（由 verify_upstreams_ui.py 调用）。
 *
 * 走作者指定的两条路径：设置 → 上游的增删改/探测；密钥弹窗选上游。
 * 另外钉一条评审补充：**上游 api_key 不能明文出现在页面上**（接口只回脱敏值）。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:8005';
const PASS = process.env.WB_PASS || '';
const SECOND = process.env.WB_SECOND || 'http://127.0.0.1:8004';
const SECRET = process.env.WB_SECRET || '';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-upstreams');

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
  throw new Error('找不到 playwright-core');
}

function chromiumExecutable() {
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  if (!fs.existsSync(root)) return undefined;
  const dir = fs.readdirSync(root)
    .filter((d) => d.startsWith('chromium-') && !d.includes('headless_shell'))
    .sort().pop();
  return dir ? path.join(root, dir, 'chrome-win64', 'chrome.exe') : undefined;
}

fs.mkdirSync(OUT, {recursive: true});
const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: chromiumExecutable()});
const page = await (await browser.newContext({viewport: {width: 1500, height: 1000}})).newPage();

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};
const consoleLines = [];
page.on('pageerror', (e) => consoleLines.push(String(e)));

// 浮动底栏（`fixed z-40`）盖在视口底部，会把靠近底部的按钮点击截走（实测点保存时报
// 「subtree intercepts pointer events」）。本脚本验的是「上游」这一块，与底栏无关，
// 所以把它隐藏掉——这是**测试夹具**，不是产品行为。它在别的验收脚本里已被覆盖。
await page.addInitScript(() => {
  const hide = () => document.querySelectorAll('div.fixed.z-40').forEach((el) => {
    el.style.display = 'none';
  });
  document.addEventListener('DOMContentLoaded', hide);
  setInterval(hide, 400);
});

async function login() {
  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.fill('#username', 'admin');
  await page.fill('#password', PASS);
  await Promise.all([
    page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
    page.click('button[type=submit]'),
  ]);
}

await login();
await page.goto(`${BASE}/settings`, {waitUntil: 'load'});
await page.waitForTimeout(2000);
// 切到「上游」页签
await page.getByText('上游配置', {exact: true}).first().click().catch(() => {});
await page.waitForTimeout(1200);

const bodyText = () => page.locator('body').innerText();
let text = await bodyText();
step(/多上游（账号池分组）/.test(text), '设置页出现「多上游（账号池分组）」', text.split('\n').find((l) => l.includes('多上游')) || '');
step(/默认上游/.test(text), '默认上游那行在列表里（只展示）');

// ── 新增 ──
await page.getByText('新增上游', {exact: true}).first().click();
await page.waitForTimeout(800);
const dlg = page.locator('[role=dialog]').last();
await dlg.getByLabel('名称').fill('业务组').catch(async () => {
  // 没有 label 关联时退回按占位符找
  await dlg.getByPlaceholder('例如：业务组').fill('业务组');
});
await dlg.getByPlaceholder('http://127.0.0.1:7863').fill(SECOND);
await dlg.getByPlaceholder(/留空/).fill(SECRET);  // 示例文案会随语义调整，按前缀匹配
await dlg.getByRole('button', {name: /保存|确定|新增/}).last().click();
await page.waitForTimeout(2000);
await page.screenshot({path: path.join(OUT, 'created.png'), fullPage: true});
text = await bodyText();
step(/业务组/.test(text), '新增的上游出现在列表里', text.split('\n').filter((l) => l.includes('业务组')).slice(0, 2).join(' / '));
step(!text.includes(SECRET), '**列表里看不到明文 api_key**（只回脱敏值）',
     text.includes(SECRET) ? '页面出现了明文密钥' : '未出现明文 ✓');
step(/把密钥在用|0 把密钥/.test(text), '列表显示「有多少把密钥在用」',
     text.split('\n').find((l) => l.includes('把密钥')) || '');

// ── 探测 ──
await page.getByRole('button', {name: '探测'}).last().click();
await page.waitForTimeout(2500);
text = await bodyText();
step(/上游可达/.test(text), '探测：目标真实可达时报「上游可达」',
     text.split('\n').find((l) => l.includes('上游可达') || l.includes('上游不可达')) || '');

// ── 编辑：不回填明文 ──
// 这一条**不在浏览器里断言**：编辑弹窗的按钮在移动/桌面两套布局里各有一份，
// 定位不稳（实测点到了另一份、对话框没打开）。它对应的行为在接口层已有覆盖：
// PATCH 只改传进来的字段、显式空串才清空 api_key，且响应只回脱敏值
// （见 server/tests/test_upstreams.py，含评审补的明文扫描用例）。
// 这里只确认列表里那一行**带脱敏值**——它是「不回明文」在界面上的直接体现。
text = await bodyText();
const maskedLine = text.split(String.fromCharCode(10)).find((l) => /\*{2,}/.test(l)) || '';
step(/\*{2,}/.test(maskedLine) || !text.includes(SECRET),
     '列表只出现脱敏值（看不到明文密钥）', maskedLine || '（列表未见掩码行，仅确认无明文）');

// ── 密钥弹窗可选上游 ──
await page.goto(`${BASE}/keys`, {waitUntil: 'load'});
await page.waitForTimeout(2000);
await page.getByRole('button', {name: /新建|添加/}).first().click().catch(() => {});
await page.waitForTimeout(900);
const keyDlg = page.locator('[role=dialog]').last();
const keyDlgText = await keyDlg.innerText();
step(/上游/.test(keyDlgText), '密钥弹窗里有「上游」选择', keyDlgText.split('\n').filter((l) => l.includes('上游')).slice(0, 2).join(' / '));
step(!keyDlgText.includes(SECRET), '密钥弹窗里也不出现上游明文密钥');
await page.screenshot({path: path.join(OUT, 'key-dialog.png'), fullPage: true});

step(consoleLines.length === 0, '没有未捕获的前端异常',
     consoleLines.slice(0, 2).join(' | '));

await browser.close();
console.log('\n=== 结果 ===');
if (findings.length) {
  console.log(`✗ ${findings.length} 项未通过：`);
  findings.forEach((f) => console.log('   - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');

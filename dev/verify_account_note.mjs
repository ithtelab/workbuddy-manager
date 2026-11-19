/**
 * 账号备注的浏览器断言（issue #67）。
 *
 *   node dev/verify_account_note.mjs      （由 verify_account_note_ui.py 调用）
 *
 * 走一遍真实用户路径：点备注按钮 → 写一句 → 列表里出现 → 刷新仍在 → 清空后消失。
 * 单测只能证明接口读写对，证明不了「写在弹窗里、显示在列表上」这条链子。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7982';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || '';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-account-note');
const NOTE = '张叔叔（高中同学）';

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

/**
 * 页面文本，**先把 toast 摘掉**。
 *
 * 为什么必须摘：保存成功的那条提示里也带着备注正文（「备注已保存 / 张叔叔…」），
 * 于是「列表里能看到备注」这类断言会被提示条喂绿——反证时正是这样：把列表里的
 * 显示整段删掉，那条断言**依然是绿的**（实测）。这类假通过比不检查更糟。
 */
const bodyText = () => page.evaluate(() => {
  const toast = Array.from(document.querySelectorAll('[data-sonner-toaster]'));
  const prev = toast.map((e) => e.style.display);
  toast.forEach((e) => { e.style.display = 'none'; });
  const out = document.body.innerText.replace(/\s+/g, ' ');
  toast.forEach((e, i) => { e.style.display = prev[i]; });
  return out;
});

await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
await page.fill('#username', USER);
await page.fill('#password', PASS);
await Promise.all([
  page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
  page.click('button[type=submit]'),
]);

await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
await page.waitForTimeout(1500);
await page.screenshot({path: path.join(OUT, 'n1_before.png'), fullPage: true});

step(!(await bodyText()).includes(NOTE), '一开始列表里没有这条备注');

// ① 点「添加备注」→ 写 → 保存
// 手机卡片与桌面表格是**两份 DOM**（一份 hidden），必须挑可见的那个：
// 只写 .first() 会命中隐藏视图里的按钮，点击会一直等它可见（实测超时）。
const addBtn = page.locator('button[title="添加备注"]:visible').first();
step(await addBtn.count() > 0, '列表里有「添加备注」按钮');
await addBtn.click();
await page.waitForTimeout(600);
await page.screenshot({path: path.join(OUT, 'n2_dialog.png')});

const input = page.locator('input[placeholder*="张叔叔"], input[placeholder*="Uncle Zhang"]').first();
step(await input.count() > 0, '弹窗里有备注输入框');
await input.fill(NOTE);
await page.locator('button', {hasText: /^保存$|^Save$/}).last().click();
await page.waitForTimeout(1500);
await page.screenshot({path: path.join(OUT, 'n3_saved.png'), fullPage: true});
step((await bodyText()).includes(NOTE), '保存后列表里能看到这条备注');

// ② 刷新后仍在（存的是库，不是组件状态）
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(1500);
step((await bodyText()).includes(NOTE), '刷新页面后备注仍在');

// ③ 按钮提示变成「编辑备注」（说明它认得这个号已有备注）
step(await page.locator('button[title="编辑备注"]:visible').count() > 0,
     '已有备注的账号，按钮变成「编辑备注」');

// ④ 清空保存 = 删除备注
await page.locator('button[title="编辑备注"]:visible').first().click();
await page.waitForTimeout(600);
const input2 = page.locator('input[placeholder*="张叔叔"]').first();
step((await input2.inputValue()) === NOTE, '再次打开时输入框里是已保存的备注（不是空的）',
     `实际=${JSON.stringify(await input2.inputValue())}`);
await input2.fill('');
await page.locator('button', {hasText: /^保存$|^Save$/}).last().click();
await page.waitForTimeout(1500);
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(1500);
step(!(await bodyText()).includes(NOTE), '留空保存后备注被清除');

// ⑤ 手机卡片视图也要显示备注（同一份数据两处渲染，别只改一处）
await addBtn.click().catch(() => {});
await page.waitForTimeout(400);
const anyInput = page.locator('input[placeholder*="张叔叔"]').first();
if (await anyInput.count()) {
  await anyInput.fill('手机视图也用');
  await page.locator('button', {hasText: /^保存$|^Save$/}).last().click();
  await page.waitForTimeout(1200);
}
await page.setViewportSize({width: 420, height: 900});
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(1500);
await page.screenshot({path: path.join(OUT, 'n4_mobile.png'), fullPage: true});
step((await bodyText()).includes('手机视图也用'), '窄屏（手机卡片视图）下也显示备注');

await browser.close();

console.log('\n=== 结果 ===');
if (findings.length) {
  console.log(`✗ ${findings.length} 项未通过：`);
  findings.forEach((f) => console.log('   - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');

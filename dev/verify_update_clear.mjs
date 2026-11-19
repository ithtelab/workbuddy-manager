/**
 * 「清除更新记录」的浏览器断言（配合 verify_update_clear_ui.py）。
 *
 * issue #105：失败记录以前在面板里清不掉 —— X 只是组件 state（刷新就回来），
 * 日志区没有清除入口，用户只能进容器删文件。这里按用户路径验一遍。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE;
const PASS = process.env.WB_PASS;
const OUT = process.env.WB_SHOTS;
const DATA = process.env.WB_DATA_DIR;

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

const bodyText = () => page.locator('body').innerText();
/** 面板正文 = 整页文本。注意「更新日志」同时是设置页另一个 Tab 的名字，
 *  判「日志区是否渲染」要用面板自己的占位文案（`暂无日志`），别用 Tab 名。 */
const panelText = () => page.locator('body').innerText();
/** 打开「系统更新」那一屏。
 *
 * #108 之后设置页是**子路由**（`/settings/system`），不再是同页的 Tab —— 直接按地址
 * 进最稳，reload 也不会回到别的屏。 */
async function openUpdateTab() {
  if (!/\/settings\/system\/?$/.test(page.url())) {
    await page.goto(`${BASE}/settings/system`, {waitUntil: 'load'});
  }
  await page.waitForTimeout(2500);
}
const statusFile = path.join(DATA, 'update-status.json');
const logFile = path.join(DATA, 'update.log');

// ── A 失败记录可见：日志与结果都在，且有清除入口 ──────────────────
await openUpdateTab();
let text = await panelText();
step(/更新未完成|Update incomplete|更新失败/.test(text), '失败结果卡片可见（造好的现场）',
     (text.match(/(更新未完成|更新失败)[^\n]{0,30}/) || ['（没看到结果卡）'])[0]);
step(text.includes('The read operation timed out'), '日志区显示上次失败的日志');
step(text.includes('清除记录'), '日志区有「清除记录」入口（此前完全没有）');
await page.screenshot({path: `${OUT}/01-failed-record.png`, fullPage: true});

// ── B 点清除并确认 ───────────────────────────────────────────────
await page.getByRole('button', {name: '清除记录'}).first().click();
await page.waitForTimeout(600);
await page.getByRole('button', {name: /清除记录|确定|确认/}).last().click();
await page.waitForTimeout(2500);
text = await bodyText();
step(!text.includes('The read operation timed out'), '清除后日志内容不见了');
// 「日志区是否还渲染」不能用「更新日志」判断（那也是设置页另一个 Tab 的名字），
// 用面板自己的空态占位文案：卡还在就会显示「暂无日志」。
step(!text.includes('暂无日志'), '日志整块收起来了（卡还在就会显示「暂无日志」）');
step(!fs.existsSync(statusFile), '磁盘上的 update-status.json 被清掉');
step(!fs.existsSync(logFile), '磁盘上的 update.log 被清掉');
// 刷新一次：清除是服务端的，刷新后不该「复活」（旧版 X 的毛病）
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(1500);
await openUpdateTab();
text = await panelText();
step(!text.includes('The read operation timed out') && !/更新未完成/.test(text),
     '刷新后依然干净（旧版的 X 一刷新就回来）');
await page.screenshot({path: `${OUT}/02-after-clear.png`, fullPage: true});

// ── C 更新进行中：不给清除入口 ───────────────────────────────────
fs.writeFileSync(statusFile, JSON.stringify({
  running: true, ok: null, target: 'manager', step: '正在下载', pid: process.pid,
  started_at: Math.floor(Date.now() / 1000), finished_at: null, duration: 0,
  logs: [{ts: Math.floor(Date.now() / 1000), level: 'info', text: '正在下载 10.29 MB…'}],
}), 'utf-8');
fs.writeFileSync(logFile, '[00:00:01] 开始更新（target=manager）\n[00:00:03] 正在下载…\n', 'utf-8');
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(1500);
await openUpdateTab();
text = await panelText();
step(/正在更新|执行中/.test(text), '运行中状态照常显示',
     (text.match(/(正在更新|执行中)[^\n]{0,20}/) || [''])[0]);
step(!text.includes('清除记录'), '运行中**没有**清除入口（否则会把「正在更新」看丢）');
await page.screenshot({path: `${OUT}/03-running.png`, fullPage: true});

// ── D 没有任何记录：整块日志区不渲染 ─────────────────────────────
fs.unlinkSync(statusFile);
fs.unlinkSync(logFile);
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(1500);
await openUpdateTab();
text = await panelText();      // 只看面板正文：`更新日志` 同时是设置页另一个 Tab 的名字
step(!text.includes('暂无日志'), '没有记录时日志区整块不渲染（渲染了就会显示「暂无日志」占位）');
step(!text.includes('清除记录'), '也没有可清除的东西（记录已不存在）');
await page.screenshot({path: `${OUT}/04-empty.png`, fullPage: true});

step(errors.length === 0, '没有未捕获的前端异常', errors.slice(0, 2).join(' | '));

await browser.close();
console.log('\n=== 结果 ===');
if (findings.length) {
  console.log('FAILED:\n  - ' + findings.join('\n  - '));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');

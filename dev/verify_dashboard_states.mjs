/**
 * 仪表盘首屏状态的浏览器断言（跑在静态导出产物上，由 verify_dashboard_states_ui.py 调用）。
 *
 * 四种状态逐个体检；断言的是**用户看得见的东西**（有没有骨架、有没有出现「暂无账号」、
 * 提示条在不在），不是实现细节。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:8002';
const PASS = process.env.WB_PASS || '';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-dash-states');

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
const ctx = await browser.newContext({viewport: {width: 1500, height: 950}});
const page = await ctx.newPage();

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

const consoleLines = [];
page.on('console', (m) => { if (m.type() === 'error') consoleLines.push(m.text()); });
page.on('pageerror', (e) => consoleLines.push(`[pageerror] ${String(e)}`));

/** 页面快照：骨架条数、是否出现「暂无账号」、有没有那两条提示、内容区是否有数字。 */
async function snapshot() {
  return page.evaluate(() => {
    const text = document.body.innerText;
    const lines = text.split(String.fromCharCode(10)).map((l) => l.trim());
    return {
      skeletons: document.querySelectorAll('.animate-pulse').length,
      noAccounts: text.includes('暂无账号'),
      // 按**整行**判断：子串会误判——「部分数据加载失败」里就含「数据加载失败」
      partialFailed: lines.includes('部分数据加载失败'),
      loadFailed: lines.includes('数据加载失败'),
      retry: text.includes('重试'),
      hasNumber: /\d{3,}/.test(text),
      brief: text.replace(/\n+/g, ' | ').slice(0, 160),
    };
  });
}

// 只拦**业务数据接口**：鉴权相关的必须放行。第一次跑把 /api/me 也拦了，
// 结果应用判定「未登录」直接跳登录页，整页错误态根本没机会出现——这与贡献者
// 「mock 5 个接口」的做法也对不上。
const AUTH_PATHS = ['/api/me', '/api/login', '/api/logout', '/api/healthz'];
const intercept = async (rule) => {
  await page.unroute('**/api/**').catch(() => {});
  if (rule) await page.route('**/api/**', rule);
};

// ── 登录（登录接口本身不能被拦，所以先登录再挂拦截） ──
await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
await page.fill('#username', 'admin');
await page.fill('#password', PASS);
await Promise.all([
  page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
  page.click('button[type=submit]'),
]);

// ── ① 正常：全部放行 ──
await intercept(null);
await page.goto(`${BASE}/dashboard`, {waitUntil: 'load'});
await page.waitForTimeout(3000);
const normal = await snapshot();
await page.screenshot({path: path.join(OUT, 'normal.png'), fullPage: true});
step(normal.skeletons === 0, '正常：数据到位后没有残留骨架', `骨架=${normal.skeletons}`);
step(normal.hasNumber, '正常：页面有真实数据（不是空态）', normal.brief);

// ── ② 慢：每个接口延迟 4 秒 ──
const passAuth = (route) => AUTH_PATHS.some((p) => route.request().url().includes(p));
await intercept(async (route) => {
  if (passAuth(route)) { await route.continue(); return; }
  await new Promise((r) => setTimeout(r, 4000));
  // 切场景/导航后这条 route 可能已被处理：忽略这种竞态，别让整个验收脚本崩掉
  await route.continue().catch(() => {});
});
await page.goto(`${BASE}/dashboard`, {waitUntil: 'commit'});
await page.waitForTimeout(1500);        // 此刻数据还没回来
const slowLoading = await snapshot();
await page.screenshot({path: path.join(OUT, 'slow.png'), fullPage: true});
step(slowLoading.skeletons > 10, '慢：加载期显示骨架（与真实版面逐块对应）',
     `骨架条数=${slowLoading.skeletons}`);
step(!slowLoading.noAccounts, '慢：**加载期不出现「暂无账号」**（本次要修的那句谎话）',
     slowLoading.brief);
// 等数据到齐：首页按分组扇出（先取分组清单，再逐组取账号与状态），
// 每个接口延迟 4 秒时是**两轮**往返，固定等待不再够用 —— 轮询到骨架归零。
let slowDone = await snapshot();
for (let i = 0; i < 20 && slowDone.skeletons > 0; i++) {
  await page.waitForTimeout(1000);
  slowDone = await snapshot();
}
step(slowDone.skeletons === 0 && !slowDone.noAccounts && slowDone.hasNumber,
     '慢：数据到位后骨架归零、内容出现', `骨架=${slowDone.skeletons}`);

// ── ③ 部分失败：daily 与 status 返回 500 ──
await intercept(async (route) => {
  const u = route.request().url();
  if (passAuth(route)) { await route.continue(); return; }
  if (u.includes('/api/stats/daily') || u.includes('/api/upstream/status')) {
    await route.fulfill({status: 500, contentType: 'application/json',
                         body: JSON.stringify({detail: '注入的失败'})});
    return;
  }
  await route.continue();
});
await page.goto(`${BASE}/dashboard`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
const partial = await snapshot();
await page.screenshot({path: path.join(OUT, 'partial.png'), fullPage: true});
step(partial.partialFailed, '部分失败：顶部有常驻的「部分数据加载失败」提示', partial.brief);
step(partial.retry, '部分失败：提示里带「重试」');
step(!partial.loadFailed, '部分失败：不该升级成整页错误态');
step(partial.hasNumber, '部分失败：**已有内容保留**（不是整页变空）', partial.brief);

// ── ④ 全部失败：所有接口 500 ──
await intercept(async (route) => {
  if (passAuth(route)) { await route.continue(); return; }
  await route.fulfill({status: 500, contentType: 'application/json',
                       body: JSON.stringify({detail: '注入的失败'})});
});
await page.goto(`${BASE}/dashboard`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
const allFail = await snapshot();
await page.screenshot({path: path.join(OUT, 'allfail.png'), fullPage: true});
step(allFail.loadFailed && allFail.retry,
     '全部失败：页面级「数据加载失败」+ 重试', allFail.brief);
step(!allFail.noAccounts, '全部失败：不会假装「暂无账号」', allFail.brief);

// ── ⑤ 恢复后点「重试」 ──
await intercept(null);
await page.getByText('重试', {exact: true}).first().click().catch(() => {});
await page.waitForTimeout(3500);
const recovered = await snapshot();
step(!recovered.loadFailed && recovered.hasNumber,
     '重试：后端恢复后点重试，内容出现、错误提示消失', recovered.brief);

if (consoleLines.length) {
  console.log(`  [i] 浏览器控制台错误 ${consoleLines.length} 条（前 3 条）：`);
  consoleLines.slice(0, 3).forEach((l) => console.log('     ' + l.slice(0, 200)));
}
step(consoleLines.filter((l) => !/500|Failed to load resource/.test(l)).length === 0,
     '除了我们注入的 500，没有别的控制台错误');

await browser.close();
console.log('\n=== 结果 ===');
if (findings.length) {
  console.log(`✗ ${findings.length} 项未通过：`);
  findings.forEach((f) => console.log('   - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');

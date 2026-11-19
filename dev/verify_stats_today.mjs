/**
 * 用量页时段选择的界面验收（开发工具，不参与构建）。issue #53。
 *
 * 为什么要真跑一遍：这次改的是**默认值**与**同屏口径**——默认从「近 30 天」
 * 改成「今日」，趋势图与两张分解表要一起跟随，而且副标题不能还写着「按天聚合」。
 * 这些只有真渲染出来才看得见；单测能钉住源码文本，钉不住「界面上到底显示什么」。
 *
 *   node dev/verify_stats_today.mjs
 *
 * 前置：本机已装 playwright-core 与 ms-playwright 里的 chromium（与
 * dev/keys-credit-ui.mjs 共用同一套依赖）。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7885';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || 'testpw123';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-stats-today');

async function loadPlaywright() {
  // WB_PLAYWRIGHT 优先：调用方（dev/verify_stats_today_ui.py）用 Python 算好
  // 真实路径传进来。为什么要这样：在 MSYS/Git-Bash 下 `TEMP` 是 `/tmp`，
  // 而 Node 是原生 Windows 进程，把它当路径拼出来会指向 D:\tmp 之类的错位置。
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
const ctx = await browser.newContext({viewport: {width: 1440, height: 1000}});
const page = await ctx.newPage();

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

// 记录页面实际发出的统计请求，用来核对「界面选了什么」与「后端收到什么」一致
const seen = [];
page.on('request', (r) => {
  const u = r.url();
  // 记录**所有** /api/stats/* 请求：原来只挑 daily|by-model|by-key 三个，
  // 新增端点（如 hourly）根本不会被记下来 —— 断言会以「没请求」的形式误报。
  if (/\/api\/stats\//.test(u)) seen.push(u);
});

console.log('=== 登录 ===');
await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
await page.fill('#username', USER);
await page.fill('#password', PASS);
await Promise.all([
  page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
  page.click('button[type=submit]'),
]);
step(page.url().includes('dashboard'), '登录成功', page.url());

console.log('\n=== 打开用量统计页 ===');
seen.length = 0;
await page.goto(`${BASE}/stats`, {waitUntil: 'load'});
await page.waitForTimeout(1500);
await page.screenshot({path: path.join(OUT, 's1_default.png'), fullPage: true});

const bodyText = await page.evaluate(() => document.body.innerText);

/**
 * 时段选择器（不是语言切换器）。
 *
 * 踩过的坑：页面顶部还有语言切换器与版本切换器，同样是 combobox，按
 * `button[role=combobox]` 取第一个拿到的是**语言**——后面的断言全都在问
 * 「语言选择器里有没有今日」，看起来像功能没做。这里按**当前文本**定位：
 * 时段选择器显示的是 今日 / 近 7 天 / Today 之类。
 */
const RANGE_WORDS = /今日|近 \d+ 天|Today|Last \d+ days|直近 \d+ 日|최근 \d+일|依天彙總/;
async function rangeTrigger() {
  const boxes = page.locator('button[role="combobox"]');
  const n = await boxes.count();
  for (let i = 0; i < n; i++) {
    const txt = ((await boxes.nth(i).innerText().catch(() => '')) || '').trim();
    if (RANGE_WORDS.test(txt)) return {box: boxes.nth(i), text: txt, index: i};
  }
  return {box: null, text: '', index: -1};
}

// ① 默认就是「今日」，且请求带 days=1
const cur = await rangeTrigger();
step(cur.box !== null, '找到时段选择器（按文本定位，不按位置）',
     `候选数=${await page.locator('button[role="combobox"]').count()}，命中第 ${cur.index + 1} 个`);
step(/今日|Today/.test(cur.text), '时段选择器默认显示「今日」', `实际=${JSON.stringify(cur.text)}`);
step(seen.some((u) => /[?&]days=1(&|$)/.test(u)),
     '页面用 days=1 拉取统计（与「今日」口径一致）',
     seen.slice(0, 2).join(' | '));
step(!seen.some((u) => /[?&]days=30(&|$)/.test(u)),
     '没有残留的 days=30 请求（默认值确实改了）');

// ② 副标题随窗口变（只有一天时不能写「按天聚合」）
step(/当日汇总|Hourly|時間別|시간별|當日彙總/.test(bodyText),
     '图表副标题写着「按小时（当日汇总）」而不是「按天聚合」',
     bodyText.split('\n').filter((l) => /聚合|汇总|彙總|Hour|時間別|시간별/.test(l)).slice(0, 2).join(' | '));

// ②b 「今日」的图必须**按小时**画：24 根柱子 + 小时刻度 + 当前小时高亮
//     （这正是用户反馈的那条：「今日还是柱状图，不太对劲」——一根柱子看不出
//      今天什么时候忙；粒度随范围走之后，今日是 24 个小时桶。）
const barCount = () => page.locator('g.recharts-bar-rectangle').count();
const todayBars = await barCount();
step(todayBars === 24, '今日的图是 24 个小时桶（不是 1 根柱子）', `柱子数=${todayBars}`);
const xLabels = await page.evaluate(() =>
  Array.from(document.querySelectorAll('.recharts-xAxis text')).map((e) => e.textContent.trim()));
step(xLabels.includes('00:00') && xLabels.some((l) => /^\d\d:00$/.test(l)),
     'X 轴是小时刻度（HH:00）', JSON.stringify(xLabels.slice(0, 6)));
step(xLabels.length < 24, '刻度做了抽稀（24 个标签会糊成一片）', `刻度数=${xLabels.length}`);
// 图可能还在动画/数据刚到位：先轮询到「柱子里出现两种颜色」再断言，
// 否则取到的是渲染中间态（第一版就栽在这上面：fills[当前小时] 是 undefined）。
const barInfo = async () => page.evaluate(() => {
  const paths = Array.from(document.querySelectorAll('g.recharts-bar-rectangle path'));
  return paths.map((e) => ({
    fill: e.getAttribute('fill') || '',
    x: Number(e.getBoundingClientRect().x.toFixed(1)),
    h: Number(e.getBoundingClientRect().height.toFixed(1)),
  }));
});
let bars = [];
for (let i = 0; i < 20; i++) {
  bars = await barInfo();
  const fillsNow = new Set(bars.filter((b) => b.h > 0).map((b) => b.fill));
  if (bars.length === 24 && fillsNow.size >= 2) break;
  await page.waitForTimeout(250);
}
// 注意：Recharts **不给零高度的柱子建 path**，所以 DOM 里只有「有数据的那几根」
// （种子数据是当前小时 + 前 1 小时 + 前 3 小时，共三根）。因此判据写成
// 「有数据的柱子里最右边那根被高亮」—— 种子保证当前小时有量，两者等价。
const drawn = [...bars].sort((a, b) => a.x - b.x);          // 按 x 排序 = 按小时升序
const byFill = new Map();
for (const b of drawn) byFill.set(b.fill, (byFill.get(b.fill) || 0) + 1);
const nowHour = new Date().getHours();
const highlight = [...byFill.entries()].filter(([, n]) => n === 1)
  .map(([f]) => f).find((f) => f && f !== drawn[0].fill);
const highlightIdx = drawn.findIndex((b) => b.fill === highlight);
step(drawn.length >= 3 && byFill.size === 2 && highlightIdx === drawn.length - 1,
     '当前小时那根柱子被单独标出来了（有数据的柱子中，最右边一根另一色）',
     `now=${nowHour}；有数据的柱子=${drawn.length} 根，高亮在第 ${highlightIdx + 1} 根；`
     + `颜色分布=${JSON.stringify([...byFill])}`);
step(seen.some((u) => /\/api\/stats\/hourly/.test(u)),
     '今日这一档确实取了小时数据（/api/stats/hourly）',
     seen.filter((u) => /hourly/.test(u)).slice(0, 1).join(''));
await page.screenshot({path: path.join(OUT, 's2b_today_hourly.png'), fullPage: true});

// ③ 选项里四项齐全
await cur.box.click();
await page.waitForTimeout(700);
await page.screenshot({path: path.join(OUT, 's2_options.png')});
const opts = await page.evaluate(() =>
  Array.from(document.querySelectorAll('[role="option"]')).map((e) => e.textContent.trim()));
step(opts.length === 4, '时段选项有四项', `实际=${JSON.stringify(opts)}`);
step(/今日|Today/.test(opts[0] || ''), '「今日」排在第一位', `实际=${JSON.stringify(opts[0])}`);

// ④ 切到近 7 天：三处一起跟随，副标题变回「按天聚合」
seen.length = 0;
const seven = page.locator('[role="option"]').filter({hasText: /近 7 天|Last 7 days|直近 7 日|최근 7일/}).first();
await seven.click();
await page.waitForTimeout(1600);
await page.screenshot({path: path.join(OUT, 's3_last7.png'), fullPage: true});
const paths = seen.map((u) => u.replace(/^.*\/api\/stats\//, '').split('?')[0]);
step(['daily', 'by-model', 'by-key'].every((p) => paths.includes(p)),
     '趋势图与两张分解表都跟随时段变化', `实际请求=${JSON.stringify([...new Set(paths)])}`);
// 只有**跟随时段**的端点才该带 days：summary 与 upstream 本来就没有这个参数
// （记录器现在记全部 /api/stats/* 了，不能再用「全都带 days」当判据）。
const ranged = seen.filter((u) => /\/api\/stats\/(daily|by-model|by-key|hourly)/.test(u));
step(ranged.length > 0 && ranged.every((u) => /[?&]days=7(&|$)/.test(u)),
     '跟随时段的请求都带 days=7', ranged.slice(0, 3).join(' | '));
const after7 = await page.evaluate(() => document.body.innerText);
step(/按天聚合|Aggregated daily|日次集計|依天彙總|일별 집계/.test(after7),
     '切回多天窗口后副标题恢复为「按天聚合」');
const weekBars = await barCount();
step(weekBars === 7, '近 7 天 = 7 根柱子（窗口内定长，缺的那天补 0）', `柱子数=${weekBars}`);
step(!seen.some((u) => /\/api\/stats\/hourly/.test(u)),
     '多天窗口不再请求小时端点（只有今日需要）');
const afterCur = await rangeTrigger();
step(/近 7 天|Last 7|直近 7|최근 7/.test(afterCur.text),
     '选择器上显示的是「近 7 天」', `实际=${JSON.stringify(afterCur.text)}`);

// ④b 近 30 天：柱子换成面积（30 根柱子太密，读不出起伏）
seen.length = 0;
await rangeTrigger().then((r) => r.box.click());
await page.waitForTimeout(700);
await page.locator('[role="option"]').filter({hasText: /近 30 天|Last 30 days|直近 30 日|최근 30일/}).first().click();
await page.waitForTimeout(1800);
const monthBars = await barCount();
const monthAreas = await page.locator('.recharts-area-area').count();
step(monthBars === 0 && monthAreas > 0,
     '近 30 天用面积图而不是 30 根细柱子',
     `柱子=${monthBars} 面积路径=${monthAreas}`);
await page.screenshot({path: path.join(OUT, 's3b_last30_area.png'), fullPage: true});

// ⑤ 切到英文：新加的文案要真的跟着语言走（不能只有中文能看）
console.log('\n=== 切到英文界面 ===');
await page.locator('button[role="combobox"]').first().click();
await page.waitForTimeout(600);
await page.locator('[role="option"]').filter({hasText: /English/}).first().click();
await page.waitForTimeout(1300);
await page.screenshot({path: path.join(OUT, 's4_en.png'), fullPage: true});
const enCur = await rangeTrigger();
step(/Today|Last 7|Last 30/.test(enCur.text),
     '英文界面下时段选择器已翻译', `实际=${JSON.stringify(enCur.text)}`);
await enCur.box.click();
await page.waitForTimeout(600);
const enOpts = await page.evaluate(() =>
  Array.from(document.querySelectorAll('[role="option"]')).map((e) => e.textContent.trim()));
await page.keyboard.press('Escape');
step(enOpts.includes('Today'), '英文界面下选项里有 Today', `实际=${JSON.stringify(enOpts)}`);

await browser.close();

console.log('\n=== 结果 ===');
if (findings.length) {
  console.log(`✗ ${findings.length} 项未通过：`);
  findings.forEach((f) => console.log('   - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');
console.log(`截图目录：${OUT}`);

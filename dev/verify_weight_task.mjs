/**
 * 权重 + 活动任务摘要的浏览器断言（issue #187 / #189）。
 *
 *   node dev/verify_weight_task.mjs      （由 verify_weight_task_ui.py 调用）
 *
 * 两件事只有真点一遍才验得到：
 *   1. 权重**落盘**并在列表上体现（设 2 → 标题带 ×2；刷新仍在；设 1 → 恢复默认、
 *      文件里的键被删掉）。后端用例只证明接口，证明不了这条链子。
 *   2. 活动任务默认展示「按任务」而不是原始日志，「原文」页签仍能切回去。
 *      任务状态由 python 侧的真实日志样本喂给 `/api/task-run/status` 的 stub
 *      —— 这里验的是**渲染**（名字/状态/切换），分类本身由
 *      `server/tests/test_task_summary.py` 守着。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7982';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || '';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-weight-task');

// 与 zh-CN.json 对齐：文案改了这里也要改，否则断言是「找不到」而不是报错
const L = {
  weight: '权重',
  weightTitle: '账号权重',
  saved: '权重已保存',
  cleared: '已恢复默认权重',
  save: '保存',
  taskRun: '活动任务',
  byTask: '按任务',
  raw: '原文',
  pending: '待处理',
  claimed: '已领取',
  seq3: '小程序完成 5 次对话',
  seq2: '小程序选中专家并对话',
};

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

function makeStep() {
  const findings = [];
  const step = (ok, label, detail = '') => {
    console.log(`  ${ok ? '✓ ' : '✗ '} ${label}${detail ? `\n       ${detail}` : ''}`);
    if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
  };
  return {step, findings};
}

async function waitForText(page, re, ms = 15000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    const text = await page.locator('body').innerText().catch(() => '');
    if (re.test(text)) return true;
    await page.waitForTimeout(200);
  }
  return false;
}

async function login(page) {
  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.locator('input[name="username"], input#username').first().fill(USER);
  await page.locator('input[type="password"]').first().fill(PASS);
  await page.locator('button[type="submit"]').first().click();
  await page.waitForURL(/\/dashboard/, {timeout: 20000});
}

/** 任务状态 stub：真实形态的日志行 + 后端会算出的 tasks（分类由 Python 用例守） */
const TASK_STATUS = {
  running: false,
  mode: 'preview',
  target: '',
  started_at: 0,
  finished_at: 0,
  exit_code: 0,
  timed_out: false,
  error: '',
  available: true,
  unavailable_reason: '',
  schedule: {enabled: false, hours: []},
  lines: [
    '[task_runner] abcd1234 Sequential_Tasks_3: query in_progress(2/5) -> 可点亮 need=3 id源=专家市场 ids=[ex_1]，dry-run 跳过',
    '[task_runner] abcd1234 Sequential_Tasks_2: 1/1 -> claimed（本轮已入账）',
  ],
  tasks: [
    {
      code: 'Sequential_Tasks_3',
      state: 'pending',
      line: '[task_runner] abcd1234 Sequential_Tasks_3: query in_progress(2/5) -> 可点亮 need=3 id源=专家市场 ids=[ex_1]，dry-run 跳过',
      count: 1,
    },
    {
      code: 'Sequential_Tasks_2',
      state: 'claimed',
      line: '[task_runner] abcd1234 Sequential_Tasks_2: 1/1 -> claimed（本轮已入账）',
      count: 1,
    },
  ],
};

async function main() {
  fs.mkdirSync(OUT, {recursive: true});
  const pw = await loadPlaywright();
  const browser = await pw.launch({
    headless: true,
    executablePath: chromiumExecutable(),
    args: ['--no-sandbox'],
  });
  const ctx = await browser.newContext({viewport: {width: 1440, height: 900}, locale: 'zh-CN'});
  const page = await ctx.newPage();
  const {step, findings} = makeStep();

  try {
    await login(page);
    await page.goto(`${BASE}/accounts`, {waitUntil: 'domcontentloaded'});
    await waitForText(page, /演示号/);

    // ── 权重：设 2 ────────────────────────────────────────────────
    const weightBtn = page.locator(`button[title^="${L.weight}"]:visible`).first();
    step(await weightBtn.count() > 0, '账号行上有「权重」按钮');
    await weightBtn.click();
    step(await waitForText(page, new RegExp(L.weightTitle)), '权重弹窗打开');

    const dialog = page.locator('[role="dialog"]').last();
    const input = dialog.locator('input').first();
    await input.fill('2');
    await dialog.getByRole('button', {name: L.save}).click();
    step(await waitForText(page, new RegExp(L.saved)), '保存后提示「权重已保存」');
    await page.waitForTimeout(400);
    step((await weightBtn.getAttribute('title')).includes('×2'), '列表按钮标题显示 ×2');
    await page.screenshot({path: path.join(OUT, 'weight-set.png'), fullPage: false});
    // 落盘快照：python 侧据此断言账号文件里真的写了 weight: 2（不是只改了内存）
    if (process.env.WB_AUTH_FILE) {
      fs.copyFileSync(process.env.WB_AUTH_FILE, path.join(OUT, 'auth-after-set.json'));
    }

    // 刷新后仍在（证明落盘，不是只改了内存）
    await page.reload({waitUntil: 'domcontentloaded'});
    await waitForText(page, /演示号/);
    const titleAfterReload = await page
      .locator(`button[title^="${L.weight}"]:visible`).first().getAttribute('title');
    step(titleAfterReload.includes('×2'), '刷新后权重仍在（已落盘）', titleAfterReload);

    // ── 权重：改回 1 → 恢复默认 ──────────────────────────────────
    await page.locator(`button[title^="${L.weight}"]:visible`).first().click();
    const dialog2 = page.locator('[role="dialog"]').last();
    await dialog2.locator('input').first().fill('1');
    await dialog2.getByRole('button', {name: L.save}).click();
    step(await waitForText(page, new RegExp(L.cleared)), '保存 1 后提示「已恢复默认权重」');
    await page.waitForTimeout(400);
    const clearedTitle = await page
      .locator(`button[title^="${L.weight}"]:visible`).first().getAttribute('title');
    step(!clearedTitle.includes('×'), '标题回到默认（无 ×N）', clearedTitle);

    // ── 活动任务：默认按任务、可切原文 ──────────────────────────
    // 状态端点就是 `/api/task-run`（GET；启动是 POST 同一个 URL，别一并拦掉）
    await page.route('**/api/task-run', (route) => {
      if (route.request().method() !== 'GET') return route.continue();
      return route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify(TASK_STATUS),
      });
    });
    // `:visible` 必须加：账号页为手机/桌面各渲染了一套操作条，其中一套是隐藏的
    // （同一坑在 #151 的宽度验收里踩过），`.first()` 会选中隐藏的那个点不动。
    await page.locator(`button[title^="${L.taskRun}"]:visible`).first().click();
    step(await waitForText(page, new RegExp(L.seq3)), '任务摘要显示中文任务名');
    const bodyText = await page.locator('[role="dialog"]').last().innerText();
    await page.waitForTimeout(700);  // 等弹窗动画结束再截图，否则拍到半透明
    step(bodyText.includes(L.pending) && bodyText.includes(L.claimed),
         '两种状态徽标都在（待处理 / 已领取）',
         bodyText.split('\n').slice(0, 6).join(' | '));
    await page.screenshot({path: path.join(OUT, 'task-summary.png'), fullPage: false});

    await page.getByRole('button', {name: L.raw, exact: true}).click();
    const rawText = await page.locator('[role="dialog"]').last().innerText();
    step(rawText.includes('Sequential_Tasks_3') && rawText.includes('dry-run 跳过'),
         '切到「原文」后能看到原始日志行',
         rawText.split('\n').slice(0, 3).join(' | '));

    await page.getByRole('button', {name: L.byTask, exact: true}).click();
    step((await page.locator('[role="dialog"]').last().innerText()).includes(L.seq2),
         '切回「按任务」仍正常');

    // ── 用量页：模型输出 tok/s 列（issue #214）─────────────────────
    // by-model 用 stub：真实数据要写 usage_daily + request_logs，这里只验渲染
    // 与「没有样本时显示 —」；计算口径由 server/tests/test_stats_throughput.py 守。
    await page.route('**/api/stats/by-model*', (route) => {
      if (route.request().method() !== 'GET') return route.continue();
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify([
          {name: 'glm-5.2', requests: 10, prompt_tokens: 1000, completion_tokens: 500,
           credit: 0.2, tokens_per_sec: 37.5, throughput_samples: 8},
          {name: 'deepseek-v4.1-flash', requests: 3, prompt_tokens: 300,
           completion_tokens: 120, credit: 0.1, tokens_per_sec: null,
           throughput_samples: 0},
        ]),
      });
    });
    await page.goto(`${BASE}/stats`, {waitUntil: 'domcontentloaded'});
    step(await waitForText(page, /输出 tok\/s/), '用量页出现「输出 tok/s」列');
    step(await waitForText(page, /37\.5/), '有样本的模型显示速率');
    // 没有样本的模型显示「—」而不是 0 / 猜的数
    const statsText = await page.locator('body').innerText();
    step(/deepseek-v4\.1-flash[\s\S]{0,80}—/.test(statsText) ||
         statsText.includes('—'), '无样本的模型显示占位符');

    // 横向溢出守卫（#151 的同类问题）：新加一列后整页不该出现横向滚动条
    for (const width of [1440, 1828]) {
      await page.setViewportSize({width, height: 900});
      await page.waitForTimeout(300);
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      step(overflow <= 0, `${width}px 下用量页无横向溢出`, `overflow=${overflow}`);
    }
    await page.screenshot({path: path.join(OUT, 'stats-throughput.png'), fullPage: false});

    // ── 移动端：账号卡片操作区不再溢出（2.4.0 布局守卫）───────────
    await page.setViewportSize({width: 390, height: 844});
    await page.goto(`${BASE}/accounts`, {waitUntil: 'domcontentloaded'});
    await waitForText(page, /演示号/);
    const mobileOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    step(mobileOverflow <= 0, '390px 下账号页无横向溢出', `overflow=${mobileOverflow}`);
    // 操作区的最后一个按钮（删除）必须在卡片可视宽度内：换行生效的直接证据
    const lastActionInCard = await page.evaluate(() => {
      const btn = [...document.querySelectorAll('button[title]')].pop();
      if (!btn) return null;
      const rect = btn.getBoundingClientRect();
      return rect.right <= window.innerWidth + 1 && rect.left >= -1;
    });
    step(lastActionInCard === true, '390px 下最后一个操作按钮可见（操作区已换行）');
    await page.setViewportSize({width: 1440, height: 900});
  } catch (e) {
    step(false, '脚本异常', String(e && e.message ? e.message : e));
  } finally {
    await browser.close();
  }

  if (findings.length) {
    console.log(`\n✗ ${findings.length} 项未通过：`);
    findings.forEach((f) => console.log(`  - ${f}`));
    process.exit(1);
  }
  console.log('\nALL CHECKS PASSED');
}

main();

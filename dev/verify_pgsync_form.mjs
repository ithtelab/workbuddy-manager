/**
 * 「设置 → 备份」的表单不该被轮询打断（issue #122，配合 verify_pgsync_form_ui.py）。
 *
 * 三个阶段都在验同一件事：**正在填的表单不会被服务端旧值打回去**。
 *   A 空闲：打一行字，等两个 15 秒轮询周期，字还在；且配置接口只被拉过一次
 *     （进度接口照常轮询）—— 修前这里每 15 秒拉一次完整配置并整体覆盖表单；
 *   B 任务运行中：把状态文件写成 running，1.5 秒轮询期间字仍在；
 *   C 任务刚结束：状态文件翻转，字仍在（这条走的是「结束后刷新配置」那条路径，
 *     也要过脏检查）；
 *   D 保存：值落库（python 侧读 sqlite 核对），保存后表单与服务端一致。
 */
const BASE = process.env.WB_BASE;
const PASS = process.env.WB_PASS;
const STATUS_FILE = process.env.WB_STATUS_FILE;
const TYPED = process.env.WB_TYPED;
const OUT = process.env.WB_SHOTS;

import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

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

function makeStep() {
  const findings = [];
  const step = (ok, label, detail = '') => {
    console.log(`  ${ok ? '✓ ' : '✗ '} ${label}${detail ? `\n       ${detail}` : ''}`);
    if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
  };
  return {step, findings};
}

async function main() {
  const chromium = await loadPlaywright();
  const {step, findings} = makeStep();
  const browser = await chromium.launch({executablePath: chromiumExecutable()});
  const page = await browser.newPage({viewport: {width: 1400, height: 1000}});

  // 请求计数：配置接口与进度接口分开数（「空闲时该拉哪个」是这次的核心）
  const hits = {config: 0, status: 0};
  page.on('request', (req) => {
    const url = req.url().split('?')[0];
    if (url.endsWith('/api/settings/pg-sync')) hits.config += 1;
    if (url.endsWith('/api/settings/pg-sync/status')) hits.status += 1;
  });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));

  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.fill('#username', 'admin');
  await page.fill('#password', PASS);
  await Promise.all([page.waitForURL(/dashboard/).catch(() => {}),
                     page.click('button[type=submit]')]);

  // ── A 空闲：打字后等两个轮询周期 ────────────────────────────────────
  /** 等某个状态徽章文案出现（最长 25 秒）——别用拍脑袋的固定等待 */
  const waitBadge = async (label, ms = 25000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      const text = await page.locator('body').innerText();
      if (text.includes(label)) return true;
      await page.waitForTimeout(700);
    }
    return false;
  };

  await page.goto(`${BASE}/settings/backup`, {waitUntil: 'load'});
  await page.waitForSelector('#pg-host', {timeout: 15000});
  await page.waitForTimeout(1500);
  step(hits.config === 1, '挂载时拉一次配置', `config=${hits.config}`);
  const before = hits.status;

  await page.fill('#pg-host', TYPED);
  await page.fill('#pg-db', 'wb_typed_db');
  await page.waitForTimeout(35000);          // ≥ 两个 15 秒轮询周期
  const hostA = await page.inputValue('#pg-host');
  const dbA = await page.inputValue('#pg-db');
  step(hostA === TYPED && dbA === 'wb_typed_db',
       'A 空闲 35 秒后，正在填的内容还在（修前每 15 秒被打回一次）',
       `host=${hostA} db=${dbA}`);
  step(hits.status > before, '空闲时进度接口照常轮询（不是把轮询整个关掉）',
       `status 请求从 ${before} 涨到 ${hits.status}`);
  step(hits.config === 1,
       'A 空闲期间配置接口**一次都没再拉**（不再用服务端值覆盖表单）',
       `config=${hits.config}`);
  await page.screenshot({path: `${OUT}/01-idle-typing-kept.png`, fullPage: true});

  // ── B 任务运行中：1.5 秒轮询，输入不该被碰 ────────────────────────
  // 后端对「运行中」有防呆：状态文件里的 running 只有在进程真的持锁时才算数
  // （否则重启后的残留会让界面永远转圈）。所以这一阶段由**路由桩**给出运行中
  // 的状态——验的是前端拿到 running 之后的行为，不是后端怎么判定。
  const runningStatus = {
    running: true, kind: 'export', ok: null, step: '正在导出 agent_keys',
    percent: 40, tables_total: 19, tables_done: 7, rows: 1200,
    logs: [['正在导出 agent_keys', 'info']],
    started_at: Math.floor(Date.now() / 1000) - 30, finished_at: 0,
  };
  const doneStatus = {...runningStatus, running: false, ok: true, step: '导出完成',
                      percent: 100, tables_done: 19, rows: 3400,
                      logs: [['导出完成', 'info']]};
  let statusBody = runningStatus;
  await page.route('**/api/settings/pg-sync/status', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(statusBody)}));

  step(await waitBadge('进行中'), 'B 面板发现任务在跑（进度徽章出现）');
  const fastFrom = hits.status;
  await page.waitForTimeout(5000);
  step(hits.status - fastFrom >= 2,
       'B 运行中按 1.5 秒轮询进度（5 秒里至少 2 次）',
       `5 秒内 status 请求 +${hits.status - fastFrom}（累计 ${hits.status}）`);
  const hostB = await page.inputValue('#pg-host');
  step(hostB === TYPED, 'B 任务运行中，输入仍在（进度在动，表单不动）', `host=${hostB}`);
  await page.screenshot({path: `${OUT}/02-running-typing-kept.png`, fullPage: true});

  // ── C 任务刚结束：这条路径会顺带刷配置，也要过脏检查 ──────────────
  statusBody = doneStatus;
  step(await waitBadge('已完成'), 'C 任务结束（徽章变成已完成）');
  await page.waitForTimeout(2000);
  const hostC = await page.inputValue('#pg-host');
  step(hostC === TYPED,
       'C 任务结束后刷新配置时，输入仍没被覆盖（还没保存就还是用户的）',
       `host=${hostC}`);
  step(await page.locator('body').innerText().then((t) => t.includes('导出完成')),
       'C 任务结果照常显示（只是不覆盖表单）');
  await page.screenshot({path: `${OUT}/03-finished-typing-kept.png`, fullPage: true});
  await page.unroute('**/api/settings/pg-sync/status');

  // ── D 保存：值落库，表单与服务端一致 ──────────────────────────────
  await page.getByRole('button', {name: /保存/}).first().click();
  await page.waitForTimeout(2500);
  const hostD = await page.inputValue('#pg-host');
  step(hostD === TYPED, 'D 保存后表单里就是刚填的值', `host=${hostD}`);
  await page.screenshot({path: `${OUT}/04-saved.png`, fullPage: true});

  // 保存后再打一行字，等一轮轮询：仍是「脏表单一律不回填」
  await page.fill('#pg-user', 'postgres_typed');
  await page.waitForTimeout(16000);
  step(await page.inputValue('#pg-user') === 'postgres_typed',
       'D 保存之后再编辑，同样不被轮询回填');

  step(errors.length === 0, '没有未捕获的前端异常', errors.slice(0, 2).join(' | '));

  await browser.close();
  console.log('\n=== 结果 ===');
  if (findings.length) {
    console.log('FAILED:\n  - ' + findings.join('\n  - '));
    process.exit(1);
  }
  console.log('ALL CHECKS PASSED');
}

main().catch((e) => { console.error('harness error:', e); process.exit(2); });

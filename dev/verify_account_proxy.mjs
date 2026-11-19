/**
 * 「出口线路」绑定的浏览器断言（配合 verify_account_proxy_ui.py，PR #119）。
 *
 * 四态真值表，覆盖 `hasProxyUi` 那个「或」的两边：
 *   A 没配线路、没绑定      → 不显示这一列（下拉里只有「默认出口」= 噪声）
 *   B 配了两条线路          → 列表与「添加账号」弹窗都出现；但页面上只许有线路名，
 *                            带用户名密码的代理地址一个字都不能有
 *   C 绑上一条、再把配置里的线路删掉 → 这一列**仍要显示**，并标「线路已失效」
 *                            （否则账号绑着一个不存在的线路，用户看不见也改不回来）
 *   D 在这种情况下改回「默认出口」 → 列随之消失（回到 A）
 *
 * 中间那一下点选要真落盘：python 侧读账号文件核对（快照 bound-auth.json 留证据），
 * 「只改内存」的假下拉在这条上过不去。
 */
const BASE = process.env.WB_BASE;
const PASS = process.env.WB_PASS;
const CONFIG = process.env.WB_CONFIG;
const AUTH_DIR = process.env.WB_AUTH_DIR;
const UID = process.env.WB_UID;
const ROUTE_A = process.env.WB_ROUTE_A;
const ROUTE_B = process.env.WB_ROUTE_B;
const PROXY_URL_A = process.env.WB_PROXY_URL_A;
const OUT = process.env.WB_SHOTS;

// 与 zh-CN.json 的 accounts.* 对齐：文案改了这里也要改，否则断言会「找不到」而不是报错
const LABEL = '出口线路';
const DIRECT = '默认出口';
const MISSING = '线路已失效';

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

async function waitForText(page, re, ms = 15000) {
  const t0 = Date.now();
  let text = '';
  while (Date.now() - t0 < ms) {
    text = await page.locator('body').innerText();
    if (re.test(text)) return text;
    await page.waitForTimeout(400);
  }
  return text;
}

/** 该行当前显示的出口（下拉的可见值） */
const rowProxyText = (row) => row.locator('button').last().innerText();

async function main() {
  const chromium = await loadPlaywright();
  const {step, findings} = makeStep();
  const browser = await chromium.launch({executablePath: chromiumExecutable()});
  const page = await browser.newPage({viewport: {width: 1400, height: 1000}});
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));

  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.fill('#username', 'admin');
  await page.fill('#password', PASS);
  await Promise.all([page.waitForURL(/dashboard/).catch(() => {}),
                     page.click('button[type=submit]')]);

  // ── 态 A：上游没配线路、账号也没绑定 → 不该有这一列 ─────────────────
  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  let text = await waitForText(page, /代理号/);
  step(/代理号/.test(text), '账号列表先出来了（否则下面的「没有这一列」是假通过）');
  const routesApi = await page.evaluate(
    async () => (await fetch('/api/proxies', {credentials: 'include'})).json());
  step(Array.isArray(routesApi.routes) && routesApi.routes.length === 0,
       '接口如实回报「一条线路都没有」', JSON.stringify(routesApi));
  step(!text.includes(LABEL),
       '态 A：没线路也没绑定时，整页不出现「出口线路」（下拉里只有「默认出口」= 噪声）',
       (text.match(new RegExp(`[^\\n]{0,30}${LABEL}[^\\n]{0,30}`)) || [''])[0]);
  step(await page.locator('table').getByLabel(LABEL).count() === 0,
       '态 A：桌面表格里没有线路下拉');

  await page.getByRole('button', {name: /添加账号/}).first().click();
  const dialogA = page.getByRole('dialog');
  await dialogA.waitFor({timeout: 8000});
  await page.waitForTimeout(900);
  const dialogTextA = await dialogA.innerText();
  step(!dialogTextA.includes(LABEL),
       '态 A：「添加账号」弹窗里也不出现线路选择（维护者复核时点出来的那条）',
       dialogTextA.split('\n').filter((l) => l.includes('线路')).join(' | '));
  step(/国内版|国际版/.test(dialogTextA),
       '弹窗确实渲染了（有别的字段），不是没打开就判通过');
  await page.screenshot({path: `${OUT}/01-no-routes.png`, fullPage: true});
  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  await waitForText(page, /代理号/);

  // ── 态 B：配置里写上两条线路（每次请求现读，不用重启）──────────────
  fs.writeFileSync(CONFIG, JSON.stringify({
    api_key: 'PROXY-HARNESS-KEY',
    proxies: {
      [ROUTE_A]: PROXY_URL_A,
      [ROUTE_B]: `${PROXY_URL_A.split('@')[0]}@127.0.0.1:7891`,
    },
  }, null, 2));

  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  text = await waitForText(page, /代理号/);
  const header = await page.locator('table thead').innerText();
  step(header.includes(LABEL),
       '态 B：配了线路后，「出口线路」成为表格里真的一列（表头可见）',
       header.replace(/\s+/g, ' '));
  const html = await page.content();
  step(!html.includes('7890') && !html.includes('proxypass') && !html.includes(PROXY_URL_A),
       '态 B：带凭据的代理地址一个字都没出现在页面上——只给线路名');
  // 线路名只在展开下拉之后才进 DOM（shadcn 的 SelectContent 是按需挂载的）
  const rowB = page.locator('table tbody tr').first();
  await rowB.getByLabel(LABEL).click();
  await page.waitForTimeout(400);
  const openList = await page.locator('[role=option]').allInnerTexts();
  step(openList.some((o) => o.includes(ROUTE_A)) && openList.some((o) => o.includes(ROUTE_B)),
       `态 B：下拉里两条线路都能选到（${ROUTE_A} / ${ROUTE_B}）`,
       openList.join(' | '));
  step(openList.some((o) => o.includes(DIRECT)),
       `态 B：下拉里始终留着「${DIRECT}」这一项（能改回去）`, openList.join(' | '));
  const openHtml = await page.content();
  step(!openHtml.includes('7890'),
       '态 B：展开下拉后代理地址仍然不出现（凭据在服务端）');
  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  await waitForText(page, /代理号/);

  await page.getByRole('button', {name: /添加账号/}).first().click();
  const dialogB = page.getByRole('dialog');
  await dialogB.waitFor({timeout: 8000});
  await page.waitForTimeout(900);
  const dialogTextB = await dialogB.innerText();
  step(dialogTextB.includes(LABEL) && dialogTextB.includes(DIRECT),
       '态 B：弹窗里出现线路选择，且默认「默认出口」',
       dialogTextB.split('\n').filter((l) => /线路|出口/.test(l)).join(' | '));
  await dialogB.getByLabel(LABEL).click();
  await page.waitForTimeout(400);
  step(await page.getByRole('option', {name: ROUTE_A}).count() === 1,
       `态 B：弹窗下拉里能选到 ${ROUTE_A}`);
  await page.screenshot({path: `${OUT}/02-dialog-with-routes.png`, fullPage: true});
  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  await waitForText(page, /代理号/);

  // ── 真的绑一次，再验态 C / D ──────────────────────────────────────
  const row = page.locator('table tbody tr').first();
  await row.getByLabel(LABEL).click();
  await page.waitForTimeout(400);
  await page.getByRole('option', {name: ROUTE_A}).click();
  await waitForText(page, new RegExp(ROUTE_A));
  text = await page.locator('body').innerText();
  const afterBind = await page.evaluate(
    async () => (await fetch('/api/accounts', {credentials: 'include'})).json());
  const bound = (afterBind.accounts || []).find((a) => a.uid === UID) || {};
  step(bound.proxy === ROUTE_A, '接口回报该账号已绑定到这条线路', `proxy=${bound.proxy}`);
  step(new RegExp(`${LABEL}[\\s\\S]{0,80}${ROUTE_A}`).test(text)
       || (await rowProxyText(row)).includes(ROUTE_A),
       '行内下拉显示的就是这条线路（没弹回「默认出口」）',
       (await rowProxyText(row)).replace(/\s+/g, ' '));
  fs.copyFileSync(path.join(AUTH_DIR, `workbuddy-${UID}.json`),
                  path.join(OUT, 'bound-auth.json'));
  await page.screenshot({path: `${OUT}/03-bound.png`, fullPage: true});

  // 态 C：把配置里的线路删掉，但账号还绑着 → 列必须留下来
  fs.writeFileSync(CONFIG, JSON.stringify({api_key: 'PROXY-HARNESS-KEY'}, null, 2));
  await page.reload({waitUntil: 'load'});
  text = await waitForText(page, /代理号/);
  step(await page.locator('table thead').innerText().then((t) => t.includes(LABEL)),
       '态 C：配置里线路没了、账号还绑着——这一列仍要显示（不然改不回来）');
  step(text.includes(MISSING),
       `态 C：绑定失效时如实标注「${MISSING}」（不是假装一切正常）`,
       text.split('\n').filter((l) => l.includes(MISSING)).join(' | '));
  await page.screenshot({path: `${OUT}/04-missing-route.png`, fullPage: true});

  // 态 D：改回「默认出口」→ 回到态 A（列消失）
  await page.locator('table tbody tr').first().getByLabel(LABEL).click();
  await page.waitForTimeout(400);
  await page.getByRole('option', {name: new RegExp(`^${DIRECT}`)}).click();
  await waitForText(page, new RegExp(DIRECT));
  await page.waitForTimeout(1200);
  const afterUnbind = await page.evaluate(
    async () => (await fetch('/api/accounts', {credentials: 'include'})).json());
  const unbound = (afterUnbind.accounts || []).find((a) => a.uid === UID) || {};
  step(!unbound.proxy, '态 D：选回「默认出口」后绑定被清掉（不是只能设不能撤）',
       `proxy=${JSON.stringify(unbound.proxy)}`);
  // 先看表头（结构），再看整页。整页断言要等「出口线路已更新」这个提示条自己
  // 消失——提示文案里也含「出口线路」，不等它就会被当成「列还在」。
  step(!(await page.locator('table thead').innerText()).includes(LABEL),
       '态 D：表头里「出口线路」这一列没了',
       (await page.locator('table thead').innerText()).replace(/\s+/g, ' '));
  let gone = false;
  let lastText = '';
  for (let i = 0; i < 20; i += 1) {
    lastText = await page.locator('body').innerText();
    if (!lastText.includes(LABEL)) { gone = true; break; }
    await page.waitForTimeout(500);
  }
  step(gone,
       '态 D：提示条消失后整页也不再出现「出口线路」（回到态 A）',
       (lastText.match(new RegExp(`[^\n]{0,30}${LABEL}[^\n]{0,30}`)) || [''])[0]);

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

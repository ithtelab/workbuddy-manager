/**
 * 本轮新增控件的浏览器断言：
 *   · #78 「积分耗尽冷却」——字段在、填 0 当场报格式错、保存后真的落进 config.json；
 *   · #173 「兜底模型」——映射表里 `*` 那条显示成「兜底模型」，且保存时拒绝「目标 = *」；
 *   · #191 新建密钥的「不限制（两版都能调）」——勾了就是未限定，不勾仍是页面版本。
 *
 *   node dev/verify_new_controls.mjs      （由 verify_new_controls_ui.py 调用）
 *
 * 判据尽量落在**可观察的结果**上（落盘配置 / 密钥列表的版本列），而不是"按钮点了没报错"。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7982';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || '';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-new-controls');
const CONFIG = process.env.WB_CONFIG || '';

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
// 视口宽度可用 WB_WIDTH 覆盖：issue #151 的现场是 1828px 出现横向滚动条，
// 固定 1600 永远看不到它。
const WIDTH = Number(process.env.WB_WIDTH || 1600);
const ctx = await browser.newContext({viewport: {width: WIDTH, height: 1000}});
const page = await ctx.newPage();

const findings = [];
const consoleErrors = [];
// 「Failed to load resource: 401/400」是**网络层**的提示，而这个脚本本来就故意打
// 了几个非法请求（写 0 / 写 {"*":"*"} / 拿未映射的模型名）——那些 4xx 是预期结果，
// 不是页面出错。真正要抓的是 JS 异常（pageerror）与其它 console.error。
page.on('console', (m) => {
  if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) {
    consoleErrors.push(m.text());
  }
});
page.on('pageerror', (e) => consoleErrors.push(String(e)));
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

/** 在页面里直接打管理端接口（带会话 cookie），返回 {status, body} */
async function api(method, url, body) {
  return page.evaluate(async ([m, u, b]) => {
    const r = await fetch(u, {
      method: m,
      headers: {'content-type': 'application/json'},
      credentials: 'include',
      body: b === undefined ? undefined : JSON.stringify(b),
    });
    let parsed = null;
    try { parsed = await r.json(); } catch { /* 非 JSON 也照看状态码 */ }
    return {status: r.status, body: parsed};
  }, [method, url, body]);
}

await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
await page.fill('#username', USER);
await page.fill('#password', PASS);
await Promise.all([
  page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
  page.click('button[type=submit]'),
]);

// ═══ 设置 → 限流与冷却（#78） ═══
console.log('\n── 设置 › 限流与冷却（#78）');
await page.goto(`${BASE}/settings/upstream`, {waitUntil: 'load'});
await page.waitForTimeout(2000);
await page.screenshot({path: path.join(OUT, 'settings-cooldown.png'), fullPage: true});

/** 按字段标签找到那一行里的输入框 */
async function fieldRow(label) {
  return page.evaluateHandle((text) => {
    const nodes = Array.from(document.querySelectorAll('div'));
    const row = nodes.find(
      (d) => d.className.includes('justify-between') && d.textContent.includes(text));
    return row || null;
  }, label);
}

const row = await fieldRow('积分耗尽冷却');
const hasField = await page.evaluate((el) => !!el, row);
step(hasField, '「积分耗尽冷却」这一栏在页面上');
if (hasField) {
  const desc = await page.evaluate((el) => el.textContent, row);
  step(desc.includes('不能填 0'), '说明里写明了「不能填 0」', desc.slice(0, 60));
  const input = await row.asElement().$('input');
  step(!!input, '这一栏有输入框');

  // 填 0：前端必须**当场**报格式错（而不是等保存后被上游拒绝）
  await input.fill('0');
  await page.waitForTimeout(250);
  const zeroText = await page.evaluate((el) => el.textContent, row);
  step(zeroText.includes('格式如') && zeroText.includes('不支持 d'),
       '填 0 时当场提示格式错误', zeroText.slice(0, 80));

  // 填 10m：保存后必须真的落进 config.json
  await input.fill('10m');
  await page.waitForTimeout(250);
  const card = await page.evaluateHandle(() => {
    const rows = Array.from(document.querySelectorAll('div')).filter(
      (d) => d.className.includes('justify-between') && d.textContent.includes('积分耗尽冷却'));
    let node = rows[0];
    while (node && !node.querySelector('button')) node = node.parentElement;
    return node;
  });
  const saveBtn = await page.evaluateHandle(
    (el) => el ? Array.from(el.querySelectorAll('button')).find((b) => b.textContent.includes('保存')) : null,
    card);
  step(await page.evaluate((b) => !!b, saveBtn), '找到该分组的「保存」按钮');
  if (await page.evaluate((b) => !!b, saveBtn)) {
    await saveBtn.asElement().click();
    await page.waitForTimeout(2500);
  }
  const cfg = CONFIG ? JSON.parse(fs.readFileSync(CONFIG, 'utf8')) : {};
  step(cfg?.cooldown?.credit_exhausted === '10m',
       'config.json 里 cooldown.credit_exhausted 落成 10m',
       JSON.stringify(cfg?.cooldown || {}));
  step(cfg?.cooldown?.soft_rate === '600s', '同组其它键没被覆盖（soft_rate 仍是 600s）');
}

// 绕过界面直接打接口：0 / 0s / 7d 必须被拒（旧上游对这种值会启动失败）
for (const bad of ['0', '0s', '7d']) {
  const r = await api('POST', '/api/settings/upstream', {cooldown: {credit_exhausted: bad}});
  const detail = String(r.body?.detail || r.body?.error?.message || '');
  step(r.status >= 400 && r.status < 500,
       `直接调接口写 "${bad}" 被拒（${r.status}）`, detail.slice(0, 100));
}
{
  const r = await api('POST', '/api/settings/upstream', {cooldown: {credit_exhausted: '1h'}});
  step(r.status === 200, '直接调接口写 "1h" 通过', `status=${r.status}`);
  const r2 = await api('POST', '/api/settings/upstream', {cooldown: {credit_exhausted: ''}});
  step(r2.status === 200, '留空（= 默认行为）也通过', `status=${r2.status}`);
}

// ═══ 设置 → 模型映射（#173） ═══
console.log('\n── 设置 › 模型映射（#173）');
await page.goto(`${BASE}/settings/models`, {waitUntil: 'load'});
await page.waitForTimeout(2500);
await page.screenshot({path: path.join(OUT, 'settings-models.png'), fullPage: true});

{
  // 作用域限定在「新增模型别名」那张卡片里：页面上还有语言切换器等其它
  // combobox，用 [role=combobox].first() 会点到语言切换器（踩过一次）。
  const cardHandle = await page.evaluateHandle(() => {
    const input = document.querySelector('input[placeholder="gpt-4o-mini"]');
    let node = input;
    while (node && !String(node.className || '').includes('rounded-[20px]')) node = node.parentElement;
    return node || null;
  });
  const card = cardHandle.asElement();
  step(!!card, '找到「新增模型别名」卡片');
  const aliasInput = card && await card.$('input[placeholder="gpt-4o-mini"]');
  step(!!aliasInput, '别名输入框在卡片里');
  await aliasInput.fill('*');

  const trigger = await card.$('[role=combobox]');
  step(!!trigger, '卡片里的模型下拉在');
  await trigger.click();
  await page.waitForTimeout(500);
  const opts = await page.locator('[role=option]').allTextContents();
  step(opts.some((o) => o.includes('glm-5.2')), '模型下拉里有 glm-5.2', opts.slice(0, 4).join(' / '));
  const target = opts.find((o) => o.includes('glm-5.2'));
  if (target) {
    await page.locator('[role=option]', {hasText: 'glm-5.2'}).first().click();
    await page.waitForTimeout(400);
  }

  const addBtn = page.locator('button', {hasText: '添加映射'}).first();
  for (let i = 0; i < 25 && await addBtn.isDisabled(); i++) await page.waitForTimeout(200);
  step(!(await addBtn.isDisabled()), '「添加映射」按钮可点（别名与目标都填上了）');
  await addBtn.click();
  await page.waitForTimeout(2500);
  const tableText = await page.evaluate(() =>
    Array.from(document.querySelectorAll('tbody tr')).map((r) => r.textContent).join(' | '));
  step(tableText.includes('兜底模型'), '表格里 `*` 那条显示成「兜底模型」', tableText.slice(0, 160));
  step(tableText.includes('glm-5.2'), '兜底的目标模型显示出来');
}

{
  const bad = await api('POST', '/api/settings/model-map', {'*': '*'});
  const detail = String(bad.body?.detail || '');
  step(bad.status >= 400 && bad.status < 500, '「目标写成 *」被拒', `${bad.status} ${detail.slice(0, 80)}`);
  const good = await api('POST', '/api/settings/model-map', {'*': 'glm-5.2', 'a-typo-model': 'glm-5.2'});
  step(good.status === 200, '正常映射（含兜底）保存成功');
  const got = await api('GET', '/api/settings/model-map');
  step(got.body?.['*'] === 'glm-5.2', '读回来兜底仍在');
}

// 兜底要真的接住未映射的名字（网关侧，不需要上游真的回答）
{
  const r = await api('POST', '/v1/chat/completions', {
    model: 'gpt-6-luna', messages: [{role: 'user', content: 'hi'}], stream: false,
  });
  // mock 上游对未知路径返回 404 —— 关键是**请求没被模型名拦掉**（不是 4xx 参数错误）
  step(r.status !== 400, `未映射的名字能交给兜底（status=${r.status}，不是 400 参数错误）`,
       JSON.stringify(r.body).slice(0, 140));
}

// ═══ 密钥页「不限制版本」（#191） ═══
console.log('\n── 密钥页 › 新建密钥（#191）');
await page.goto(`${BASE}/keys`, {waitUntil: 'load'});
await page.waitForTimeout(2000);

async function createKey(name, unlimited) {
  await page.locator('button', {hasText: '新建密钥'}).first().click();
  await page.waitForTimeout(700);
  // 一切都在**弹窗内部**取：页面上另有同名按钮（空态那张卡片里也有「新建密钥」），
  // 不限定作用域会点到被遮罩盖住的那个（点击会一直等 overlay 让路而超时）。
  const dialog = page.locator('[role=dialog]').last();
  await dialog.locator('input[placeholder="例如：客服组"]').first().fill(name);
  const sw = dialog.locator('#key-realm-unlimited');
  step(await sw.count() > 0, '新建弹窗里有「不限制」开关');
  if (unlimited && !(await sw.isChecked())) {
    await sw.click({force: true});
    await page.waitForTimeout(300);
  }
  await dialog.getByRole('button', {name: '创建', exact: true}).click();
  await page.waitForTimeout(2500);
  // 建完会弹「这是你的新密钥」窗，不关掉它下一次点「新建密钥」会被遮罩挡住。
  const done = page.locator('button', {hasText: '我已保存'}).first();
  if (await done.count() > 0) {
    await done.click().catch(() => {});
    await page.waitForTimeout(600);
  }
}

await createKey('不限版本-验证', true);
await createKey('跟随页面版本-验证', false);
await page.screenshot({path: path.join(OUT, 'keys-realm.png'), fullPage: true});

const keyRows = await page.evaluate(() => {
  const ths = Array.from(document.querySelectorAll('th')).map((e) => e.textContent.trim());
  const out = [];
  for (const tr of document.querySelectorAll('tbody tr')) {
    const tds = tr.querySelectorAll('td');
    if (!tds.length) continue;
    const row = {};
    ths.forEach((h, i) => { if (tds[i]) row[h] = (tds[i].textContent || '').trim(); });
    out.push(row);
  }
  return out;
});
const realmOf = (name) => {
  const r = keyRows.find((x) => Object.values(x).some((v) => String(v).includes(name)));
  return r ? String(r['版本'] || '') : '(没找到)';
};
step(realmOf('不限版本-验证') === '未限定', '勾了开关建出来的密钥是「未限定」', realmOf('不限版本-验证'));
step(realmOf('跟随页面版本-验证') === '国内版', '没勾时仍跟随当前页面版本（国内版）', realmOf('跟随页面版本-验证'));

// ═══ 任务页「每日领补偿」（活动补偿自动领取） ═══
console.log('\n── 任务页 › 每日领补偿');
await page.goto(`${BASE}/tasks`, {waitUntil: 'load'});
await page.waitForTimeout(3000);
await page.screenshot({path: path.join(OUT, 'tasks-compensation.png'), fullPage: true});

{
  // 按标签文字定位那一行（行内应有：开关 / 标签 / 整点下拉 / 立即执行）
  const rowHandle = await page.evaluateHandle(() => {
    const label = Array.from(document.querySelectorAll('*')).find(
      (el) => el.children.length === 0 && (el.textContent || '').trim() === '每日领补偿');
    let node = label;
    while (node && !(node.querySelector?.('[role=switch]') && node.querySelector?.('button'))) {
      node = node.parentElement;
    }
    return node || null;
  });
  const row = rowHandle.asElement();
  step(!!row, '计划任务里有「每日领补偿」这一行');
  if (row) {
    const sw = await row.$('[role=switch]');
    const on = sw && await sw.evaluate(
      (el) => el.getAttribute('aria-checked') === 'true' || el.getAttribute('data-state') === 'checked');
    step(!on, '它默认**不**自动跑（上游每日任务本来就会领；这一行主打手动「立即执行」）');

    // **不能**用 row.$('button')：行内第一个 button 就是那个开关（Radix Switch 也是
    // button 元素），点它会切开关而不是执行任务（第一次跑就点错了，还看不出异常）。
    const btn = await row.$('button:has-text("立即执行")');
    step(!!btn, '这一行有「立即执行」按钮');
    if (btn) {
      await btn.click();
      // 轮询而不是「等 4 秒再看」：sonner 的 toast 默认约 4 秒就消失，
      // 固定等待正好会错过它。
      let toasts = '';
      for (let i = 0; i < 30 && !toasts; i++) {
        await page.waitForTimeout(250);
        toasts = await page.evaluate(() =>
          Array.from(document.querySelectorAll('[data-sonner-toast]'))
            .map((el) => (el.textContent || '').trim()).filter(Boolean).join(' | '));
      }
      step(toasts.includes('1 个领到 +500 积分'),
           '点「立即执行」后回报「1 个领到 +500 积分」（走的是 mock 的补偿接口）',
           toasts.slice(0, 160));
    }
  }

  // 再直接打一次接口：即使界面那条 toast 被改动，这条也能独立证明
  // 「面板 → 腾讯补偿接口 → 汇总回执」这条链是通的。
  const direct = await api('POST', '/api/accounts/daily-plan/run', {task: 'compensation'});
  step(direct.status === 200 && String(direct.body?.message || '').includes('1 个领到 +500 积分'),
       '直接调 /api/accounts/daily-plan/run 也回报领到 500',
       JSON.stringify(direct.body).slice(0, 180));
}

// ═══ 账号页「强制冷却」对话框 ═══
// 只验界面：点了会真的去停/起上游（harness 里没有 docker），所以**不提交**，
// 只确认按钮在、弹窗能开、档位与「永久」都在。
console.log('\n── 账号页 › 强制冷却');
await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
await page.waitForTimeout(2500);
await page.screenshot({path: path.join(OUT, 'accounts-cooling.png'), fullPage: true});

{
  // 先确认账号行真的渲染了 —— 否则「按钮找不到」会被误读成功能没接上
  const pageText = await page.evaluate(() => document.body.innerText);
  step(pageText.includes('演示号'), '账号列表里有那个演示账号');

  // 标题按 zh-CN 文案精确匹配（「强制进入冷却」/「调整冷却时间」）。
  // 不能只用 [title*=冷却]：行内还有「清除冷却」，会命中错的那个按钮。
  // 也必须加 `:visible` —— 账号页同时渲染**移动卡片**（md:hidden）与**桌面表**
  // （hidden md:block），不加就会点到 1600px 下被 display:none 的那一份，
  // 表现为「元素不可见」的点击超时（第一次跑就是这样）。
  const snowflake = page.locator(
    'button[title="强制进入冷却"]:visible, button[title="调整冷却时间"]:visible').first();
  step(await snowflake.count() > 0, '账号行上有「强制进入冷却」按钮（雪花）');
  if (await snowflake.count() > 0) {
    await snowflake.click();
    await page.waitForTimeout(700);
    const dialog = page.locator('[role=dialog]').last();
    const text = await dialog.textContent().catch(() => '');
    step(!!text && text.includes('永久'), '弹窗里有「永久」档', (text || '').slice(0, 80));
    step(!!text && /(小时|分钟|天)/.test(text), '弹窗里有自定义时长输入');
    await page.screenshot({path: path.join(OUT, 'accounts-cooling-dialog.png')});
    await page.keyboard.press('Escape');   // 不提交：真实停/起上游不在这里做
    await page.waitForTimeout(400);
  }
}

// ═══ 横向溢出（issue #151：1828px 下账号页/任务页出现横向滚动条） ═══
console.log(`
── 横向溢出（视口 ${WIDTH}px）`);
for (const path of ['/accounts', '/tasks']) {
  await page.goto(`${BASE}${path}`, {waitUntil: 'load'});
  await page.waitForTimeout(2200);
  const info = await page.evaluate(() => {
    const d = document.documentElement;
    const over = d.scrollWidth - d.clientWidth;
    let widest = null;
    if (over > 1) {
      for (const el of Array.from(document.querySelectorAll('*'))) {
        const r = el.getBoundingClientRect();
        if (r.right > d.clientWidth + 1) {
          widest = {tag: el.tagName, cls: String(el.className || '').slice(0, 80), right: Math.round(r.right)};
          break;
        }
      }
    }
    return {over, widest, table: !!document.querySelector('table')};
  });
  step(info.over <= 1, `${path} 没有横向滚动条（溢出 ${info.over}px）`,
       info.widest ? `${info.widest.tag}.${info.widest.cls} 右边界 ${info.widest.right}` : '');
}

// ═══ 控制台 ═══
console.log('\n── 控制台');
step(consoleErrors.length === 0, '浏览器控制台没有报错', consoleErrors.slice(0, 3).join(' | '));

await browser.close();
console.log('');
if (findings.length) {
  console.log(`FAILURES (${findings.length}):`);
  for (const f of findings) console.log('  - ' + f);
  process.exit(1);
}
console.log('ALL CHECKS PASSED');

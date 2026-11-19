/**
 * 红包抽奖页的浏览器断言（由 verify_claim_ui.py 调用）。
 *
 * 只钉「这一页不这么写就会让用户回来问人」的性质，不钉措辞。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7996';
const CODE = process.env.WB_CODE || '';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-claim');

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
const ctx = await browser.newContext({viewport: {width: 420, height: 900}, permissions: []});
const page = await ctx.newPage();

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

// 页面渲染不出来时，光看断言只能知道「没有」，不知道「为什么」——把浏览器
// 控制台与未捕获异常一起打出来（首轮就是这么定位到问题的）。
const consoleLines = [];
page.on('console', (m) => consoleLines.push(`[${m.type()}] ${m.text()}`));
page.on('pageerror', (e) => consoleLines.push(`[pageerror] ${String(e)}`));

// 剪贴板：headless 下 writeText 会失败，换成记录调用的 stub —— 断言的是
// 「我们真的把密钥写进剪贴板了」，而不是浏览器是否给了权限。
await ctx.addInitScript(() => {
  window.__copied = [];
  Object.defineProperty(navigator, 'clipboard', {
    value: {writeText: (t) => { window.__copied.push(t); return Promise.resolve(); }},
    configurable: true,
  });
});

await page.goto(`${BASE}/claim?code=${encodeURIComponent(CODE)}`, {waitUntil: 'load'});
await page.waitForTimeout(1200);

// 页面没渲染出来就别再往下走断言了：先把控制台打出来，否则只能看到一串
// 「没有这个按钮」，定位不到原因（首轮就是这么绕了一圈）。
const bodyNow = await page.locator('body').innerText();
if (bodyNow.trim().length < 60) {
  console.log('  ✗  页面几乎是空的，浏览器控制台如下：');
  consoleLines.slice(0, 10).forEach((l) => console.log('     ' + l.slice(0, 500)));
  console.log('     [body]', bodyNow.replace(/\n+/g, ' | ').slice(0, 200));
  await browser.close();
  process.exit(1);
}

const infoText = bodyNow;
console.log(`  [i] 打开 /claim 后的页面文本（前 200 字）：${bodyNow.replace(/\n+/g, ' | ').slice(0, 200)}`);
if (consoleLines.length) {
  console.log(`  [i] 控制台 ${consoleLines.length} 条，前 3 条：`);
  consoleLines.slice(0, 3).forEach((l) => console.log('     ' + l.slice(0, 300)));
}
step(/验收红包/.test(infoText), '抽奖页显示了红包标题', infoText.split('\n').slice(0, 3).join(' / '));
step(/还剩 2 \/ 2 份/.test(infoText) || /2 \/ 2/.test(infoText), '显示了剩余份数',
     (infoText.match(/还剩[^\n]*/) || [''])[0]);
step(new RegExp(`${BASE.replace(/[/.]/g, '\\$&')}/v1`).test(infoText),
     '抽之前就能看到调用地址（带当前 origin）',
     (infoText.match(/https?:\/\/[^\s]*\/v1/) || [''])[0]);
await page.screenshot({path: path.join(OUT, 'before.png'), fullPage: true});

// 开启抽奖
await page.getByRole('button', {name: /开启/}).click();
await page.waitForTimeout(1400);
await page.screenshot({path: path.join(OUT, 'won.png'), fullPage: true});
const won = await page.locator('[role=dialog]').last().innerText();

step(/恭喜/.test(won), '抽到之后弹出恭喜弹窗', won.split('\n').slice(0, 4).join(' / '));
step(/2,?500/.test(won), '弹窗里显示额度（5000 均分两份 = 2500）',
     (won.match(/[\d,]{3,}/) || [''])[0]);
const keyMatch = won.match(/wbk_[A-Za-z0-9_-]{8,}/);
step(!!keyMatch, '弹窗里显示密钥', keyMatch ? keyMatch[0].slice(0, 16) + '…' : '(没找到 wbk_)');
step(/调用地址/.test(won), '弹窗里有「调用地址」这一块');
step(new RegExp(`${BASE.replace(/[/.]/g, '\\$&')}/v1`).test(won),
     '调用地址就是当前站点（子路径部署时也对）',
     (won.match(/https?:\/\/[^\s]*\/v1/) || [''])[0]);
step(/OpenAI/.test(won), '给了「填到哪里」的一句话说明',
     (won.match(/[^\n]*OpenAI[^\n]*/) || [''])[0]);

// 彩屑不能挡点击：点「完成」必须点得到
const confetti = await page.evaluate(() => {
  const el = document.querySelector('.wb-confetti');
  if (!el) return null;
  const cs = getComputedStyle(el);
  return {pieces: el.children.length, pe: cs.pointerEvents, z: cs.zIndex};
});
step(!!confetti && confetti.pieces > 10, '抽中时撒了彩屑',
     confetti ? `片数=${confetti.pieces}` : '(没有彩屑层)');
step(!!confetti && confetti.pe === 'none', '彩屑层不截点击（pointer-events: none）',
     confetti ? `pointer-events=${confetti.pe} z=${confetti.z}` : '');

await page.getByRole('button', {name: /完成/}).click();
await page.waitForTimeout(700);
const copied = await page.evaluate(() => window.__copied || []);
step(copied.some((t) => /^wbk_/.test(t)), '点「完成」会把密钥写进剪贴板',
     `剪贴板收到 ${copied.length} 条，首条=${(copied[0] || '').slice(0, 14)}…`);

// 再开一次：应当能看到「上次领到的那份」——包括密钥本身（关掉弹窗才想起没存
// 是常事，能刷新找回比「请联系发红包的人」有用得多）
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(1200);
const again = await page.locator('body').innerText();
step(/已经领过/.test(again), '同一个网络再打开时提示已领过',
     (again.match(/[^\n]*已经领过[^\n]*/) || [''])[0]);
step(!!keyMatch && again.includes(keyMatch[0]),
     '而且把上次那份密钥显示回来了（明文只显示一次，刷新能找回）',
     keyMatch ? `找回升钥 ${keyMatch[0].slice(0, 16)}…` : '(首轮没抓到密钥)');
const againEndpoint = new RegExp(`${BASE.replace(/[/.]/g, '\\$&')}/v1`).test(again);
step(againEndpoint, '二次访问时调用地址仍在页面上');
await page.screenshot({path: path.join(OUT, 'after.png'), fullPage: true});

await browser.close();
console.log('\n=== 结果 ===');
if (findings.length) {
  console.log(`✗ ${findings.length} 项未通过：`);
  findings.forEach((f) => console.log('   - ' + f));
  if (consoleLines.length) {
    console.log('\n浏览器控制台（前 12 条）：');
    consoleLines.slice(0, 12).forEach((l) => console.log('   ' + l.slice(0, 400)));
  }
  process.exit(1);
}
console.log('ALL CHECKS PASSED');

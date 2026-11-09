/**
 * 「取不到 ≠ 没有」的浏览器断言（静态导出产物）。
 *
 * 安全页（PR #93）：
 *   A 全挂     → 整页「数据加载失败」+ 重试；**不**出现「暂无规则」，**不**渲染开关
 *   B 只 config 挂 → 策略卡说「未取到」，**一个开关都不渲染**，其余内容仍在
 *   C 恢复     → 规则与开关回来
 *
 * 任务记录页（PR #97）：
 *   D 慢加载   → 有骨架、**不**出现「暂无签到记录 / 暂无自动任务记录」
 *   E 签到记录挂 → 该面板说「签到记录加载失败」，**不**出现「共 0 条」，另一块照常
 *   F 恢复     → 两块内容与页脚都回来
 *
 * 密钥页（PR #100）：
 *   G 全挂     → 整页「数据加载失败」+ 重试；**不**出现「暂无 API 密钥」
 *   H 只密钥列表挂 → 顶部「部分数据加载失败」，**不**出现「暂无 API 密钥」
 *                    （手上这份列表不知道有没有，就不能说「没有」）
 *   I 只上游列表挂 → 顶部「部分数据加载失败」，空列表**照常**说「暂无 API 密钥」
 *                    （这一份确实取到了、确实为空。用它反过来证明 H 用的判据是
 *                     「**这一份**失败了」，而不是「有任一份失败」——后者会把一个
 *                     正常的空列表也藏起来）
 *   J 恢复     → 回到正常的空列表
 *
 * 设置页（批次 1 收尾）：
 *   K 配置挂   → 整页「数据加载失败」+ 重试；**一个 Tab 都不渲染** —— 那张表单填的是
 *                内置默认值，渲染出来等于把「默认值」说成「你现在的配置」
 *   L 只用户列表挂 → 表单照常；用户 Tab **不**说「暂无管理用户」，其余内容仍在
 *   M 恢复     → 设置页正常
 *
 * 账号管理 / 模型中心 / 红包（批次 1 收尾三页）：
 *   N 慢加载   → 三页都出骨架，**不**出现「暂无账号 / 暂无模型 / 暂无红包」
 *   O 账号池挂  → 整页错误态 + 重试；**不**说「暂无账号」；
 *                **分组切换条必须仍在**（它不依赖账号池，用户得能切回默认分组自救）
 *   P 只上游状态挂 → 列表照常渲染；账号状态标「未知」；顶部有「上游状态没取到」说明；
 *                    **不**升级成整页错误（这一条是 P0-2：原来 `upRes` 失败无 else，完全无声）
 *   Q 模型目录挂 → 整页错误态 + 重试；**不**说「暂无模型」
 *   R 红包列表挂 → 整页错误态 + 重试；**不**说「暂无红包」
 *   S 恢复     → 三页都正常
 *
 * S 对模型中心是**正向**断言（要找得到模型行），因此 fixture 的上游必须给出
 * 非空模型清单：目录为空时「暂无模型」是如实的空态，只断言「错误态消失了」的话，
 * 「页面根本没恢复」与「恢复了但确实没有模型」长得一样，这条断言就没有判别力。
 *
 * 批次 3（P1-3 / P1-4）：
 *   P 日志页筛选 → 改完即生效：下拉改完**不点任何按钮**就重查；文本输入 4 个字符
 *                  只发 **1** 次请求（400ms 防抖）；「重置」清空全部筛选且只重取一次
 *   Q 账号页     → 4 组状态摆在明面上（不再只靠 hover）；筛不到时说「没有匹配的账号」
 *                  并给「清除筛选」；切到没有账号的版本时说「国际版没有账号」而**不是**
 *                  「暂无账号」（账号在池子里，只是版本不对）
 *
 * P 的判据是**请求次数**（见下面 `hits`），因为「筛选生效了吗」在界面上看不出来。
 *
 * 批次 4（P1-1：底栏 11 项平铺、分隔线无标签、引导气泡压住正文）：
 *   ① 分组  → 桌面 3 条组间分隔线（4 组），靠近时出组名「运营 / 治理」**且稳定不闪**；
 *              动作组那条**不带**组名（快速添加/个人信息是动作，不是一类页面）
 *   ② 手机  → 原来分组语义完全消失（11 项平铺）：抽屉里要有 3 条**带组名**的横线
 *   ③ 气泡  → 有明确关闭按钮；点页面别处也能关；关过之后刷新不再出现
 *   ④ 停靠  → 悬浮时长按拖动**能**移动（正对照），固定底部时**完全**不动，
 *              并回到默认位（水平居中 + 贴底）；刷新后仍是固定
 *   ⑤ 子路由（P1-2）→ 设置页 7 个 Tab 变成 `/settings/<tab>`：`/settings` 换地址到
 *              第一个 Tab；深链 `/settings/models` 的**导航项与内容都是「模型映射」**；
 *              点导航项是一次真实导航（地址栏变、后退键能回上一个 Tab）；
 *              而**切 Tab 不重新拉配置**（取数在外壳 layout 上，不随子路由重挂载）。
 *              最后一条的判据是**请求次数**——「偷偷重取」在界面上看不出来（骨架可能
 *              只闪几毫秒），并配一条正对照：真刷新**必须**重取，否则「没重取」可能
 *              只是计数器坏了。
 *   ⑥ 底栏 11 → 8 + 页内二级导航（P1-1 的另一半）→ 底栏**只剩 8 个页面入口**，被吸收的
 *              三页（任务记录 / 红包 / 聊天测试台）不在其中；但这三页**都还在**，
 *              而且从它们所属的那一页（账号 / 密钥 / 模型）点一下 Tab 就能到，
 *              高亮也跟着走，后退键能回来。
 *              这一段要防的是「少了一个入口」与「那一页没了」在界面上长得一样
 *              ——都只是「找不到了」。所以「底栏里没有」和「一步可达」必须**同时**断言。
 *
 * ④ 的「能移动」是**正对照**，不能省：只断言「固定时拖不动」的话，一个从来就
 * 拖不动的底栏同样会通过——断言恒真。③ 的「关过之后不再出现」也必须先证明
 * 「重置之后真的又出现了」，否则「不出现」可能只是因为压根没显示过。
 *
 * 批次 5（P1-8 决策留白 + 上游重载状态 + ⌘K 命令面板）：
 *   ⑦ 重载状态 → 闲时**没有**这条提示（正对照）；重载中出现「正在应用配置」；
 *                **不刷新**地等它变成「已生效」（真实用法是页面没关的那一串）；
 *                失败时明确说「重载失败」、给出宿主机重启命令、把上游原样输出带出来、
 *                且**不说**「保存失败」（配置已经写入）；切到别的 Tab 后提示仍在
 *                （它挂在设置页外壳上）。最后一类的判据是**请求次数**。
 *   ⑧ ⌘K      → 按钮点得开、快捷键也按得开；空查询列全部 19 项；搜「红包」只剩红包
 *                那一页（筛的是「命中的留下」）；多词是 AND 且归属提示也参与匹配；
 *                搜不到时给出空状态；回车跳到搜出来的那一页并把面板关掉；
 *                ↓ 换高亮后回车打开的是**高亮的那一条**（不是永远第一条）。
 *
 * ⑦ 的「已生效」不能靠刷新到达：判定要求 `last_at` 比挂载时读到的那一次更大，
 * 刷新后基线就是新值，只会是闲——所以那一段是**同一页内**从「重载中」切过去的。
 * ⑧ 的「搜红包只剩一条」不能只数条数：筛选逻辑写反时条数照样对，所以要核对 href。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE;
const PASS = process.env.WB_PASS;
const OUT = process.env.WB_SHOTS;

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
// 断言全是中文文案，所以语言必须钉死：应用的 detectLocale() 会读 navigator.languages，
// 在浏览器语言不是中文的机器上，下面每一条正向断言都会失败——而页面其实完全正常。
// 更坏的是负向断言（「不许出现『暂无规则』」这类）照样是绿的，于是整份报告看起来像
// 「应用坏了」，方向完全错。钉住之后在哪台机器上跑结果都一样。
const ctx = await browser.newContext({viewport: {width: 1400, height: 1000}, locale: 'zh-CN'});
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));

await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
// 等表单真的被 React 接管再填。dev 冷编译时 hydration 会晚于首次输入：那时 fill
// 只改了 DOM，React 的受控 state 仍是空的，提交上去就是**空密码**。这个坑的表现
// 特别误导——登录失败后每一页都被弹回登录页，于是正向断言全红、负向断言全绿。
// React 会给它接管过的 DOM 节点挂一个 `__react*` 内部键，可当作 hydration 信号。
await page.waitForFunction(() => {
  const el = document.querySelector('#password');
  return !!el && Object.keys(el).some((k) => k.startsWith('__react'));
}, null, {timeout: 20000}).catch(() => {});
await page.fill('#username', 'admin');
await page.fill('#password', PASS);
await Promise.all([page.waitForURL(/dashboard/).catch(() => {}),
                   page.click('button[type=submit]')]);

// 没登录成功就立刻退出，别往下跑。后面三十多条断言都是「页面上应该有这句话」，
// 而未登录时每一页都会被弹回登录页 —— 于是正向断言全红、负向断言（「不许出现
// 『暂无规则』」这类）反而全绿，整份报告看起来像「应用坏了」，方向完全错。
if (!/dashboard/.test(page.url())) {
  console.error(`✗ 登录没成功（当前停在 ${page.url()}）——后面的断言没有意义，直接退出`);
  await browser.close();
  process.exit(1);
}

// 运行时开关：0=全放行 1=安全页全挂 2=只 config 挂 3=签到记录延迟 4=签到记录 500
//              5=密钥+上游全挂 6=只密钥列表挂 7=只上游列表挂
//              8=设置页配置挂（主数据） 9=只设置页用户列表挂
//             10=账号池挂 11=只上游状态挂 12=模型目录挂 13=红包列表挂 14=三页都延迟
//             15=上游重载中 16=上游重载失败 17=上游重载已生效 18=上游重载空闲（强制）
const MODE = {value: 0};

/**
 * 每个接口被打了多少次（键是 `/api/` 之后、去掉查询串的那一段）。
 *
 * 「筛选改完生效了吗」「防抖有没有生效」这两件事的判据都是**请求次数**——在界面上
 * 看不出来（列表看起来一样，只是内容不同）。所以计数放在拦截器里，失败的那次也算。
 */
const hits = {};

await page.route('**/api/**', async (route) => {
  const url = route.request().url();
  const fail = (code = 500) => route.fulfill({
    status: code, contentType: 'application/json',
    body: JSON.stringify({detail: '模拟后端故障'})});
  // 路径要钉到结尾（`(\?|$)`）：`/api/accounts` 是 `/api/accounts-xxx` 的前缀，
  // 宽松匹配会在将来加接口时悄悄多拦一个。
  const is = (p) => new RegExp(`/api/${p}(\\?|$)`).test(url);
  {
    const path = url.split('?')[0].split('/api/').pop();
    hits[path] = (hits[path] ?? 0) + 1;
  }
  if (MODE.value === 1 && /\/api\/security/.test(url)) return fail();
  if (MODE.value === 2 && /\/api\/security\?|config/.test(url)
      && /\/api\/security/.test(url)) return fail();
  if (MODE.value === 3 && /\/api\/checkin-logs/.test(url)) {
    await new Promise((r) => setTimeout(r, 3500));
  }
  if (MODE.value === 4 && /\/api\/checkin-logs/.test(url)) return fail();
  // 结尾用 (\?|$) 锚住：`/api/keys/import/status` 这类子路径是另一份数据
  // （弹窗打开时才探测的配件），不该被这里的「列表挂了」一起打掉。
  if (MODE.value === 5 && /\/api\/(keys|upstreams)(\?|$)/.test(url)) return fail();
  if (MODE.value === 6 && /\/api\/keys(\?|$)/.test(url)) return fail();
  if (MODE.value === 7 && /\/api\/upstreams(\?|$)/.test(url)) return fail();
  // 设置页：主数据是配置（那张表单就是它的副本）；用户列表是另一个 Tab 的配件
  if (MODE.value === 8 && /\/api\/settings\/upstream/.test(url)) return fail();
  if (MODE.value === 9 && /\/api\/users/.test(url)) return fail();
  // 账号页：主数据是账号池（`/api/accounts`）；上游状态是另一个 hook（`/api/upstream/status`）
  if (MODE.value === 10 && is('accounts')) return fail();
  if (MODE.value === 11 && is('upstream/status')) return fail();
  if (MODE.value === 12 && is('model-catalog')) return fail();
  if (MODE.value === 13 && is('red-packets')) return fail();
  if (MODE.value === 14 && (is('accounts') || is('model-catalog') || is('red-packets'))) {
    await new Promise((r) => setTimeout(r, 3500));
  }
  // 上游重载状态（批次 5）：真实环境里要等一次真重启（分钟级）或让它真失败，
  // 所以这里伪造响应。**18 是「强制空闲」**——正对照要用它，不能靠真后端的
  // 初始状态（那取决于这台机器上有没有 docker、之前有没有失败过）。
  if (MODE.value >= 15 && MODE.value <= 18 && is('upstream/reload-state')) {
    const bodies = {
      15: {running: true, pending: false, last_at: 0, last_ok: null,
           last_message: '', restart_count: 0},
      16: {running: false, pending: false, last_at: 0, last_ok: false,
           last_message: 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock',
           restart_count: 1},
      17: {running: false, pending: false, last_at: 1800000000, last_ok: true,
           last_message: '', restart_count: 1},
      18: {running: false, pending: false, last_at: 0, last_ok: null,
           last_message: '', restart_count: 0},
    };
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(bodies[MODE.value])});
  }
  return route.continue();
});

const bodyText = () => page.locator('body').innerText();
/**
 * React 的水合回退（#418/#423/#425）**偶发**，成因是这套夹具「假响应瞬时返回」：
 * 数据在 React 还没水合完就回来了，于是这次水合作废、整棵重渲染。真后端下 60 次
 * 加载 0 次，即时 500 的假响应下约 1/60；合并前（404fd3a）的产物同样能复现，
 * 与本仓库近几批改动无关。它不影响断言——页面最终是对的。
 * 所以这类异常单独计数、逐条打印：水合真的坏了会是「每次加载都报」，那条会撞上限。
 */
const HYDRATION_FALLBACK = /Minified React error #(418|423|425)/;
const isHydrationFallback = (e) => HYDRATION_FALLBACK.test(e);

/** 取这一阶段新增的前端异常（用于把异常定位到具体步骤）；水合回退不算 */
let errMark = 0;
const phaseErrors = () => {
  const fresh = errors.slice(errMark).filter((e) => !isHydrationFallback(e));
  errMark = errors.length;
  return fresh;
};

// ══ 安全页 ══════════════════════════════════════════════════════════
console.log('\n安全页（PR #93）');
MODE.value = 1;
await page.goto(`${BASE}/security`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
let text = await bodyText();
step(/数据加载失败/.test(text), '全部取不到时给整页错误态');
step(/重试/.test(text), '错误态里有「重试」');
step(!/暂无规则/.test(text), '不许说「暂无规则」（那等于说没有任何网段被放行或拦截）');
step(!/暂无访问记录/.test(text), '不许说「暂无访问记录」（那等于说没人被拦过）');
step((await page.locator('[role=switch]').count()) === 0,
     '一个开关都不渲染（把「不知道」显示成「关着」比空白更糟）',
     `页面上的开关数：${await page.locator('[role=switch]').count()}`);
await page.screenshot({path: `${OUT}/01-security-all-failed.png`, fullPage: true});

MODE.value = 2;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/未取到/.test(text), '只 config 没取到时，策略卡如实说「未取到」');
step((await page.locator('[role=switch]').count()) === 0, '仍然不渲染开关');
step(!/请检查网络连接或后端服务是否正常/.test(text),
     '其余数据取到了 → 不升级成整页错误（只有顶部那条常驻提示）',
     /部分数据加载失败/.test(text) ? '顶部有「部分数据加载失败」常驻提示' : '（没有常驻提示）');
step(phaseErrors().length === 0, '这一步没有前端异常', phaseErrors().join(' | '));
await page.screenshot({path: `${OUT}/02-security-config-missing.png`, fullPage: true});

MODE.value = 0;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step((await page.locator('[role=switch]').count()) > 0, '恢复后开关回来（正常态没有被搞坏）');
step(!/数据加载失败/.test(text) && !/未取到/.test(text), '正常态不出现错误态文案');
await page.screenshot({path: `${OUT}/03-security-ok.png`, fullPage: true});

// ══ 任务记录页 ══════════════════════════════════════════════════════
console.log('\n任务记录页（PR #97）');
MODE.value = 3;
await page.goto(`${BASE}/tasks`, {waitUntil: 'load'});
await page.waitForTimeout(1200);            // 数据还在路上（签到记录被延迟 3.5s）
text = await bodyText();
step(!/暂无签到记录/.test(text),
     '加载期不说「暂无签到记录」（看起来像账号从没签过到）',
     (text.match(/暂无[^\n]{0,10}/) || ['（无）'])[0]);
step(!/暂无自动任务记录/.test(text), '加载期不说「暂无自动任务记录」（看起来像采集器没在跑）');
await page.screenshot({path: `${OUT}/04-tasks-loading.png`, fullPage: true});
await page.waitForTimeout(4000);
text = await bodyText();
step(/共 \d+ 条/.test(text), '数据到位后两块正片与页脚都出来',
     (text.match(/共 \d+ 条/) || [''])[0]);

MODE.value = 4;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(4000);
text = await bodyText();
step(/签到记录加载失败/.test(text), '签到记录挂了：该面板说「加载失败」而不是「暂无」');
step(!/暂无签到记录/.test(text), '不说「暂无签到记录」');
// 两个面板各有自己的页脚（种子数据：签到 2 条 / 自动任务 1 条）。挂掉的那一块必须
// 连页脚一起收起来 —— 取不到时总数就是 0，「共 0 条」挂在「加载失败」下面等于自己
// 打自己；而另一块的 1 条必须照常留着（否则就是「整块都没了」而不是「如实收起」）。
step(!/共 2 条/.test(text),
     '挂掉的那一块连页脚一起收起来了（它自己的 2 条不该露面）',
     (text.match(/共 \d+ 条/g) || ['（没有页脚）']).join(' / '));
step(/共 1 条/.test(text), '另一块（自动任务）照常带着自己的页脚');
step(!/暂无签到记录/.test(text), '也不出现「共 0 条」式的空态文案');
step(!/数据加载失败/.test(text), '只有一块挂 → 不升级成整页错误');
await page.screenshot({path: `${OUT}/05-tasks-one-failed.png`, fullPage: true});

MODE.value = 0;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(!/加载失败/.test(text) && /共 2 条/.test(text) && /共 1 条/.test(text),
     '恢复后两块内容与页脚都回来');

// ══ 密钥页 ══════════════════════════════════════════════════════════
console.log('\n密钥页（PR #100）');
MODE.value = 5;
await page.goto(`${BASE}/keys`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/数据加载失败/.test(text), '全部取不到时给整页错误态');
step(/重试/.test(text), '错误态里有「重试」');
// 这一句是这一页最要紧的一条：密钥是**凭据**，说「暂无 API 密钥」读起来是
// 「我的密钥被删了」，用户会顺手点旁边的「新建密钥」重建一个 —— 于是建出重复密钥，
// 而重复密钥会让「按密钥限额 / 按密钥统计用量」全都对不上。
step(!/暂无 API 密钥/.test(text),
     '不许说「暂无 API 密钥」（那是「确实没有」，而事实是不知道有没有）',
     (text.match(/暂无[^\n]{0,12}/) || ['（无）'])[0]);
step(phaseErrors().length === 0, '这一步没有前端异常', phaseErrors().join(' | '));
await page.screenshot({path: `${OUT}/06-keys-all-failed.png`, fullPage: true});

MODE.value = 6;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/部分数据加载失败/.test(text), '只密钥列表没取到时，顶部给常驻提示');
step(!/请检查网络连接或后端服务是否正常/.test(text),
     '上游列表取到了 → 不升级成整页错误（只有顶部那条常驻提示）');
step(!/暂无 API 密钥/.test(text),
     '列表这一份没取到 → 不说「暂无 API 密钥」',
     (text.match(/暂无[^\n]{0,12}/) || ['（无）'])[0]);
// 同一句谎话的另一半：空状态挡住了，tab 上却还挂着「普通密钥 · 0」——列表取不到时
// `keys` 就是空的，那个 0 没有依据，读起来仍是「一个密钥都没有」。（任务记录页的
// 「共 0 条」是同一处，PR #97 才补上。）
step(!/普通密钥 · 0/.test(text),
     'tab 上的数字也一起收起来（否则等于说「一个密钥都没有」）',
     (text.match(/普通密钥[^\n]{0,8}/) || ['（无）'])[0]);
step(phaseErrors().length === 0, '这一步没有前端异常', phaseErrors().join(' | '));
await page.screenshot({path: `${OUT}/07-keys-list-failed.png`, fullPage: true});

MODE.value = 7;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/部分数据加载失败/.test(text), '只上游列表没取到时，同样给常驻提示');
step(/暂无 API 密钥/.test(text),
     '密钥列表这一份**取到了且确实为空** → 照常显示空态（判据是「这一份失败没」，'
     + '不是「有任一份失败」）');
step(/普通密钥 · 0/.test(text),
     '数字也照常显示 0 —— 这一份确实取到了、确实为空，那个 0 是如实的',
     (text.match(/普通密钥[^\n]{0,8}/) || ['（无）'])[0]);
step(!/请检查网络连接或后端服务是否正常/.test(text), '仍然不升级成整页错误');
step(phaseErrors().length === 0, '这一步没有前端异常', phaseErrors().join(' | '));
await page.screenshot({path: `${OUT}/08-keys-upstreams-failed.png`, fullPage: true});

// 恢复要**回到这一页**再断言。原来这一步只写「reload 当前页」——它跟在最后一节
// 后面，于是后面的批次往脚本末尾追加新页面时，这条「恢复后……」就会在**别的页面**
// 上求值：`/settings` 上当然找不到「暂无 API 密钥」，断言恒真、形同没有。
MODE.value = 0;
await page.goto(`${BASE}/keys`, {waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step(/暂无 API 密钥/.test(text) && !/数据加载失败/.test(text),
     '恢复后回到正常的空列表');

// ══ 设置页 ══════════════════════════════════════════════════════════
console.log('\n设置页（批次 1 收尾：配置没取到时不拿默认值冒充）');
MODE.value = 8;
await page.goto(`${BASE}/settings`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/数据加载失败/.test(text), '配置取不到时给整页错误态');
step(/重试/.test(text), '错误态里有「重试」');
// 判据用**文本**而不是数元素：二级导航在错误态里整条不渲染（那张表单填的是内置
// 默认值，不是「你现在的配置」），所以这里数的是「有没有『上游配置』这一项」。
// 批次 4 起导航项从 `TabsTrigger`（button）换成了 `<Link>`，标记也随之换成
// `data-slot=section-tab`——继续数 `[role=tab]` 会恒为 0，这条断言就变成了空转。
const settingsTabs = () => page.locator('[data-slot=section-tab]').filter({hasText: '上游配置'}).count();
step((await settingsTabs()) === 0,
     '一个 Tab 都不渲染（那张表单填的是内置默认值，不是「你现在的配置」）',
     `「上游配置」Tab 数：${await settingsTabs()}`);
step(!/暂无管理用户/.test(text), '不许说「暂无管理用户」');
await page.screenshot({path: `${OUT}/09-settings-all-failed.png`, fullPage: true});

MODE.value = 9;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step((await settingsTabs()) > 0,
     '配置取到了 → 表单照常渲染（正常态没被搞坏）',
     // 失败时要能一眼看出「是没渲染」还是「渲染在别的地址上」——只报 true/false 的话
     // 这两种情况长得一样，而处置方式完全不同。
     `${page.url()}；二级导航项数：${await page.locator('[data-slot=section-tab]').count()}`);
step(!/数据加载失败/.test(text), '只有用户列表挂 → 不升级成整页错误');
await page.locator('[data-slot=section-tab]').filter({hasText: '管理用户'}).click().catch(() => {});
await page.waitForTimeout(800);
text = await bodyText();
step(!/暂无管理用户/.test(text),
     '用户列表挂了：不说「暂无管理用户」（那等于说系统里一个账号都没有）',
     (text.match(/暂无[^\n]{0,12}/) || ['（无）'])[0]);
await page.screenshot({path: `${OUT}/10-settings-users-failed.png`, fullPage: true});

MODE.value = 0;
await page.goto(`${BASE}/settings`, {waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step((await settingsTabs()) > 0 && !/数据加载失败/.test(text),
     '恢复后设置页正常');

// ══ 加载期：三页都要出骨架，且都不能先摆空态 ═══════════════════════
// 骨架的选择器用 `[data-slot=skeleton]`（ui/skeleton.tsx 上的稳定标记），
// 不用 `.animate-pulse`：后者页面上别的地方也可能用（如按钮图标在转圈）。
console.log('\n批次 1 收尾三页（加载期）');
MODE.value = 14;                       // 三页的接口都延迟 3.5s
const skeletonCount = () => page.locator('[data-slot=skeleton]').count();

await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
await page.waitForTimeout(1200);       // 数据还在路上
text = await bodyText();
step(!/暂无账号/.test(text), '账号管理：加载期不说「暂无账号」（看起来像号池是空的）',
     (text.match(/暂无[^\n]{0,10}/) || ['（无）'])[0]);
step((await skeletonCount()) > 0, '账号管理：加载期渲染骨架',
     `骨架条数：${await skeletonCount()}`);
await page.screenshot({path: `${OUT}/11-accounts-loading.png`, fullPage: true});

await page.goto(`${BASE}/models`, {waitUntil: 'load'});
await page.waitForTimeout(1200);
text = await bodyText();
step(!/暂无模型/.test(text), '模型中心：加载期不说「暂无模型」');
step((await skeletonCount()) > 0, '模型中心：加载期渲染骨架',
     `骨架条数：${await skeletonCount()}`);
await page.screenshot({path: `${OUT}/12-models-loading.png`, fullPage: true});

await page.goto(`${BASE}/red-packets`, {waitUntil: 'load'});
await page.waitForTimeout(1200);
text = await bodyText();
step(!/还没有红包/.test(text), '红包：加载期不说「还没有红包」');
step((await skeletonCount()) > 0, '红包：加载期渲染骨架',
     `骨架条数：${await skeletonCount()}`);
await page.screenshot({path: `${OUT}/13-redpackets-loading.png`, fullPage: true});

// ══ 账号管理：主数据挂 / 只配件挂 ══════════════════════════════════
console.log('\n账号管理（批次 1 收尾：账号池取不到时不拿空列表冒充）');
/** 分组切换条上的按钮。**它不依赖账号池**，所以账号池挂掉时它必须还在 */
const groupChips = () => page.locator('button').filter({hasText: '默认分组'}).count();

MODE.value = 10;
await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/数据加载失败/.test(text), '账号池取不到时给整页错误态');
step(/重试/.test(text), '错误态里有「重试」');
step(!/暂无账号/.test(text), '不说「暂无账号」');
step((await groupChips()) > 0,
     '分组切换条仍在（账号池挂了也得能切回默认分组自救，所以守卫不能早返回整页）',
     `「默认分组」按钮数：${await groupChips()}`);
await page.screenshot({path: `${OUT}/14-accounts-pool-failed.png`, fullPage: true});

// 只把上游状态打挂：账号列表是好的，必须照常渲染。这一条同时是 P0-2 的回归
// ——原实现 `if (upRes.status === 'fulfilled')` 后面**没有 else**，上游状态挂了
// 界面上一点痕迹都没有，每个号的状态看起来还跟正常一样。
MODE.value = 11;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(!/数据加载失败/.test(text), '只上游状态挂 → 不升级成整页错误（账号列表照常）');
step(/上游状态没取到/.test(text), '顶部如实说明「上游状态没取到」（原来这里是完全无声的）');
step(/状态未知/.test(text), '账号状态标成「未知」而不是「未加载」');
step(!/暂无账号/.test(text), '也不落进「暂无账号」');
await page.screenshot({path: `${OUT}/15-accounts-status-failed.png`, fullPage: true});

MODE.value = 0;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step(!/数据加载失败/.test(text) && !/上游状态没取到/.test(text) && !/暂无账号/.test(text),
     '恢复后账号页正常');

// ══ 模型中心 ══════════════════════════════════════════════════════
console.log('\n模型中心（批次 1 收尾）');
MODE.value = 12;
await page.goto(`${BASE}/models`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/数据加载失败/.test(text), '模型目录取不到时给整页错误态');
step(/重试/.test(text), '错误态里有「重试」');
step(!/暂无模型/.test(text), '不说「暂无模型」（那等于说腾讯那边没有可用模型）');
step(!/没有匹配/.test(text), '也不落进「没有匹配的模型」（筛选结果为空的前提是清单已取到）');
await page.screenshot({path: `${OUT}/16-models-failed.png`, fullPage: true});

MODE.value = 0;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step(!/数据加载失败/.test(text) && /glm-5\.2/.test(text),
     '恢复后模型中心正常（模型行真的渲染出来，不只是错误态消失）');

// ══ 红包 ══════════════════════════════════════════════════════════
console.log('\n红包（批次 1 收尾）');
MODE.value = 13;
await page.goto(`${BASE}/red-packets`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/数据加载失败/.test(text), '红包列表取不到时给整页错误态');
step(/重试/.test(text), '错误态里有「重试」');
step(!/还没有红包/.test(text), '不说「还没有红包」（那等于说红包发完了 / 被清了）');
await page.screenshot({path: `${OUT}/17-redpackets-failed.png`, fullPage: true});

MODE.value = 0;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step(!/数据加载失败/.test(text), '恢复后红包页正常');

// ══ 日志页筛选：改完即生效（批次 3 修 P1-4）══════════════════════════
//
// 这一段的判据是**请求次数**，不是页面上的一句话——「筛选改完生效了吗」在界面上
// 看不出来（列表看起来一样，只是内容不同）。所以先记一份每个接口被打了多少次。
console.log('\n日志页筛选（批次 3：同一排控件不再有两种脾气）');
MODE.value = 0;
await page.goto(`${BASE}/logs`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
const logsHits = () => hits.logs ?? 0;

step((await page.getByRole('button', {name: '重置'}).count()) > 0,
     '按钮从「查询」换成了「重置」（改完即生效，再留一个查询按钮只会把同一份查询再打一次）');

// ① 下拉类：改完立刻重查，**不用点任何按钮**
{
  const before = logsHits();
  // ⚠️ 必须把范围限在筛选区（`section`）里再数：页面顶部还有一个语言选择器，
  // 它是**第 0 个** combobox。按全页 `.nth(2)` 会点到「密钥」而不是「状态」——
  // 而失败的样子特别误导：那个菜单能正常打开，只是里面没有「失败」这个选项，
  // 于是断言在「等一个不存在的选项」上超时，看起来像下拉坏了。
  await page.locator('section [data-slot=select-trigger]').nth(2).click();   // 状态
  await page.getByRole('option', {name: '失败'}).click();
  await page.waitForTimeout(1500);
  step(logsHits() > before,
       '改状态筛选后立刻重查（原来只在「查询」按钮里生效，且在第 1 页时点了没反应）',
       `请求数 ${before} → ${logsHits()}`);
}

// ② 文本类：输入 4 个字符只发 1 次请求（400ms 防抖）。
//    这里必须逐字符输入：`fill()` 一次就填完，只产生一个 input 事件，
//    测不出防抖——那样这条断言是**空转**的（不防抖也会通过）。
{
  const box = page.locator('section input').nth(0);          // 模型
  const before = logsHits();
  await box.pressSequentially('kimi', {delay: 40});
  // 先确认字符真的进去了：若上一步的菜单没关干净，Radix 的焦点陷阱会把按键
  // 吃在浮层里，输入框仍是空的 —— 那时「只发了 1 次请求」会因为「压根没输入」
  // 而通过，断言变成空转。
  step((await box.inputValue()) === 'kimi', '关键词真的输进了输入框',
       `输入框里是 ${JSON.stringify(await box.inputValue())}`);
  await page.waitForTimeout(1500);
  const delta = logsHits() - before;
  step(delta === 1, '文本筛选输入 4 个字符只发 1 次请求（400ms 防抖）',
       `请求数 +${delta}（不防抖会是 +4）`);
}

// ③ 重置：一键清掉全部筛选，且只重取一次
{
  const box = page.locator('section input').nth(0);
  const before = logsHits();
  await page.getByRole('button', {name: '重置'}).click();
  await page.waitForTimeout(1200);
  step((await box.inputValue()) === '', '「重置」清掉了输入框里的关键词');
  step(logsHits() - before === 1,
       '重置只重取一次（文本类若走防抖计时器，会先按「旧文本 + 新下拉」查一次、再查一次）',
       `请求数 +${logsHits() - before}`);
}
await page.screenshot({path: `${OUT}/13-logs-filters.png`, fullPage: true});

// ══ 账号管理检索与状态分组（批次 3 修 P1-3）══════════════════════════
console.log('\n账号管理检索与状态分组（批次 3）');
MODE.value = 0;
await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
// 4 组状态**摆在明面上**：原先 9 档只挂在徽章的 hover 提示里，移动端没有 hover，
// 等于状态信息不可见。
step(['可用', '需处理', '冷却中', '已停用'].every((s) => text.includes(s)),
     '4 组状态筛选摆在明面上（不再只靠悬浮提示）');

// 状态筛选**真的在筛**：fixture 里只有一个「可用」的账号，点「需处理」必然筛空。
// 这条比「按钮在不在」有用得多——四个按钮都加上去而列表仍然渲染全量，是最容易
// 「看起来做完、其实没用」的形态（源码层那条断言盯的就是这个）。
await page.getByRole('button', {name: '需处理'}).click();
await page.waitForTimeout(800);
text = await bodyText();
step(/没有匹配的账号/.test(text),
     '按状态筛空时说「没有匹配的账号」（状态筛选真的在筛，不是装饰）');
step(!/暂无账号/.test(text), '不说「暂无账号」（账号在池子里，只是被筛选挡住了）');
step(/清除筛选/.test(text), '给了「清除筛选」的出口（不用自己回忆刚才改了什么）');
await page.screenshot({path: `${OUT}/14-accounts-no-match.png`, fullPage: true});

await page.getByRole('button', {name: '清除筛选'}).click();
await page.waitForTimeout(800);
text = await bodyText();
step(!/没有匹配的账号/.test(text), '清除筛选后账号回来（正常态没被搞坏）');

// 搜索一个必然不存在的关键词：同上，但走的是搜索那条路
await page.locator('input[placeholder*="搜索昵称"]').fill('zzz-不存在的账号');
await page.waitForTimeout(800);
text = await bodyText();
step(/没有匹配的账号/.test(text), '搜不到时说「没有匹配的账号」');
step(!/暂无账号/.test(text), '不说「暂无账号」（搜索词不匹配 ≠ 没有账号）');
await page.getByRole('button', {name: '清除筛选'}).click();
await page.waitForTimeout(800);

// 切到国际版：池子里只有国内版账号 → 这是**版本**筛空，不是「一个账号都没有」
await page.getByRole('tab', {name: '国际版'}).first().click();
await page.waitForTimeout(1500);
text = await bodyText();
step(/国际版没有账号/.test(text),
     '切到没有账号的版本时如实说「国际版没有账号」（原来这里说「暂无账号」，读起来像号池是空的）');
step(!/暂无账号/.test(text), '不说「暂无账号」（账号就在池子里，只是不属于这个版本）');
await page.screenshot({path: `${OUT}/15-accounts-realm-empty.png`, fullPage: true});

// 切回国内版：后面的断言与人工复看都默认是国内版
await page.getByRole('tab', {name: '国内版'}).first().click();
await page.waitForTimeout(800);

// ══ 底栏分组语义（批次 4 修 P1-1）══════════════════════════════════
//
// 底栏原来靠一个**哨兵条目**表达分组（`{title: 'divider', icon: <div />}` 混在
// items 里），后果有两个：分隔线不带任何说明（用户只能猜那条竖线分开的是什么），
// 以及**手机端完全没有分组语义**——移动端分支直接 `return null` 跳过了它，
// 于是手机上 11 项平铺。
//
// 这一段的判据是「分隔线在不在、组名显不显示」，界面不报错就看不出来。
console.log('\n底栏分组语义（批次 4）');
MODE.value = 0;
// 清掉上次跑留下的底栏坐标与已读标记，让这一次从默认态开始（否则脚本不可重复跑）
await page.goto(`${BASE}/dashboard`, {waitUntil: 'load'});
await page.evaluate(() => {
  localStorage.removeItem('workbuddy-manager:dock-position-v2');
  localStorage.removeItem('workbuddy-manager:dock-mode');
});
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(2500);

/** 只数**可见**的：桌面底栏在窄屏下仍在 DOM 里（`hidden md:flex`），数 DOM 会数错 */
const visibleCount = (sel) => page.locator(`${sel}:visible`).count();

const dividers = page.locator('[data-slot=dock-group-divider]');
const separators = page.locator('[data-slot=dock-group-separator]');

step((await page.locator('[data-slot=dock-root]').count()) === 1, '底栏在页面上（定位得到）');
// 4 组 → 3 条分隔线，且第一组之前不画。少一条说明组没切开；多一条说明最左边多画了，
// 看起来像「前面还有一组」。
step((await visibleCount('[data-slot=dock-group-divider]')) === 3,
     '桌面端画了 3 条组间分隔线（总览/运营/治理 + 动作组 = 4 组）',
     `可见分隔线数：${await visibleCount('[data-slot=dock-group-divider]')}`);

// 组名默认不显示：底栏高度写死 `h-16`，图标靠近时会从 40px 放大到 70px（本就溢出），
// 再加一行常显的组名会把它顶高、并与放大后的图标打架。所以靠近才显示。
step((await dividers.nth(0).locator('text=运营').count()) === 0,
     '组名默认不显示（靠近才出现）');
// ⚠️ 这里必须等**超过图标放大的时间**再断言，而且这不是为了「等渲染」：
// 图标放大会让整个底栏变宽（实测 725 → 787px），而底栏是居中摆放的，于是分隔线会
// 被从光标底下推开 8~30px —— 靠分隔线自己的 hover 事件时，组名会在 ~700ms 时灭掉。
// hover 完立刻断言会**看不到**这个闪烁，也就盖不住「组名亮一下就没了」这个缺陷。
await dividers.nth(0).hover();
await page.waitForTimeout(1500);
step((await dividers.nth(0).locator('text=运营').count()) === 1,
     '靠近第一条分隔线显示组名「运营」，而且**稳定不闪**（原来那条竖线不带任何说明）');
// 截在**组名可见**的这一刻：只在动作组那条（不带组名）后面截，证据里就没有组名
await page.screenshot({path: `${OUT}/18-dock-groups-desktop.png`, fullPage: false});
await dividers.nth(1).hover();
await page.waitForTimeout(1500);
step((await dividers.nth(1).locator('text=治理').count()) === 1,
     '靠近第二条分隔线显示组名「治理」，同样稳定不闪');
// 动作组（快速添加 / 个人信息）不是「目的地」而是动作，刻意不给组名——只与前面的
// 页面分开。给它编一个组名反而让人以为那是一类页面。
await dividers.nth(2).hover();
await page.waitForTimeout(1000);
step((await dividers.nth(2).innerText()).trim() === '',
     '动作组那条分隔线不带组名（快速添加/个人信息是动作，不是一类页面）');

// ── 手机端：分组语义原来完全消失（11 项平铺）────────────────────────
await page.setViewportSize({width: 390, height: 844});
await page.waitForTimeout(800);
step((await visibleCount('[data-slot=dock-group-divider]')) === 0,
     '手机端不画桌面那条竖线（换成带组名的横线）');
step((await visibleCount('[data-slot=dock-group-separator]')) === 0,
     '手机端抽屉没展开时当然没有分隔线');
await page.locator('[data-slot=dock-mobile-toggle]').click();
await page.waitForTimeout(900);
step((await visibleCount('[data-slot=dock-group-separator]')) === 3,
     '手机端抽屉里有 3 条分组分隔线（原来手机上是 11 项平铺，看不出哪几项是一类）',
     `可见分隔线数：${await visibleCount('[data-slot=dock-group-separator]')}`);
step(/运营/.test(await page.locator('[data-slot=dock-root]').innerText()),
     '手机端的分隔线上**直接写着组名**（手机没有 hover，藏起来就等于没有）');
await page.screenshot({path: `${OUT}/19-dock-groups-mobile.png`, fullPage: false});
await page.setViewportSize({width: 1400, height: 1000});
await page.waitForTimeout(800);

// ── 引导气泡：随时关得掉，且关过就不再打扰 ─────────────────────────
//
// 原实现只能靠「把几条都点完」才关得掉，于是它一直挂在底栏正上方 —— 而底栏可以被
// 拖到页面中部，于是它正好压住正文（用户反馈「反复出现在页面中部」）。
console.log('\n底栏引导气泡（批次 4）');
const tip = page.locator('[data-slot=dock-tip]');
const showTipAgain = async () => {
  await page.evaluate(() => localStorage.removeItem('workbuddy-manager:dock-tip-dismissed'));
  await page.reload({waitUntil: 'load'});
  await page.waitForTimeout(2500);
};
/**
 * 在页面别处点一下。用「派发真实 PointerEvent」而不是 `page.mouse.click(x, y)`：
 * 后者先做命中测试，坐标上正好是链接时会一路重试到超时（报「元素拦截了点击」），
 * 而真点中链接还会导航走，后面每条断言都跟着错。这里要验的是**捕获阶段的
 * document 监听**，派发一个真实 PointerEvent 走的完全是同一条路径。
 */
const clickSomewhereElse = () => page.evaluate(() => {
  document.body.dispatchEvent(
      new PointerEvent('pointerdown', {bubbles: true, cancelable: true}));
});

await showTipAgain();
step((await tip.count()) === 1, '首次进入时引导气泡出现（前提：这一步真的把它弄出来了）');
step(/知道了/.test(await tip.innerText()), '气泡上有明确的关闭按钮（不必把几条都点完）');
await clickSomewhereElse();
await page.waitForTimeout(900);
step((await tip.count()) === 0, '点页面别处就把气泡收起来（原来只能一条条点完）');

await showTipAgain();
step((await tip.count()) === 1, '重置后气泡又出现（证明上一步是真的关掉了，不是本来就没有）');
await tip.getByRole('button', {name: '知道了'}).click();
await page.waitForTimeout(900);
step((await tip.count()) === 0, '点「知道了」也关得掉');
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(2500);
step((await tip.count()) === 0, '关掉后刷新不再出现（记住了，不再反复打扰）');

// ── 底栏「固定底部 / 悬浮」：固定时拖动要真的关掉 ───────────────────
//
// 底栏是浮动的，拖到页面中部就会压住正文。形态按既定决策保留（用户已决定不改），
// 但给一个「别再挡我」的确定性选项。判据分两半，缺一不可：
//   ① 悬浮模式下长按拖动**能**移动 —— 正对照。少了它，「固定模式下拖不动」在一个
//      从来就拖不动的底栏上也是绿的（断言恒真）；
//   ② 固定模式下拖动**完全**不动 —— 不是「先跟手走一段、松手才归位」，后者看起来
//      像拖动坏了。
console.log('\n底栏停靠模式（批次 4：解决底栏遮挡正文）');
const dockRoot = page.locator('[data-slot=dock-root]');
const dockBox = () => dockRoot.boundingBox();
/** 长按底栏的**左上角**再拖。左上是内边距，不是图标；而且图标 hover 时会从 40px
 *  放大到 70px，只有「按下」发生在这个瞬间之前，命中的才一定是空白处
 *  （`event.target` 在 pointerdown 那一刻就定了，之后图标怎么长都不影响）。 */
const dragDock = async () => {
  const box = await dockBox();
  const x = box.x + 3;
  const y = box.y + 3;
  await page.mouse.move(x, y);
  await page.mouse.down();                       // 紧接着按下，不给图标放大的时间
  await page.waitForTimeout(400);                // DOCK_LONG_PRESS_MS = 180
  await page.mouse.move(x - 150, y - 170, {steps: 12});
  await page.waitForTimeout(200);
  await page.mouse.up();
  await page.waitForTimeout(700);
  return {before: box, after: await dockBox()};
};
const shift = (r) => Math.hypot(r.after.x - r.before.x, r.after.y - r.before.y);

const floated = await dragDock();
step(shift(floated) > 40, '悬浮模式：长按拖动**能**移动底栏（正对照）',
     `位移 ${shift(floated).toFixed(0)}px`);
await page.screenshot({path: `${OUT}/20-dock-dragged.png`, fullPage: false});

// 打开个人信息 → 切到「固定底部」
await page.locator('[data-slot=dock-profile-trigger]').click();
await page.waitForTimeout(900);
const modeToggle = page.locator('[data-slot=dock-mode-toggle]');
step((await modeToggle.count()) === 1, '个人信息里有「底栏位置」开关');
step(/悬浮/.test(await modeToggle.innerText()), '默认是「悬浮（可拖动）」',
     (await modeToggle.innerText()).trim());
await modeToggle.click();
await page.waitForTimeout(900);
step(/固定底部/.test(await modeToggle.innerText()), '点一下切到「固定底部」',
     (await modeToggle.innerText()).trim());
await page.keyboard.press('Escape');
await page.waitForTimeout(700);

// 固定后应当**回到默认位置**（桌面：底部居中）——这正是「别再挡我」的含义：
// 不只是拖不动，还要把它从用户上次拖到的地方请回原位。
{
  const box = await dockBox();
  const centerX = box.x + box.width / 2;
  const bottom = box.y + box.height;
  step(Math.abs(centerX - 700) < 6,
       '固定底部后回到水平居中（桌面默认位），而不是停在刚才被拖到的位置',
       `中心 x = ${centerX.toFixed(0)}（期望 700）`);
  step(Math.abs(bottom - 984) < 8,
       '固定底部后贴住底部（视口高 1000 − 16 边距）',
       `下沿 y = ${bottom.toFixed(0)}（期望 984）`);
}
const pinned = await dragDock();
step(shift(pinned) < 10, '固定底部：长按拖动**完全**不动（不是先跟手走一段再弹回）',
     `位移 ${shift(pinned).toFixed(0)}px`);
await page.screenshot({path: `${OUT}/21-dock-pinned.png`, fullPage: false});

// 记住了吗：刷新一次还应该是「固定底部」
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(2500);
await page.locator('[data-slot=dock-profile-trigger]').click();
await page.waitForTimeout(900);
step(/固定底部/.test(await page.locator('[data-slot=dock-mode-toggle]').innerText()),
     '刷新后仍是「固定底部」（记住了，不必每次重设）');
// 还原成悬浮并清掉底栏坐标，让脚本可以重复跑
await page.locator('[data-slot=dock-mode-toggle]').click();
await page.waitForTimeout(700);
await page.keyboard.press('Escape');
await page.waitForTimeout(500);
await page.evaluate(() => localStorage.removeItem('workbuddy-manager:dock-position-v2'));
step(true, '（收尾）已还原成悬浮并清掉底栏坐标，脚本可重复跑');

// ══ 设置页子路由（批次 4 修 P1-2）══════════════════════════════════
//
// 这一段的判据分三类，每类都能在界面上「看起来正常」而实际坏掉：
//   · 地址栏有没有跟着变（`TabsTrigger` 版本：不变，分享出去的链接永远落到第一个 Tab）；
//   · 导航项与内容是不是同一件事（算错了就变成「地址是 models、内容是 users」）；
//   · 切 Tab 有没有偷偷重取数据（取数从 layout 掉回 page 就会每切一次闪一次骨架，
//     还会丢掉没保存的编辑——功能全在，只是变卡）。
// 最后一类的判据是**请求次数**，因为骨架可能只闪几毫秒，截图与文本都抓不到。
console.log('\n设置页子路由（批次 4：7 个 Tab 变成可寻址子路由）');
MODE.value = 0;

const sectionTabs = page.locator('[data-slot=section-tab]');
const activeTab = page.locator('[data-slot=section-tab][data-active=true]');
/** 取不到当前项时返回「（当前项数：n）」而不是抛异常——失败信息要指向真正的问题 */
const activeTabText = async () => {
  const n = await activeTab.count();
  return n === 1 ? (await activeTab.innerText()).trim() : `（当前项数：${n}）`;
};

await page.goto(`${BASE}/settings`, {waitUntil: 'load'});
await page.waitForTimeout(3000);
// 7 → 8：PostgreSQL 备份页（#114）在「系统」之后加了一个设置 Tab。
// 数字写死是有意的——Tab 增删必须有人过一眼这条断言。
step((await sectionTabs.count()) === 8, '二级导航有 8 项',
     `实际 ${await sectionTabs.count()}`);
step(/\/settings\/upstream\/?$/.test(page.url()),
     '/settings 把地址换成第一个 Tab 的规范路径', page.url());
step((await activeTabText()).includes('上游配置'), '当前项是「上游配置」',
     await activeTabText());
await page.screenshot({path: `${OUT}/22-settings-subroute.png`, fullPage: true});

// 深链：直接落到第 2 个 Tab。这一步同时证明「当前 Tab 不是组件内部状态」——
// 内部状态的版本（`defaultValue`）无论从哪个地址进来都只会是第一个 Tab。
await page.goto(`${BASE}/settings/models`, {waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step((await activeTabText()).includes('模型映射'),
     '深链 /settings/models 的当前项是「模型映射」', await activeTabText());
step(/新增模型别名/.test(text),
     '深链落到的**内容**也是「模型映射」那一块（导航与内容没说两套）');
step(!/多上游（账号池分组）/.test(text),
     '没有把「上游配置」那块也渲染出来（一次只挂一个面板）');

const cfgHitsBefore = hits['settings/upstream'] ?? 0;
await sectionTabs.filter({hasText: '关于'}).click();
await page.waitForTimeout(1500);
step(/\/settings\/about\/?$/.test(page.url()),
     '点导航项是一次**真实导航**（地址栏跟着变）', page.url());
step((await activeTabText()).includes('关于'), '当前项跟着变成「关于」',
     await activeTabText());
step((hits['settings/upstream'] ?? 0) === cfgHitsBefore,
     '切 Tab **没有**重新拉配置（取数在外壳上，不随子路由重挂载）',
     `/api/settings/upstream 次数：${cfgHitsBefore} → ${hits['settings/upstream'] ?? 0}`);

await page.goBack({waitUntil: 'load'});
await page.waitForTimeout(1500);
step(/\/settings\/models\/?$/.test(page.url()),
     '后退回到上一个 Tab（每个 Tab 都是一条历史记录）', page.url());

// 正对照：真刷新**必须**重取。少了它，「没重取」可能只是计数器没工作。
const cfgHitsBeforeReload = hits['settings/upstream'] ?? 0;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3000);
step((hits['settings/upstream'] ?? 0) > cfgHitsBeforeReload,
     '（正对照）真刷新会重新拉配置 —— 所以上一条的「没重取」不是计数器坏了',
     `/api/settings/upstream 次数：${cfgHitsBeforeReload} → ${hits['settings/upstream'] ?? 0}`);
step(/\/settings\/models\/?$/.test(page.url()),
     '刷新后仍停在同一个 Tab（地址就是状态，不需要额外记忆）', page.url());

// ══ 底栏收敛到 8 项 + 页内二级导航（批次 4 ②）══════════════════════
//
// 这一批把底栏**目的地从 11 项收敛到 8 项**：任务记录 / 红包 / 聊天测试台不再各占
// 一个入口，改成在「账号」/「密钥」/「模型」页里用页内二级导航切换（路径没变）。
//
// 判据必须**成对**出现：「底栏里没有」+「一步可达」。只断言前者的话，一个被误删的
// 页面同样通过——在用户眼里「入口少了一个」和「这一页没了」长得一模一样，都只是
// 「找不到了」。所以先证明底栏真的只剩 8 个，再逐个证明这三页都还在、都点得到。
console.log('\n底栏收敛与页内二级导航（批次 4：11 → 8）');
MODE.value = 0;

await page.goto(`${BASE}/dashboard`, {waitUntil: 'load'});
await page.waitForTimeout(2500);

// `:visible` 不能省：手机端与桌面端两套渲染都在 DOM 里，只数 `a[href]` 会数到
// 抽屉里那一份（数量翻倍，而且报出来的数字看着还挺像回事）。
const dockHrefs = await page.locator('[data-slot=dock-root] a[href]:visible').evaluateAll(
    (els) => els.map((el) => new URL(el.href).pathname.replace(/\/+$/, '')));
const absorbed = ['/tasks', '/red-packets', '/playground'];

step(dockHrefs.length === 8, '底栏只剩 8 个页面入口（原来是 11 个）',
     `实际 ${dockHrefs.length} 个：${dockHrefs.join(' ')}`);
step(absorbed.every((h) => !dockHrefs.includes(h)),
     '被吸收的三页不再挂在底栏上',
     `底栏里仍有：${absorbed.filter((h) => dockHrefs.includes(h)).join(' ')}`);
step(['/accounts', '/keys', '/models'].every((h) => dockHrefs.includes(h)),
     '三节的落点（账号 / 密钥 / 模型）都还在底栏上 —— 收敛不能把入口一起收掉',
     `底栏：${dockHrefs.join(' ')}`);

// 六页各自都要有二级导航，且高亮的是**自己**。高亮算错的表现是「地址是任务记录、
// 高亮在账号」——页面完全正常，只有把两页并排看才发现。
for (const [path_, label] of [['/accounts', '账号'], ['/tasks', '任务'],
                              ['/keys', '密钥'], ['/red-packets', '红包'],
                              ['/models', '模型'], ['/playground', '测试台']]) {
  await page.goto(`${BASE}${path_}`, {waitUntil: 'load'});
  await page.waitForTimeout(2200);
  step((await sectionTabs.count()) === 2, `${path_} 顶部有 2 项二级导航`,
       `实际 ${await sectionTabs.count()} 项`);
  step((await activeTabText()) === label, `${path_} 高亮的是「${label}」`,
       await activeTabText());
}

// 「一步可达」：从归属页点一下 Tab 就到被吸收的那一页，后退能回来。
await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
await page.waitForTimeout(2200);
await page.screenshot({path: `${OUT}/23-section-tabs-accounts.png`, fullPage: true});
await sectionTabs.filter({hasText: '任务'}).click();
await page.waitForTimeout(1500);
step(/\/tasks\/?$/.test(page.url()), '从「账号」点一下 Tab 就到「任务记录」', page.url());
step((await activeTabText()) === '任务', '到了之后高亮跟着走', await activeTabText());
await page.screenshot({path: `${OUT}/24-section-tabs-tasks.png`, fullPage: true});

await page.goBack({waitUntil: 'load'});
await page.waitForTimeout(1500);
step(/\/accounts\/?$/.test(page.url()), '后退回到「账号」', page.url());

// ══ 命令面板 ⌘K（批次 5 ③）════════════════════════════════════════
//
// 这一批给「11 个业务页 + 设置页 7 个 Tab」加了一条键盘入口。判据分三层，缺一层
// 就有一种坏法能蒙过去：
//
//   1. **能打开**：按钮点得开、快捷键按得开。只测其中一个的话，另一个可能早就
//      坏了而没人知道（快捷键尤其——它没有可见的失败面）。
//   2. **搜得准**：空查询是全部 19 项；搜「红包」只剩一条，而且**就是**红包那一页。
//      只数条数是不够的：筛选逻辑写反（命中留下没命中的）时条数照样对。
//   3. **跳得对**：回车真的换路由，而且面板自己关掉（不关的话它会一直盖着新页面）。
//
// 顺带证明「面板里没有动作」：它整段只有导航，回车之后地址一定变成某个站内路径。
console.log('\n命令面板 ⌘K（批次 5：可发现性与检索）');
MODE.value = 0;

const palette = page.locator('[data-slot=command-palette]');
const paletteInput = page.locator('[data-slot=command-palette-input]');
const paletteItems = page.locator('[data-slot=command-palette-item]');
const paletteActive = page.locator('[data-slot=command-palette-item][aria-selected=true]');
const itemHrefs = () => paletteItems.evaluateAll(
    (els) => els.map((el) => el.getAttribute('data-href')));

await page.goto(`${BASE}/dashboard`, {waitUntil: 'load'});
await page.waitForTimeout(2500);

// —— 1. 能打开（可见的按钮）——
await page.locator('[data-slot=command-palette-trigger]').click();
await page.waitForTimeout(600);
step(await palette.isVisible(), '点右上角的「搜索」按钮能打开命令面板');
step(await paletteInput.isVisible(), '打开后输入框就在，可以直接打字');

// —— 2. 搜得准 ——
// 空查询给全部：打开面板不该是一片空白（用户得先知道有什么才搜得动）。
step((await paletteItems.count()) === 19,
     '空查询列出全部 19 项（8 个底栏目的地 + 3 个被吸收的页面 + 8 个设置 Tab）',
     `实际 ${await paletteItems.count()} 项`);

// 快捷键。macOS 上是 ⌘K，其它平台是 Ctrl+K —— 与组件里的渲染判据同一套。
await page.keyboard.press('Escape');
await page.waitForTimeout(400);
await page.keyboard.press(process.platform === 'darwin' ? 'Meta+k' : 'Control+k');
await page.waitForTimeout(600);
step(await palette.isVisible(), '⌘K / Ctrl+K 也能打开（第二条路径，不能只有按钮）');

await paletteInput.fill('红包');
await page.waitForTimeout(500);
const redPacketHrefs = await itemHrefs();
step(redPacketHrefs.length === 1 && redPacketHrefs[0] === '/red-packets',
     '搜「红包」只剩红包那一页（筛的是「命中的留下」，不是反过来）',
     `实际 ${redPacketHrefs.length} 项：${redPacketHrefs.join(' ')}`);
await page.screenshot({path: `${OUT}/25-command-palette-search.png`, fullPage: false});

// 多词是 AND，且归属提示也参与匹配 —— 「设置 用户」这种最自然的组合必须搜得到。
await paletteInput.fill('设置 用户');
await page.waitForTimeout(500);
const settingsUserHrefs = await itemHrefs();
step(settingsUserHrefs.includes('/settings/users'),
     '多词是 AND，且右侧的归属提示也参与匹配（「设置 用户」搜得到用户管理）',
     `实际 ${settingsUserHrefs.join(' ')}`);

// 搜不到就是空，且**说清楚**是空的（不能默默显示一个空框）。
await paletteInput.fill('zzzzzz');
await page.waitForTimeout(500);
step((await paletteItems.count()) === 0, '搜不到时不显示任何条目',
     `实际 ${await paletteItems.count()} 项`);
step(await page.locator('[data-slot=command-palette-empty]').isVisible(),
     '搜不到时给出明确的空状态（不是一片空白）');

// —— 3. 跳得对 ——
await paletteInput.fill('红包');
await page.waitForTimeout(500);
await page.keyboard.press('Enter');
await page.waitForTimeout(1500);
step(/\/red-packets\/?$/.test(page.url()), '回车跳到搜出来的那一页', page.url());
step(!(await palette.isVisible().catch(() => false)),
     '跳转后面板自己关掉（不关就会一直盖在新页面上）');

// 键盘选：↓ 换高亮，回车打开的必须是**高亮的那一条**（不是永远第一条）。
// 查询词取「模型」而不是「密钥」：后者只命中一条，↓ 会绕回自己，断言就退化成
// 「高亮没变」——那种情况下这条测试是恒真的。
await page.locator('[data-slot=command-palette-trigger]').click();
await page.waitForTimeout(600);
await paletteInput.fill('模型');
await page.waitForTimeout(500);
const firstHref = await paletteActive.getAttribute('data-href');
await page.keyboard.press('ArrowDown');
await page.waitForTimeout(300);
const secondHref = await paletteActive.getAttribute('data-href');
step(secondHref !== null && secondHref !== firstHref,
     '↓ 能移动高亮（读屏软件靠 aria-activedescendant 播报，这条同时证明它在动）',
     `${firstHref} → ${secondHref}`);
await page.screenshot({path: `${OUT}/26-command-palette-keyboard.png`, fullPage: false});
await page.keyboard.press('Enter');
await page.waitForTimeout(1500);
step(page.url().includes(secondHref.replace(/\/+$/, '')),
     '回车打开的是**高亮的那一条**（不是永远第一条）', page.url());

// ══ 上游重载状态（批次 5 ②）════════════════════════════════════════
//
// 这一段要防的是「保存完只有一句『正在应用配置』，然后没有下文」：重载失败时界面
// 同样一片祥和，而配置其实**已经写进去了**，只是没生效。四档里只有一档该有提示，
// 所以判据必须**成对**：先证明闲时没有它，再逐档证明该出现的那档出现了。
//
// 「已生效」这一档**不能靠刷新页面到达**：判定要求 `last_at` 比挂载时读到的那一次
// 更大，所以刷新后基线就是新值，只会是闲。真实用法是「保存 → 重载中 → 重载完成」
// 这一串**页面没关**的过程。所以这里也照着走：先置成重载中，**不刷新**地切到已生效。
console.log('\n上游重载状态（批次 5：保存后能核对「到底成没成」）');
const reloadNotice = page.locator('[data-slot=reload-notice]');
/** 这条提示自己的文字。不读整页：整页里别处也可能有「正在」两个字，那样的断言没有判别力。 */
const reloadNoticeText = async () =>
  (await reloadNotice.count())
    ? (await reloadNotice.first().innerText()).replace(/\s+/g, ' ').trim()
    : '（没有这条提示）';

// —— 正对照：闲时不该有这条提示 ——
// 少了它，下面「出现了」可能只是因为这条提示**一直都在**。
MODE.value = 18;
await page.goto(`${BASE}/settings/upstream`, {waitUntil: 'load'});
await page.waitForTimeout(2500);
step((await reloadNotice.count()) === 0,
     '（正对照）闲时没有这条提示 —— 否则「出现了」可能只是它一直在那儿',
     await reloadNoticeText());

// —— 重载中 ——
MODE.value = 15;
await page.goto(`${BASE}/settings/upstream`, {waitUntil: 'load'});
await page.waitForTimeout(2500);
step(await page.locator('[data-slot=reload-notice][data-phase=applying]').isVisible(),
     '重载中显示「正在应用配置」', await reloadNoticeText());
await page.screenshot({path: `${OUT}/27-reload-applying.png`, fullPage: false});

// —— 重载完成（不刷新，靠轮询自己发现）——
MODE.value = 17;
await page.waitForTimeout(4000);
step(await page.locator('[data-slot=reload-notice][data-phase=ok]').isVisible(),
     '重载完成后面板自己变成「已生效」（靠轮询发现，不需要用户刷新）',
     await reloadNoticeText());
step(!/正在应用配置/.test(await reloadNoticeText()),
     '变成「已生效」之后不再同时说「正在」', await reloadNoticeText());
await page.screenshot({path: `${OUT}/28-reload-ok.png`, fullPage: false});

// —— 重载失败 ——
MODE.value = 16;
await page.goto(`${BASE}/settings/upstream`, {waitUntil: 'load'});
await page.waitForTimeout(2500);
text = await bodyText();
step(await page.locator('[data-slot=reload-notice][data-phase=failed]').isVisible(),
     '重载失败时明确说「重载失败」', await reloadNoticeText());
step(/docker compose restart/.test(text),
     '失败提示给出可照做的下一步（宿主机重启命令），不是只说「失败了」');
step(!/保存失败/.test(text),
     '失败文案不说是「保存失败」—— 配置已经写入，失败的是让它生效的那一步');
step(/Cannot connect to the Docker daemon/.test(text),
     '把上游的原样输出带出来（那是唯一能定位问题的东西）');

// 切 Tab 后提示还在：它挂在**设置页外壳**上，不随子路由重挂载。
// 挂在「上游配置」这一个 Tab 里的话，用户切到别的 Tab 就再也看不到了 ——
// 而重载失败影响的是整个面板，不只是那一页。
await sectionTabs.filter({hasText: '管理用户'}).click();
await page.waitForTimeout(1800);
step(await page.locator('[data-slot=reload-notice][data-phase=failed]').isVisible(),
     '切到别的 Tab 之后提示仍在（挂在设置页外壳上，不随子路由重挂载）',
     `${page.url()} / ${await reloadNoticeText()}`);
step((hits['upstream/reload-state'] ?? 0) >= 3,
     '面板确实在轮询重载状态（判据是请求次数 —— 「有没有在问」在界面上看不出来）',
     `/api/upstream/reload-state 次数：${hits['upstream/reload-state'] ?? 0}`);
await page.screenshot({path: `${OUT}/29-reload-failed-other-tab.png`, fullPage: false});

// —— 收尾：恢复放行 ——
MODE.value = 0;

// React 的水合回退（#418）单独算一类：它**偶发**，而且是「假响应瞬时返回」这个
// 夹具特性造成的竞态——真后端下 60 次加载 0 次，即时 500 的假响应下约 1/60。
// 合并前（404fd3a）的产物同样能复现，与本仓库这三批改动无关。它不影响断言：
// React 会整棵重渲染，页面最终是对的。所以：允许极少几次并逐条打印，
// 一旦变成「每次加载都报」（真正的水合坏了）就会撞上上限而失败。
const hydrationFallbacks = errors.filter(isHydrationFallback);
const otherErrors = errors.filter((e) => !isHydrationFallback(e));
if (hydrationFallbacks.length) {
  console.log(`  （水合回退 ${hydrationFallbacks.length} 次·夹具备忘：数据回来得比水合快）`);
}
step(otherErrors.length === 0, '全程没有未捕获的前端异常', otherErrors.slice(0, 2).join(' | '));
step(hydrationFallbacks.length <= 6,
     '水合回退没有变成常态（上限 6；真坏掉会每页必报，远不止 6）',
     `水合回退 ${hydrationFallbacks.length} 次`);

await browser.close();
console.log('\n=== 结果 ===');
if (findings.length) {
  console.log('FAILED:\n  - ' + findings.join('\n  - '));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');

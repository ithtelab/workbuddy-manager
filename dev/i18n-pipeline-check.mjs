/**
 * 展示层文案管线的**端到端**检查（开发工具，不参与构建、不影响发版）。
 *
 * 为什么需要它：`web/app/(main)/tasks/page.tsx` 的「结果」列由一段接力拼成 ——
 * 一键执行历史模板 → 积分流水模板 → 服务端模板 → 短语表兜底。这套接力只在
 * **真实浏览器里渲染出来**才看得见结果，而静态导出的 HTML 永远是源语言，抓 HTML
 * 什么也验不到；Python 侧的守卫只看字典，看不到这条链路。
 *
 * 于是直接把真实模块 import 进来跑（jiti 负责 TS → JS，`@/` 别名一并解析）。
 *
 * **断言的是性质，不是译文原文**：译文措辞属于翻译质量，会随润色变化，把某一版
 * 措辞写死只会得到一堆「改个词就红」的假警报，最后没人信它。这里钉的是管线本身
 * 接错才会出现的形态：
 *   · 简体中文下输出必须与原文**逐字相同**（管线是纯展示层替换，源语言不该有差异）；
 *   · 动态数据（账号 / 计数 / code / 脚本名）必须原样出现在输出里，不丢不改；
 *   · 命中模板的语言必须真的产出译文（与简体输出不同），除非该条按设计就是原样透传；
 *   · 任何语言下都不许漏出键名（`tasks.xxx`）或空串。
 *
 *   cd web && node ../dev/i18n-pipeline-check.mjs
 */
import {createRequire} from 'node:module';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// path.resolve（不是 join）：把 `dev/..` 规范化掉。否则 alias 解析出的路径里
// 留着 `..`，与源码里 `./index` 解析出的规范化路径是两个不同的缓存键 ——
// 于是同一份 i18n 模块被加载两次，setActiveLocale 只影响其中一个。
const WEB = path.resolve(HERE, '..', 'web');
const require = createRequire(path.join(WEB, 'package.json'));

/* jiti 的模块缓存按**字面说明符**分键：`@/lib/i18n`、`@/lib/i18n/index.ts`、
 * 绝对路径、源码里相对写的 `./index`，四种写法会得到**四个互不相干的实例**，
 * 各有各的 activeLocale。于是在一个上 setActiveLocale('en')，被测代码里那个仍停在
 * zh-CN —— 断言全红，而红的是脚手架。（webpack 打包时这些都指向同一个模块，
 * 只有本脚本会遇上；是脚手架问题，不是应用问题。）
 *
 * 解法：把所有可能被源码用到的入口写法都实例化一遍，切语言时**逐个**设置。
 * 比"想办法只留一个实例"可靠 —— 后者依赖 jiti 内部的解析细节，版本一变就悄悄失效，
 * 而这种失效的表现又是"测试全绿"（译文没生效时只有依赖译文的断言会红，容易被当成
 * 文案问题忽略过去）。 */
const I18N_ENTRY = path.join(WEB, 'lib', 'i18n', 'index.ts');
const jiti = require('jiti')(path.join(WEB, 'node_modules', 'jiti', 'lib', 'jiti.cjs'), {
  interopDefault: true,
  alias: {'@': WEB},
});

const i18nInstances = [];
{
  const seen = new Set();
  for (const spec of [I18N_ENTRY, '@/lib/i18n', '@/lib/i18n/index.ts',
                      path.join(WEB, 'lib', 'i18n')]) {
    try {
      const mod = jiti(spec);
      if (!seen.has(mod)) { seen.add(mod); i18nInstances.push(mod); }
    } catch { /* 该形态解析不出来就算了，别的形态仍然覆盖 */ }
  }
}
/** 切换语言：所有实例一起改，避免踩上面那个缓存坑。 */
function setLocale(loc) {
  for (const inst of i18nInstances) inst.setActiveLocale(loc);
}

const taskrun = jiti(path.join(WEB, 'lib', 'i18n', 'taskrun.ts'));
// 结果列的渲染逻辑只有一份实现（lib/tasklog-text.ts），页面与这里都走它 ——
// 脚本自己重写一遍的话，两边漂移时脚本照样绿，等于没测。
const taskText = jiti(path.join(WEB, 'lib', 'tasklog-text.ts'));

/** 结果列的完整接力，与 page.tsx 里 `taskLogResultText(l)` 同一个函数。 */
function resultText(message) {
  return taskText.taskLogResultText({message});
}

/* ── 用例 ─────────────────────────────────────────────────────────────
 * data: 必须原样出现在输出里的动态片段（账号 / 计数 / code / 脚本名）。
 * passthrough: true 表示这一条按设计就**不该**被翻译（自由文本、未命中模板），
 *              此时非中文语言里出现汉字是正确的行为。
 * 原文取自各写入点（taskrun.py / tasklog.py / accounts.py / tencent.py）。 */
const CASES = [
  {label: '一键领奖历史', raw: '一键领奖（ALL）完成', data: ['ALL']},
  {label: '一键做任务历史 + 退出码', raw: '一键做任务（ALL）退出码 0', data: ['ALL', '0']},
  {label: '单账号历史 + 非零退出码', raw: '一键做任务（abcd1234）退出码 1', data: ['abcd1234', '1']},
  {label: '历史里的失败摘要', raw: '一键做任务（ALL）失败：脚本不存在', data: ['ALL'],
   passthrough: ['脚本不存在']},
  {label: '积分流水', raw: '余额 +100（200 → 300）', data: ['100', '200', '300']},
  {label: '积分流水 + 昵称', raw: '余额 +100（200 → 300） · 昵称A', data: ['100', '200', '300'],
   passthrough: ['昵称A']},
  {label: '脚本任务成功（脚本名是数据）', raw: '执行成功（开学季）', data: [], passthrough: ['开学季']},
  {label: '脚本任务失败（异常是数据）', raw: '执行失败（网络超时）', data: [], passthrough: ['网络超时']},
  {label: '签到汇总（整条模板句）',
   raw: '本轮签到完成：共 5 个，成功 3，已签到 1，失败 1，跳过 0',
   data: ['5', '3', '1', '0']},
  {label: '签到失败明细（code 是数据）', raw: '签到返回 code=40001', data: ['40001']},
  // 传的是页面真正读到的那一份：task_logs 有 message_cn（服务端归一化过标点，
  // 见 tasklog.py 的 cn_text），页面取 `message_cn || message`，所以这里用全角冒号
  // 的形态。半角的原始 message 只在没有 message_cn 时才被读到。
  {label: '阶段名 + 异常文本', raw: '刷新令牌失败：invalid_grant', data: ['invalid_grant']},
  // 专门走 `?? i18n.tp(...)` 那条兜底分支：不属于任何模板，但在短语表里有译文。
  // PR #24 的键名泄漏正是出在这一环（把 tp() 当取值函数传进模板，拿模板键去查
  // 短语表）—— 所以必须有用例踩在这条路上，否则改坏了也测不出来。
  {label: '短语表兜底（无模板命中）', raw: '签到成功', data: []},
  {label: '未命中的服务端文案', raw: '完全没见过的服务端文案', passthrough: ['完全没见过的服务端文案']},
];

const LOCALES = ['zh-CN', 'zh-TW', 'en', 'ja', 'ko'];
const HAN = /[\u3400-\u4dbf\u4e00-\u9fff]/;
/** 键名泄漏的形态：`tasks.xxx` 这种点分小写标识，正常译文里不会出现。 */
const KEY_LEAK = /(^|[\s(（])[a-z][a-zA-Z0-9]*(?:\.[a-zA-Z0-9]+)+/;

let bad = 0;
function check(ok, label, detail) {
  if (ok) return true;
  bad++;
  console.log(`  ✗  ${label}`);
  if (detail) console.log(`      ${detail}`);
  return false;
}

/** 摘掉按设计原样透传的片段后再看有没有汉字，避免「数据里本来就有中文」被误判。 */
function withoutPassthrough(text, fragments = []) {
  let out = text;
  for (const frag of fragments) out = out.split(frag).join('');
  return out;
}

console.log('=== 结果列（tasks/page.tsx 的 resultText 接力）===');
const zhOutputs = new Map();
for (const c of CASES) {
  setLocale('zh-CN');
  const zh = resultText(c.raw);
  zhOutputs.set(c.label, zh);
  // 源语言：管线是**纯展示层替换**，简体下必须与原文逐字一致
  check(zh === c.raw, `[zh-CN] ${c.label}`, `原文: ${c.raw}\n      实得: ${zh}`);
}

for (const loc of LOCALES.filter((l) => l !== 'zh-CN')) {
  console.log(`\n--- ${loc} ---`);
  for (const c of CASES) {
    setLocale(loc);
    const got = resultText(c.raw);
    const label = `[${loc}] ${c.label}`;
    if (!got || !got.trim()) { check(false, label, '输出为空'); continue; }
    if (KEY_LEAK.test(got)) { check(false, label, `疑似键名泄漏: ${got}`); continue; }
    let ok = true;
    // 动态数据必须原样保留
    for (const d of c.data ?? []) {
      if (!got.includes(d)) { ok = check(false, label, `丢了数据片段 ${JSON.stringify(d)} → ${got}`); break; }
    }
    if (!ok) continue;
    // 透传片段必须逐字留下（翻了反而不好跟上游日志对照）
    for (const frag of c.passthrough ?? []) {
      if (!got.includes(frag)) { ok = check(false, label, `透传片段被改动 ${frag} → ${got}`); break; }
    }
    if (!ok) continue;
    // 该翻译的必须真的翻了
    if (!c.passthrough?.length && got === zhOutputs.get(c.label)) {
      ok = check(false, label, `与简体输出完全相同，模板没生效: ${got}`);
    }
    if (ok && loc === 'en' && HAN.test(withoutPassthrough(got, c.passthrough))) {
      ok = check(false, label, `英文里出现汉字（非透传片段）: ${got}`);
    }
    if (ok) console.log(`  ✓  ${c.label}`);
  }
}

/* ── 面板日志行（taskrun.ts）────────────────────────────────────────── */
console.log('\n=== 面板日志行（taskrun.ts）===');
const LINE_CASES = [
  {label: 'query 已领跳过', raw: '[task_runner] abcd1234 t001: query claimed(3/5) -> 已领，跳过',
   data: ['[task_runner]', 'abcd1234', 't001', '3/5']},
  // 上游 2026-09-23（ee3c694 / 9a26ae7）成长任务续作引入的两行：对象 id 池为空、
  // 或数量不够本轮目标。任务类型（miniexpert）是数据，必须原样留着。
  {label: '对象池为空：跳过点亮',
   raw: '[task_runner] abcd1234 t009: miniexpert 无可用对象 id，WARN 跳过点亮',
   data: ['[task_runner]', 'abcd1234', 't009', 'miniexpert']},
  {label: '对象池不够本轮目标',
   raw: '[task_runner] abcd1234 t009: 可用对象 id 3 < 需 5，本轮按可用数上报',
   data: ['[task_runner]', 'abcd1234', 't009', '3', '5']},
  // 上游 2026-09-24（06878fd / 1580a86）成长任务续作新增：任务未到上线时间。
  // 解锁时间是数据，形态可能是时间戳（带空格）也可能是 "?"。
  {label: '任务未到上线时间（解锁时间戳带空格）',
   raw: '[task_runner] abcd1234 t011: query locked（解锁 2026-09-25 09:00:00）-> 未到上线时间，跳过',
   data: ['[task_runner]', 'abcd1234', 't011', '2026-09-25 09:00:00']},
  {label: '任务未到上线时间（无解锁时间）',
   raw: '[task_runner] abcd1234 t011: query locked（解锁 ?）-> 未到上线时间，跳过',
   data: ['abcd1234', 't011', '?']},
  {label: '看护追加：空闲超时（我们自己的行）', raw: '!! 已 300s 无输出，判定卡死并终止', data: ['300']},
  {label: '未命中的上游新行', raw: '这条上游新加的行没模板，应当原样返回',
   passthrough: ['这条上游新加的行没模板，应当原样返回']},
];

const lineZh = new Map();
for (const c of LINE_CASES) {
  setLocale('zh-CN');
  const zh = taskrun.translateRunLine(c.raw);
  lineZh.set(c.label, zh);
  check(zh === c.raw, `[zh-CN] ${c.label}`, `原文: ${c.raw}\n      实得: ${zh}`);
}

for (const loc of LOCALES.filter((l) => l !== 'zh-CN')) {
  for (const c of LINE_CASES) {
    setLocale(loc);
    const got = taskrun.translateRunLine(c.raw);
    const label = `[${loc}] ${c.label}`;
    if (!got || !got.trim()) { check(false, label, '输出为空'); continue; }
    if (KEY_LEAK.test(got)) { check(false, label, `疑似键名泄漏: ${got}`); continue; }
    let ok = true;
    for (const d of c.data ?? []) {
      if (!got.includes(d)) { ok = check(false, label, `丢了数据片段 ${d} → ${got}`); break; }
    }
    if (ok && !c.passthrough?.length && got === lineZh.get(c.label)) {
      ok = check(false, label, `与简体输出完全相同，模板没生效: ${got}`);
    }
    if (ok && loc === 'en' && HAN.test(withoutPassthrough(got, c.passthrough))) {
      ok = check(false, label, `英文里出现汉字（非透传片段）: ${got}`);
    }
    if (ok) console.log(`  ✓  ${label} → ${got}`);
  }
}

console.log(bad ? `\n共 ${bad} 处问题` : '\n全部通过');
process.exit(bad ? 1 : 0);

/**
 * `web/lib/tap.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/tap.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_tap_web`
 *
 * 为什么值得单独测：抓包页上这些都是「错了也不报错」的展示逻辑 ——
 *   · 「上一轮」算错 → 差异视图对比的是两条不相干的请求，用户会以为模型行为突变；
 *   · 差异为空的判定漏一类 → 明明一样的请求却显示「有 N 处变化」；
 *   · 耗时为 null 时显示成 0ms → 非流式请求看起来「瞬间完成」；
 *   · 字节数为 0 时显示成 `NaN`。
 */
import {
  clientLabel,
  diffCount,
  diffIsEmpty,
  fmtBytes,
  fmtMs,
  prevTurn,
  sessionTitle,
  statusTone,
  summaryText,
} from './tap.ts';

let failed = 0;

function check(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) {
    console.log(`  ok  ${name}`);
  } else {
    console.log(`  FAIL ${name}\n       got  ${g}\n       want ${w}`);
    failed += 1;
  }
}

/* ── 格式化 ───────────────────────────────────────────────────── */

check('字节：0 与空值都显示 0 B（不是 NaN）', [fmtBytes(0), fmtBytes(null), fmtBytes(undefined)],
      ['0 B', '0 B', '0 B']);
check('字节：KB', fmtBytes(2048), '2.0 KB');
check('字节：MB', fmtBytes(3 * 1024 * 1024), '3.00 MB');
check('耗时：毫秒', fmtMs(340), '340ms');
check('耗时：秒', fmtMs(1234), '1.2s');
check('耗时：未采集显示破折号（不是 0ms）', [fmtMs(null), fmtMs(undefined), fmtMs(-1)],
      ['—', '—', '—']);

/* ── 会话与轮次 ───────────────────────────────────────────────── */

const session = {
  id: 1, fingerprint: 'abcdef1234567890ab', key_id: null, key_name: null,
  client: 'claude', protocol: 'chat', model: 'gpt-4o', started_at: 0, updated_at: 0,
  turns: 3, prompt_tokens: 0, completion_tokens: 0,
};
check('会话标题带模型、轮次与短指纹', sessionTitle(session), 'gpt-4o · 3 轮 · abcdef');
check('客户端家族：已知', ['claude', 'codex', 'cursor', 'cli'].map(clientLabel),
      ['Claude Code', 'Codex', 'Cursor', 'CLI']);
check('客户端家族：未知值原样显示', clientLabel('weird-ua'), 'weird-ua');
check('客户端家族：空值给「未知」', [clientLabel(null), clientLabel('')], ['未知', '未知']);

check('摘要：空值给破折号', summaryText(undefined), '—');
check('摘要：三个数字都出现', summaryText({messages: 12, tools: 3, system_chars: 900, roles: {}, tool_names: []}),
      '12 条消息 / 3 个工具 / system 900 字');

/* ── 差异判定 ─────────────────────────────────────────────────── */

const EMPTY_DIFF = {
  before: {id: 1, seq: 1, ts: 0, model: null}, after: {id: 2, seq: 2, ts: 0, model: null},
  params: [], system: {changed: false, before_chars: 0, after_chars: 0, before: '', after: ''},
  messages: {before_total: 1, after_total: 1, common: 1, added: [], removed: [], changed: []},
  tools: {added: [], removed: [], changed: []},
  summary: {params: 0, system: 0, added: 0, removed: 0, changed: 0, tools: 0},
};
check('无差异：判为空（不能显示成「有变化」）', diffIsEmpty(EMPTY_DIFF), true);
check('未加载：也判为空', [diffIsEmpty(null), diffIsEmpty(undefined)], [true, true]);
check(
  '只有工具变化也算有差异',
  diffIsEmpty({...EMPTY_DIFF, tools: {added: ['x'], removed: [], changed: []},
               summary: {...EMPTY_DIFF.summary, tools: 1}}),
  false,
);
check('差异条数：各项相加', diffCount({...EMPTY_DIFF, summary: {
  params: 1, system: 1, added: 2, removed: 1, changed: 1, tools: 3}}), 9);

/* ── 「上一轮」 ───────────────────────────────────────────────── */

const turns = [{id: 10, seq: 1}, {id: 11, seq: 2}, {id: 12, seq: 3}];
check('上一轮：取 seq 前一项', prevTurn(turns, 2)?.id, 10);
check('上一轮：最后一轮取到倒数第二轮', prevTurn(turns, 3)?.id, 11);
check('上一轮：第一轮没有上一轮', prevTurn(turns, 1), null);
check('上一轮：找不到该 seq 时返回 null', prevTurn(turns, 99), null);
check('上一轮：单轮会话', prevTurn([{id: 1, seq: 1}], 1), null);

/* ── 状态色 ───────────────────────────────────────────────────── */

check('状态色', [200, 204, 400, 404, 500, null].map(statusTone),
      ['ok', 'ok', 'warn', 'warn', 'error', 'error']);

if (failed) {
  console.log(`\n${failed} 项失败`);
  process.exit(1);
}
console.log('\nall passed');

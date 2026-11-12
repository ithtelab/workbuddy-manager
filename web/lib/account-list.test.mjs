/**
 * `web/lib/account-list.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/account-list.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_account_list`
 * （没有 node 时那条会 skip，不会阻塞后端测试套件）。
 *
 * 为什么值得单独测：这几步全在客户端做（一次请求就把整个分组的账号拿回来了），
 * 所以它们错了**界面一声不响**，只是列表看起来不对：
 *   · 排序把「没有积分数据」的账号当成 0 分 → 排到最前面，看着像「这些号都没钱了」，
 *     而事实是我们根本不知道它们有多少积分；
 *   · 排序顺手改了入参数组 → React 拿到的还是同一个引用，列表不重渲染，
 *     点了排序没反应；
 *   · 分页页码越界不夹回 → 删掉一个账号后停在空页上，显示「第 3 / 2 页」+ 空表，
 *     看起来像数据丢了；
 *   · 搜索把 `uid` 当子串匹配到别的字段上 → 搜出一堆不相干的号。
 * 四条都不会报错。
 */
import {
  groupCounts,
  matchesQuery,
  paginate,
  selectAccounts,
} from './account-list.ts';

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

/** 造一个账号；只填这一批测试真正用到的字段 */
const acct = (file, extra = {}) => ({
  file,
  uid: `uid-${file}`,
  nickname: file,
  realm: 'cn',
  remain_seconds: 1000,
  ...extra,
});

/** 全部归到一组的分组函数（筛选/计数只关心「分到哪一组」，不关心怎么分的） */
const groupOf =
  (map) =>
  (a) =>
    map[a.file] ?? 'usable';

/* ── matchesQuery ─────────────────────────────────────────────── */

const q = (a, kw) => matchesQuery(a, kw);

check('空关键词命中一切（调用方不必在外面判空）', q(acct('a'), ''), true);
check('只有空格的关键词也命中一切', q(acct('a'), '   '), true);
check('命中昵称', q(acct('a', {nickname: '张叔叔'}), '张叔'), true);
check('命中 uid', q(acct('a', {uid: 'sh0001-abc'}), 'sh0001'), true);
check('命中账号文件名', q(acct('sub2api.json'), 'sub2api'), true);
check('命中备注', q(acct('a', {note: '备用号'}), '备用'), true);
check('大小写不敏感', q(acct('a', {nickname: 'Alice'}), 'alice'), true);
check('关键词前后空格忽略', q(acct('a', {nickname: 'Alice'}), '  alice  '), true);
check('都不命中就是 false', q(acct('a', {nickname: 'Alice'}), 'bob'), false);
// 刻意**不**搜的字段：它们在界面上不显示，搜出来用户也不知道命中了什么
check(
  '不搜 enterprise_id（界面上看不到，搜出来无从对照）',
  q(acct('a', {enterprise_id: 'ent-secret'}), 'ent-secret'),
  false,
);

/* ── selectAccounts：筛选 ─────────────────────────────────────── */

const pool = [
  acct('a', {remain_seconds: 300, credits: 500}),
  acct('b', {remain_seconds: 100, credits: null}),
  acct('c', {remain_seconds: 900, credits: 50}),
  acct('d', {remain_seconds: 200, credits: 900}),
];
const GROUPS = {a: 'usable', b: 'attention', c: 'cooling', d: 'stopped'};
const deps = {
  groupOf: groupOf(GROUPS),
  creditOf: (a) => (a.credits === undefined ? null : a.credits),
};
const files = (rows) => rows.map((r) => r.file);

check(
  '状态筛选 all → 全留',
  files(selectAccounts(pool, {q: '', group: 'all', sort: 'default'}, deps)),
  ['a', 'b', 'c', 'd'],
);
check(
  '状态筛选 attention → 只留需处理的',
  files(selectAccounts(pool, {q: '', group: 'attention', sort: 'default'}, deps)),
  ['b'],
);
check(
  '状态筛选 + 搜索是「与」的关系',
  files(selectAccounts(pool, {q: 'a', group: 'attention', sort: 'default'}, deps)),
  [], // 'a' 命中账号 a，但它属于 usable，被状态筛掉
);
check(
  '搜索命中 uid 时也走同一套筛选',
  files(selectAccounts(pool, {q: 'uid-b', group: 'all', sort: 'default'}, deps)),
  ['b'],
);

/* ── selectAccounts：排序 ─────────────────────────────────────── */

check(
  '默认排序 = 后端给的顺序，原样不动',
  files(selectAccounts(pool, {q: '', group: 'all', sort: 'default'}, deps)),
  ['a', 'b', 'c', 'd'],
);
check(
  '按剩余有效期升序（有效期近的在前）',
  files(selectAccounts(pool, {q: '', group: 'all', sort: 'remain'}, deps)),
  ['b', 'd', 'a', 'c'],
);
check(
  '按积分升序，**没有积分数据的排最后**（不是当成 0 分排最前）',
  files(selectAccounts(pool, {q: '', group: 'all', sort: 'credits'}, deps)),
  ['c', 'a', 'd', 'b'],
);

check('默认顺序不受方向影响', files(selectAccounts(pool,
  {q: '', group: 'all', sort: 'default', direction: 'desc'}, deps)), ['a', 'b', 'c', 'd']);
check('有效期支持降序', files(selectAccounts(pool,
  {q: '', group: 'all', sort: 'remain', direction: 'desc'}, deps)), ['c', 'a', 'd', 'b']);
check('积分降序仍将未知余额放最后', files(selectAccounts(pool,
  {q: '', group: 'all', sort: 'credits', direction: 'desc'}, deps)), ['d', 'a', 'c', 'b']);
const additions = [acct('legacy'), acct('old', {added_at: 100}),
  acct('new', {added_at: 300}), acct('same', {added_at: 300}), acct('unknown', {added_at: null})];
check('添加时间默认最新在前，未知历史时间在后', files(selectAccounts(additions,
  {q: '', group: 'all', sort: 'added'}, deps)), ['new', 'same', 'old', 'legacy', 'unknown']);
check('添加时间支持升序，未知仍在后面', files(selectAccounts(additions,
  {q: '', group: 'all', sort: 'added', direction: 'asc'}, deps)), ['old', 'new', 'same', 'legacy', 'unknown']);
check('按添加时间排序仍与搜索结合', files(selectAccounts(additions,
  {q: 'new', group: 'all', sort: 'added', direction: 'desc'}, deps)), ['new']);
check('添加时间排序不改变源数组', files(additions), ['legacy', 'old', 'new', 'same', 'unknown']);

/* ── selectAccounts：自定义顺序（拖拽排序） ───────────────────── */

const manualDeps = {...deps, orderRank: (a) => ({c: 0, a: 1})[a.file] ?? null};
check('自定义顺序按名次排列，未登记的保持原顺序排在最后', files(selectAccounts(pool,
  {q: '', group: 'all', sort: 'manual'}, manualDeps)), ['c', 'a', 'b', 'd']);
check('没有顺序来源时退化成后端顺序（不是随机打乱）', files(selectAccounts(pool,
  {q: '', group: 'all', sort: 'manual'}, deps)), ['a', 'b', 'c', 'd']);
check('自定义顺序同样与搜索结合', files(selectAccounts(pool,
  {q: 'c', group: 'all', sort: 'manual'}, manualDeps)), ['c']);
check('自定义顺序不改动入参数组', files(pool), ['a', 'b', 'c', 'd']);
for (const direction of ['asc', 'desc']) {
  const invalid = [acct('nan', {credits: NaN}), acct('known', {credits: 1}), acct('inf', {credits: Infinity})];
  check(`非有限数值在 ${direction} 下也放最后`, files(selectAccounts(invalid,
    {q: '', group: 'all', sort: 'credits', direction}, deps)), ['known', 'nan', 'inf']);
  const expiry = [acct('unknown', {expires_at: 0, remain_seconds: 0}),
    acct('valid', {expires_at: 2000, remain_seconds: 20}), acct('expired', {expires_at: 1, remain_seconds: 0})];
  check(`未知有效期在 ${direction} 下放最后，过期账号仍按 0 排序`, files(selectAccounts(expiry,
    {q: '', group: 'all', sort: 'remain', direction}, deps)),
    direction === 'asc' ? ['expired', 'valid', 'unknown'] : ['valid', 'expired', 'unknown']);
}

// 稳定性：键值相同的保持后端给的顺序，否则「刷新一下顺序就变了」
const tie = [
  acct('x', {credits: 7}),
  acct('y', {credits: 7}),
  acct('z', {credits: 1}),
];
check(
  '键值相同的保持原顺序（排序稳定）',
  files(selectAccounts(tie, {q: '', group: 'all', sort: 'credits'}, deps)),
  ['z', 'x', 'y'],
);
check('降序时同值仍保持原顺序', files(selectAccounts(tie,
  {q: '', group: 'all', sort: 'credits', direction: 'desc'}, deps)), ['x', 'y', 'z']);

// 入参数组不能被改动：改了的话 React 拿到的还是同一个引用，列表不重渲染
const before = files(pool);
selectAccounts(pool, {q: '', group: 'all', sort: 'remain'}, deps);
check('排序不改动入参数组', files(pool), before);

/* ── groupCounts ─────────────────────────────────────────────── */

check(
  '各状态组的条数（含 all）',
  groupCounts(pool, groupOf(GROUPS)),
  {all: 4, usable: 1, attention: 1, cooling: 1, stopped: 1},
);
check(
  '空列表时每个组都是 0（不是缺键）',
  groupCounts([], groupOf(GROUPS)),
  {all: 0, usable: 0, attention: 0, cooling: 0, stopped: 0},
);

/* ── paginate ────────────────────────────────────────────────── */

const rows10 = Array.from({length: 10}, (_, i) => `r${i}`);

check('第 1 页取前 3 条', paginate(rows10, 1, 3).rows, ['r0', 'r1', 'r2']);
check('第 2 页', paginate(rows10, 2, 3).rows, ['r3', 'r4', 'r5']);
check('total 是**全部**条数而不是这一页的条数', paginate(rows10, 1, 3).total, 10);
check('pages 向上取整（10 / 3 → 4 页）', paginate(rows10, 1, 3).pages, 4);
check(
  '页码越界 → 夹回最后一页，而不是给一张空表',
  paginate(rows10, 9, 3),
  {rows: ['r9'], total: 10, pages: 4, page: 4},
);
check('页码小于 1 → 夹到第 1 页', paginate(rows10, 0, 3).page, 1);
check(
  '空列表 → 1 页、第 1 页、没有行（不是 0 页）',
  paginate([], 1, 3),
  {rows: [], total: 0, pages: 1, page: 1},
);
check('整除时不留空页', paginate(rows10, 5, 5), {rows: ['r5', 'r6', 'r7', 'r8', 'r9'], total: 10, pages: 2, page: 2});

console.log(failed ? `\n${failed} failed` : '\nall passed');
process.exit(failed ? 1 : 0);

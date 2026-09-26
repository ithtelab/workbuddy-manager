/**
 * `mergePoolStatus` / `availabilityOf` 与跨分组聚合那几个纯函数
 * （`accountGroups` / `tagGroup` / `accountKey` / `interleave` / `poolTotals`）的
 * 行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/account-status.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_account_status`
 * （没有 node 时那条会 skip，不会阻塞后端测试套件）。
 *
 * 为什么值得单独测：这几个函数错起来**界面不报错**，只是数字变得不对——
 *   · 池状态取不到时（`connected: false`）判成「不在池里」→ 每个账号都显示
 *     「未加载 / 账号文件可能有问题」，而事实是连不上上游。这条误报曾经真实
 *     发生过，且有心跳的页面每 30 秒会自己「恢复」一次，用户会以为账号随机坏掉；
 *   · 合并时不打分组标记 → 两个分组的同名账号文件撞成同一个 React key，
 *     列表少一条、状态串到别的号上；
 *   · 跨组先拼账号、再拿某一组的 /status 去并 → 其余组的账号整批被标成「未加载」；
 *   · 把「没配账号目录的分组」也算进分母 → 「共 N 个分组」虚高，而那几组的
 *     账号接口必然 409；
 *   · 快照「一组接一组」平铺 → 前 9 格被默认分组占满，其余组一条都看不到，
 *     等于把这次要修的「只报 1/N 个池子」在快照上又演一遍。
 * 五条都不会报错，只会让用户看到一个**看起来很正常**的错数字——正是上游
 * issue #94 问题 2 的形态。
 */
import {
  accountGroups,
  accountKey,
  availabilityOf,
  degradedCount,
  interleave,
  mergePoolStatus,
  poolTotals,
  tagGroup,
} from './account-status.ts';

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
const acct = (file, uid, extra = {}) => ({file, uid, nickname: uid, realm: 'cn', ...extra});

/** 造一份 /status；poolUids 是池里的账号 */
const status = (poolUids, extra = {}) => ({
  connected: true,
  accounts: poolUids.map((uid) => ({uid, healthy: true})),
  ...extra,
});

/** 造一个上游分组条目（/api/upstreams 的一项） */
const group = (id, name, authDir) => ({
  id,
  name,
  auth_dir: authDir,
  is_default: id === null,
  enabled: true,
  base_url: 'http://127.0.0.1:7864',
  has_key: false,
  api_key_masked: '',
  note: '',
  container: '',
  bound_keys: 0,
});

console.log('mergePoolStatus / availabilityOf：上游状态取不到时**不能**判成「不在池里」');
check(
  '上游连不上（connected: false）→ 标 poolUnknown',
  mergePoolStatus([acct('a.json', 'u1')], {connected: false})[0].poolUnknown,
  true,
);
check(
  '上游连不上 → 分档是「未知」而不是「未加载」',
  availabilityOf(mergePoolStatus([acct('a.json', 'u1')], {connected: false})[0]),
  'unknown',
);
check(
  'upstream 整个为 null 也一样（不能因为「没有池」就说账号不在池里）',
  availabilityOf(mergePoolStatus([acct('a.json', 'u1')], null)[0]),
  'unknown',
);
check(
  '上游连上了但池里没这个号 → 才是真的「未加载」',
  availabilityOf(mergePoolStatus([acct('a.json', 'u1')], status([]))[0]),
  'notLoaded',
);
check(
  '在池里 → 在线',
  availabilityOf(mergePoolStatus([acct('a.json', 'u1')], status(['u1']))[0]),
  'online',
);
check(
  '面板主动停用要早于「不在池里」判定（否则会显示成故障）',
  availabilityOf(mergePoolStatus(
    [acct('a.json', 'u1', {disabled_by_panel: true})], status([]))[0],
  ),
  'disabledByPanel',
);
check(
  '上游手动停用位要早于「不在池里」判定',
  availabilityOf(mergePoolStatus(
    [acct('a.json', 'u1')],
    {connected: true, accounts: [{uid: 'u1', healthy: true, manual_disabled: true}]},
  )[0]),
  'manualDisabled',
);
check(
  '「看不到上游」要早于「冷却中」——否则会凭一份过时快照说账号在冷却',
  availabilityOf(mergePoolStatus(
    [acct('a.json', 'u1', {cooling: true})], {connected: false})[0],
  ),
  'unknown',
);

console.log('\naccountGroups：只取「配了本地账号目录」的分组');
check(
  '默认分组 + 一个新分组（都配了目录）都留下',
  accountGroups([group(null, '默认上游', '/data/auths'), group(3, 'B 组', '/data/b')])
    .map((g) => g.name),
  ['默认上游', 'B 组'],
);
check(
  '没配目录的分组被剔除（它只做密钥转发，账号接口会 409）',
  accountGroups([group(null, '默认上游', '/data/auths'), group(3, '只转发', '')])
    .map((g) => g.name),
  ['默认上游'],
);
check('只有空白的目录也算没配', accountGroups([group(3, '只转发', '   ')]), []);

console.log('\ntagGroup：逐组合并池状态，并打上分组标记');
const g1 = tagGroup([acct('a.json', 'u1'), acct('b.json', 'u2')], status(['u1']), null, '默认上游');
check('每条都带上分组（id 与名字）', g1.map((a) => a.group), [
  {id: null, name: '默认上游'},
  {id: null, name: '默认上游'},
]);
check('在池里的账号标 in_pool: true', g1[0].in_pool, true);
check('不在池里的账号标 in_pool: false', g1[1].in_pool, false);
check(
  '池状态取不到时标 poolUnknown（而不是 in_pool: false）',
  tagGroup([acct('a.json', 'u1')], {connected: false}, 3, 'B 组')[0].poolUnknown,
  true,
);
check(
  '池状态取不到时**不**说它不在池里',
  tagGroup([acct('a.json', 'u1')], {connected: false}, 3, 'B 组')[0].in_pool,
  undefined,
);
check(
  'upstream 为 null 也走「未知」这一支',
  tagGroup([acct('a.json', 'u1')], null, 3, 'B 组')[0].poolUnknown,
  true,
);
check(
  '逐组合并：B 组的账号不会被 A 组的池快照判成「不在池里」',
  tagGroup([acct('x.json', 'u9')], status(['u9']), 3, 'B 组')[0].in_pool,
  true,
);

console.log('\naccountKey：同名账号文件在不同分组里必须是两个 key');
const a1 = tagGroup([acct('sub2api.json', 'u1')], status(['u1']), null, '默认上游')[0];
const a2 = tagGroup([acct('sub2api.json', 'u2')], status(['u2']), 3, 'B 组')[0];
check('默认分组用 default 前缀', accountKey(a1), 'default:sub2api.json');
check('非默认分组用 id 前缀', accountKey(a2), '3:sub2api.json');
check(
  '同一个文件名、不同分组 → key 不同（否则 React 会把两条当成一条）',
  accountKey(a1) === accountKey(a2),
  false,
);

console.log('\ninterleave：按分组交错，避免某一组把前 9 格占满');
const slice = (gid, files) => ({
  gid,
  name: `g${gid}`,
  accounts: files.map((f) => ({file: f, uid: f, group: {id: gid, name: `g${gid}`}})),
  upstream: null,
  error: null,
});
check(
  '两组等长 → 轮流取',
  interleave([slice(1, ['a1', 'a2']), slice(2, ['b1', 'b2'])]).map((a) => a.file),
  ['a1', 'b1', 'a2', 'b2'],
);
check(
  '两组不等长 → 短的取完后剩下的接着出',
  interleave([slice(1, ['a1', 'a2', 'a3']), slice(2, ['b1'])]).map((a) => a.file),
  ['a1', 'b1', 'a2', 'a3'],
);
check('空切片不影响', interleave([slice(1, []), slice(2, ['b1'])]).map((a) => a.file), ['b1']);
check('全空 → 空数组', interleave([slice(1, []), slice(2, [])]), []);

console.log('\ndegradedCount：只数当前版本里「窗口还没过」的连败降权');
const future = new Date(Date.now() + 3600_000).toISOString();
const past = new Date(Date.now() - 3600_000).toISOString();
const dslice = {
  gid: 3,
  name: 'B 组',
  upstream: null,
  error: null,
  accounts: [
    {file: 'a', uid: 'a', realm: 'cn', degrade_until: future, group: {id: 3, name: 'B 组'}},
    {file: 'b', uid: 'b', realm: 'global', degrade_until: future, group: {id: 3, name: 'B 组'}},
    {file: 'c', uid: 'c', realm: 'cn', degrade_until: past, group: {id: 3, name: 'B 组'}},
    {file: 'd', uid: 'd', realm: 'cn', group: {id: 3, name: 'B 组'}},
  ],
};
check('只数当前版本、且窗口还没过的', degradedCount(dslice, 'cn'), 1);
check('切到另一个版本时数的是另一条', degradedCount(dslice, 'global'), 1);

console.log('\npoolTotals：优先用按版本分好的计数，老上游退回顶层并如实标注');
const withRealm = {
  connected: true,
  total: 99,
  healthy: 99,
  cooling: 99,
  disabled: 99,
  realm_totals: {cn: {total: 3, healthy: 2, cooling: 1, disabled: 0, in_flight_full: 0}},
};
check(
  '有 realm_totals 时用它（不是顶层那份含两个版本的）',
  poolTotals(withRealm, 'cn'),
  {total: 3, healthy: 2, cooling: 1, disabled: 0, known: true, globalOnly: false},
);
check(
  '老上游没有 realm_totals → 退回顶层并标 globalOnly',
  poolTotals({connected: true, total: 7, healthy: 5, cooling: 1, disabled: 1}, 'cn'),
  {total: 7, healthy: 5, cooling: 1, disabled: 1, known: true, globalOnly: true},
);
check(
  'realm_totals 里没有这个版本 → 同样退回顶层（不能给 0）',
  poolTotals(
    {
      connected: true,
      total: 7,
      realm_totals: {global: {total: 4, healthy: 4, cooling: 0, disabled: 0, in_flight_full: 0}},
    },
    'cn',
  ).total,
  7,
);
check(
  'status 为 null → known 为假（界面据此显示「—」而不是 0）',
  poolTotals(null, 'cn'),
  {total: 0, healthy: 0, cooling: 0, disabled: 0, known: false, globalOnly: false},
);

console.log(failed ? `\n${failed} failed` : '\nall passed');
process.exit(failed ? 1 : 0);

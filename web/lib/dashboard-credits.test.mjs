/**
 * `collectScopedExpiries` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/dashboard-credits.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_dashboard_credit_expiry`
 *
 * 为什么值得单独测：仪表盘的积分余额已经按当前版本过滤，到期明细却曾经扫
 * **全部** uid 的元数据。国内版更早的一笔就会出现在国际版卡片上——金额、
 * 时刻、紧急色一起串，刷新账号也改不掉，因为刷新换的是同一份跨版本的 map。
 */
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {collectScopedExpiries} from './dashboard-credits.ts';
import {groupExpiriesByDay} from './display-prefs.ts';

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

/** 与 dashboard 卡片同一条线：7 天内算紧急。 */
function expiryUrgent(entry, nowSec) {
  return !!entry && entry.at - nowSec < 7 * 86400;
}

/** 本地时区下的「某天 12:00」。 */
function localNoon(y, m, d) {
  return Math.floor(new Date(y, m - 1, d, 12, 0, 0).getTime() / 1000);
}

/** 存量账号 realm 为空视为国内版，与仪表盘 `scopedSlices` 同一判据。 */
function scopedTo(accounts, realm) {
  return accounts.filter((a) => (a.realm ?? 'cn') === realm);
}

const now = 1_800_000_000;
const domesticAt = now + 3600;
const intlAt = now + 30 * 86400;

const pool = [
  {uid: 'cn-1', realm: 'cn'},
  {uid: 'intl-1', realm: 'intl'},
  {uid: 'legacy'},
];
const meta = {
  'cn-1': {expiries: [{at: domesticAt, amount: 596}]},
  'intl-1': {expiries: [{at: intlAt, amount: 250}]},
  legacy: {expiries: [{at: domesticAt + 10, amount: 7}]},
  removed: {expiries: [{at: domesticAt - 100, amount: 1}]},
};

console.log('collectScopedExpiries:');

const intl = collectScopedExpiries(scopedTo(pool, 'intl'), meta);
const domestic = collectScopedExpiries(scopedTo(pool, 'cn'), meta);
check('international keeps its own package', intl, [{at: intlAt, amount: 250}]);
check('domestic keeps its own packages', domestic, [
  {at: domesticAt, amount: 596},
  {at: domesticAt + 10, amount: 7},
]);
check('international urgency ignores the other realm', expiryUrgent(intl[0], now), false);
check('domestic urgency follows its own earliest', expiryUrgent(domestic[0], now), true);

const noon = localNoon(2026, 10, 12);
const dayMeta = {
  a: {expiries: [{at: noon + 3600, amount: 100}, {at: noon, amount: 50}]},
  b: {expiries: [{at: noon + 7200, amount: 100}]},
  other: {expiries: [{at: noon + 1800, amount: 596}]},
};
const dayAccounts = [{uid: 'a'}, {uid: 'b'}];
const dayList = collectScopedExpiries(dayAccounts, dayMeta);
check('same local day sums only the selected packages', groupExpiriesByDay(dayList), [
  {at: noon, amount: 250},
]);

check('empty account list yields nothing', collectScopedExpiries([], meta), []);
check('missing metadata yields nothing', collectScopedExpiries([{uid: 'absent'}], meta), []);
check('omitted expiries yield nothing', collectScopedExpiries([{uid: 'bare'}], {bare: {}}), []);
check('empty expiries yield nothing', collectScopedExpiries([{uid: 'empty'}], {empty: {expiries: []}}), []);
check(
  'metadata for an account outside the list stays out',
  collectScopedExpiries([{uid: 'intl-1'}], meta).some((e) => e.amount === 1),
  false,
);

const dupMeta = {
  shared: {expiries: [{at: 300, amount: 3}, {at: 100, amount: 1}]},
  other: {expiries: [{at: 200, amount: 2}]},
};
const dupAccounts = [{uid: 'other'}, {uid: 'shared'}, {uid: 'shared'}];
check('unique uids, chronological, no double count', collectScopedExpiries(dupAccounts, dupMeta), [
  {at: 100, amount: 1},
  {at: 200, amount: 2},
  {at: 300, amount: 3},
]);

const accountsBefore = JSON.stringify(dupAccounts);
const metaBefore = JSON.stringify(dupMeta);
const expiryOrder = dupMeta.shared.expiries.map((e) => e.at);
collectScopedExpiries(dupAccounts, dupMeta);
check('does not mutate accounts', JSON.stringify(dupAccounts), accountsBefore);
check('does not mutate metadata', JSON.stringify(dupMeta), metaBefore);
check('does not reorder stored expiries', dupMeta.shared.expiries.map((e) => e.at), expiryOrder);

const refreshed = {
  ...meta,
  'intl-1': {expiries: [{at: intlAt + 86400, amount: 80}]},
};
check('refresh replaces the selected package', collectScopedExpiries(scopedTo(pool, 'intl'), refreshed), [
  {at: intlAt + 86400, amount: 80},
]);
const unrelated = {
  ...meta,
  'cn-1': {expiries: [{at: now, amount: 9999}]},
};
check(
  'an unrelated uid cannot change the selected list',
  collectScopedExpiries(scopedTo(pool, 'intl'), unrelated),
  intl,
);

if (process.env.WB_TZ_CHILD !== '1') {
  const self = fileURLToPath(import.meta.url);
  for (const tz of ['UTC', 'Asia/Shanghai']) {
    const proc = spawnSync(
      process.execPath,
      ['--experimental-strip-types', self],
      {env: {...process.env, TZ: tz, WB_TZ_CHILD: '1'}, encoding: 'utf8'},
    );
    if (proc.status === 0) {
      console.log(`  ok  grouping in ${tz}`);
    } else {
      const out = `${proc.stdout || ''}${proc.stderr || ''}`;
      console.log(`  FAIL grouping in ${tz}\n${out}`);
      failed += 1;
    }
  }
}

console.log(failed ? `\n${failed} failed` : '\nall passed');
process.exit(failed ? 1 : 0);

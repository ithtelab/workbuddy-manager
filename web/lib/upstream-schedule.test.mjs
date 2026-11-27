/**
 * `web/lib/upstream-schedule.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/upstream-schedule.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_upstream_schedule`
 * （没有 node 时那条会 skip，不会阻塞后端测试套件）。
 *
 * 为什么值得单独测：这块逻辑错了**界面不会报错**——
 *   · 只渲染前 1-2 个时刻、改动即写回 `[h]`：多时刻排程被静默截断，任务少跑；
 *   · 上报条数越界时回落默认 5：实际上报强度被悄悄改掉；
 *   · patch 不是稀疏的：上游 schedule 段里本前端不认识的键会被整段覆盖；
 *   · 脏值收敛把合法值也当垃圾丢掉。
 * 四条都只会表现为「设置看起来保存了，行为却不对」。
 */
import assert from 'node:assert/strict';
import {
  ACTIVITY_REPORT_COUNT_MAX,
  ACTIVITY_REPORT_COUNT_MIN,
  activityReportCountInRange,
  defaultUpstreamSchedule,
  diffUpstreamSchedule,
  isUpstreamScheduleDirty,
  pickUpstreamSchedule,
} from './upstream-schedule.ts';

let checks = 0;
function check(name, fn) {
  fn();
  checks += 1;
  console.log(`  ok  ${name}`);
}

const HOUR_KEYS = [
  'checkin_hours',
  'travel_hours',
  'activity_hours',
  'keepalive_hours',
  'school_hours',
  'cat_hours',
];

check('defaults cover every visual field', () => {
  const d = defaultUpstreamSchedule();
  for (const key of HOUR_KEYS) {
    assert.ok(Array.isArray(d[key]) && d[key].length >= 1, `${key} 默认应有时刻`);
  }
  assert.equal(d.activity_report_count, 5);
  assert.equal(d.random_delay_minutes, 0);
});

check('multi-hour arrays are preserved in full (never truncated)', () => {
  const src = {};
  for (const key of HOUR_KEYS) src[key] = [8, 12, 18, 22];
  const picked = pickUpstreamSchedule(src);
  for (const key of HOUR_KEYS) {
    assert.deepEqual(picked[key], [8, 12, 18, 22], `${key} 不能被截断`);
  }
});

check('hours are de-duplicated and sorted, invalid entries dropped', () => {
  const picked = pickUpstreamSchedule({
    checkin_hours: [21, 9, 9, 24, -1, 21.5, '21', 12],
    travel_hours: [24, -3],  // 全部非法 → 回落默认
  });
  assert.deepEqual(picked.checkin_hours, [9, 12, 21]);
  assert.deepEqual(picked.travel_hours, defaultUpstreamSchedule().travel_hours);
});

check('booleans: false is honoured, junk falls back to default', () => {
  const picked = pickUpstreamSchedule({
    checkin_enabled: false,
    travel_enabled: 'no',
    activity_enabled: true,
    keepalive_enabled: 0,
  });
  const d = defaultUpstreamSchedule();
  assert.equal(picked.checkin_enabled, false);
  assert.equal(picked.travel_enabled, d.travel_enabled);
  assert.equal(picked.activity_enabled, true);
  assert.equal(picked.keepalive_enabled, d.keepalive_enabled);
});

check('activity_report_count keeps in-range values, including the new 21..50 range', () => {
  for (const n of [1, 5, 20, 21, 30, 50]) {
    const picked = pickUpstreamSchedule({activity_report_count: n});
    assert.equal(picked.activity_report_count, n, `${n} 不该被改写`);
    assert.equal(activityReportCountInRange(n), true);
  }
  assert.equal(ACTIVITY_REPORT_COUNT_MIN, 1);
  assert.equal(ACTIVITY_REPORT_COUNT_MAX, 50);
});

check('out-of-range activity_report_count is preserved, not silently reset to 5', () => {
  for (const n of [0, -1, 51, 80]) {
    const picked = pickUpstreamSchedule({activity_report_count: n});
    assert.equal(picked.activity_report_count, n, `${n} 应保留原值供界面提示`);
    assert.equal(activityReportCountInRange(n), false);
  }
  // 真正的脏值（非整数 / 空值）才回落默认
  for (const junk of [3.5, 'abc', '', null, undefined, true]) {
    const picked = pickUpstreamSchedule({activity_report_count: junk});
    assert.equal(picked.activity_report_count, 5, `${String(junk)} 应回落默认 5`);
  }
});

check('delay fields still clamp to their backend ranges', () => {
  const picked = pickUpstreamSchedule({
    random_delay_minutes: 61,
    account_delay_min_seconds: 30,
    account_delay_max_seconds: 700,
  });
  assert.equal(picked.random_delay_minutes, 0);
  assert.equal(picked.account_delay_min_seconds, 30);
  assert.equal(picked.account_delay_max_seconds, 0);
});

check('sparse patch contains only changed keys', () => {
  const org = defaultUpstreamSchedule();
  const cur = {
    ...org,
    checkin_hours: [7, 9, 21, 23],
    activity_report_count: 30,
  };
  const patch = diffUpstreamSchedule(cur, org);
  assert.deepEqual(Object.keys(patch).sort(), ['activity_report_count', 'checkin_hours']);
  assert.deepEqual(patch.checkin_hours, [7, 9, 21, 23]);
  assert.equal(patch.activity_report_count, 30);
});

check('identical drafts produce an empty patch', () => {
  const org = defaultUpstreamSchedule();
  const cur = {
    ...org,
    checkin_hours: [...org.checkin_hours],
    travel_hours: [...org.travel_hours],
  };
  assert.deepEqual(diffUpstreamSchedule(cur, org), {});
  assert.equal(isUpstreamScheduleDirty(cur, org), false);
});

check('any single field change marks the draft dirty', () => {
  const org = defaultUpstreamSchedule();
  assert.equal(isUpstreamScheduleDirty({...org, cat_hours: [2]}, org), true);
  assert.equal(isUpstreamScheduleDirty({...org, activity_report_count: 6}, org), true);
  assert.equal(isUpstreamScheduleDirty({...org, keepalive_enabled: false}, org), true);
});

check('unknown upstream schedule keys never leak into the patch', () => {
  const source = {
    checkin_enabled: true,
    checkin_hours: [9, 21],
    future_upstream_key: 'keep-me',
    nested: {a: 1},
  };
  const picked = pickUpstreamSchedule(source);
  assert.equal('future_upstream_key' in picked, false, '未知键不进草稿');
  const patch = diffUpstreamSchedule({...picked, checkin_hours: [9, 21, 23]}, picked);
  assert.deepEqual(Object.keys(patch), ['checkin_hours'],
    'patch 只应包含用户改动的已知键，未知键留给上游原样保留');
});

check('patch arrays are copies, mutating them does not corrupt the draft', () => {
  const org = defaultUpstreamSchedule();
  const cur = {...org, school_hours: [12, 18]};
  const patch = diffUpstreamSchedule(cur, org);
  patch.school_hours.push(20);
  assert.deepEqual(cur.school_hours, [12, 18]);
});

check('missing source returns defaults', () => {
  assert.deepEqual(pickUpstreamSchedule(undefined), defaultUpstreamSchedule());
});

console.log(`upstream schedule draft passed: ${checks} checks`);

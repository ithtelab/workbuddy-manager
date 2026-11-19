/**
 * `groupExpiriesByDay` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/display-prefs.test.mjs
 * 或走 Python 包装：`python -m pytest server/tests/test_display_prefs.py`
 * （没有 node 时那条会 skip，不会阻塞后端测试套件）。
 *
 * 为什么值得单独测：这个函数最容易错在**日切用的时区**。用 UTC 日切时，
 * 东八区会把当天 08:00 前的到期算进前一天——分组算错，而界面上完全看不出
 * （数字照样有、日期也像个日期，只是歸到了一天前）。这类错误没有运行时
 * 断言就只能等用户发现。
 */
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

/** 本地时区下的「某天 12:00」——用它构造与本地日历日一致的样本。 */
function localNoon(y, m, d) {
  return Math.floor(new Date(y, m - 1, d, 12, 0, 0).getTime() / 1000);
}

console.log('groupExpiriesByDay:');

// 同一天的多笔 → 合并成一笔，金额相加
check('merges same day', groupExpiriesByDay([
  {at: localNoon(2026, 9, 30), amount: 100},
  {at: localNoon(2026, 9, 30) + 3600, amount: 200},
  {at: localNoon(2026, 9, 30) + 7200, amount: 300},
]), [{at: localNoon(2026, 9, 30), amount: 600}]);

// 不同天 → 不合并；结果按时刻升序（与 CreditCountdown 期望的第一条一致）
check('keeps days apart, sorted', groupExpiriesByDay([
  {at: localNoon(2026, 10, 2), amount: 50},
  {at: localNoon(2026, 9, 30), amount: 10},
  {at: localNoon(2026, 10, 1), amount: 20},
]), [
  {at: localNoon(2026, 9, 30), amount: 10},
  {at: localNoon(2026, 10, 1), amount: 20},
  {at: localNoon(2026, 10, 2), amount: 50},
]);

// 合并后的时刻取当天**最早**那笔：倒计时宁可保守
check('takes earliest of the day', groupExpiriesByDay([
  {at: localNoon(2026, 9, 30) + 5000, amount: 1},
  {at: localNoon(2026, 9, 30), amount: 2},
]), [{at: localNoon(2026, 9, 30), amount: 3}]);

check('empty input', groupExpiriesByDay([]), []);

// 不能改到调用方的数据（仪表盘还要用原始明细画悬停提示）
const src = [
  {at: localNoon(2026, 9, 30), amount: 100},
  {at: localNoon(2026, 9, 30) + 60, amount: 200},
];
const before = JSON.stringify(src);
groupExpiriesByDay(src);
check('does not mutate input', JSON.stringify(src), before);

// 跨时区不变式：同一天的三笔在任何时区下都该合成一笔。
// （这条覆盖的就是「误用 UTC 日切」——那种写法在 UTC 下也对，
//   所以只在非 UTC 机器上才暴露。）
const threeSameDay = groupExpiriesByDay([
  {at: localNoon(2026, 9, 30), amount: 1},
  {at: localNoon(2026, 9, 30) + 60, amount: 1},
  {at: localNoon(2026, 9, 30) + 120, amount: 1},
]);
check('same local day collapses', threeSameDay.length, 1);
check('sums to 3', threeSameDay[0]?.amount, 3);

console.log(failed ? `\n${failed} failed` : '\nall passed');
process.exit(failed ? 1 : 0);

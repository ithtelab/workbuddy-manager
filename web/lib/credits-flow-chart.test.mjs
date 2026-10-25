/**
 * 积分收支折线图的数据行选择（`lib/credits-flow-chart.ts` 的行为测试）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/credits-flow-chart.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_credits_flow_chart`
 *
 * 为什么值得测：这几条错了都不会报错，只是图退回难读的形态：
 *   · 当天仍喂天序列 → 一个点画不出线（issue #285 要修的现象本身）；
 *   · 后端升级窗口内没带 hourly → 得退回天序列，不能抛错白屏；
 *   · 24 个小时标签全画出来 → X 轴糊成一片。
 */
import {creditsFlowChart} from './credits-flow-chart.ts';

let pass = 0;
const ok = (cond, label, detail = '') => {
  if (!cond) {
    console.error(`  FAIL  ${label}${detail ? ' -- ' + detail : ''}`);
    process.exitCode = 1;
    return;
  }
  pass += 1;
  console.log(`  ok  ${label}${detail ? ' -- ' + detail : ''}`);
};

const daily = [
  {day: '2026-10-24', income: 100, expense: 20, net: 80},
  {day: '2026-10-25', income: 50, expense: 5, net: 45},
];
const hourly = [
  {day: '2026-10-25', hour: 0, income: 0, expense: 0, net: 0},
  {day: '2026-10-25', hour: 9, income: 40, expense: 3.5, net: 36.5},
  {day: '2026-10-25', hour: 14, income: 0, expense: 2, net: -2},
];

// ── 当天：下探到小时 ───────────────────────────────────────────
console.log('creditsFlowChart：当天带 hourly 时下探到小时');
{
  const {rows, xInterval} = creditsFlowChart({daily, hourly}, 1);
  ok(rows.length === hourly.length, '行数跟 hourly 一致（补零由后端保证 24 桶）',
     `实得 ${rows.length}`);
  ok(rows[0].label === '00:00' && rows[1].label === '09:00',
     '小时标签补零成 HH:00', rows.map((r) => r.label).join(','));
  ok(rows[1].income === 40 && rows[1].expense === 3.5 && rows[1].net === 36.5,
     '收支数值原样透传', JSON.stringify(rows[1]));
  ok(xInterval === 2, '24 个标签每 3 个显示 1 个（与用量趋势图一致）', String(xInterval));
}

// ── 升级窗口：后端没带 hourly ──────────────────────────────────
console.log('creditsFlowChart：没有 hourly 时退回天序列，不抛错');
{
  const {rows, xInterval} = creditsFlowChart({daily}, 1);
  ok(rows.length === 2 && rows[0].label === '2026-10-24',
     '退回 daily，label 仍是完整日期（维持现状）', JSON.stringify(rows[0]));
  ok(xInterval === 'preserveEnd', '天序列交给 recharts 自动稀疏', String(xInterval));
}

// ── 多日：忽略 hourly ──────────────────────────────────────────
console.log('creditsFlowChart：多日范围不碰 hourly');
{
  const {rows, xInterval} = creditsFlowChart({daily, hourly}, 7);
  ok(rows.length === 2 && rows[1].label === '2026-10-25',
     '多日按天序列', rows.map((r) => r.label).join(','));
  ok(xInterval === 'preserveEnd', '天序列刻度密度');
}

if (process.exitCode) {
  console.error('\n有断言未通过');
} else {
  console.log(`\nall passed（${pass} 条）`);
}

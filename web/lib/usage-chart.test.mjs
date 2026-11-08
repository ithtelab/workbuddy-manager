/**
 * 用量趋势图的粒度/形态选择与序列补齐（`lib/usage-chart.ts` 的行为测试）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/usage-chart.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_usage_chart`
 *
 * 为什么值得测：这几条错了都**不会报错**，只是图变得难读 ——
 *   · 范围 = 今日却仍按天 → 一根柱子（这就是这次要修的现象本身）；
 *   · 多日范围不补零 → 没请求的那天凭空消失，读者以为「那天不存在」；
 *   · 24/90 个标签全画出来 → X 轴糊成一片。
 */
import {
  chartSpecFor,
  dailySeries,
  hourlySeries,
  todayKeyLocal,
} from './usage-chart.ts';

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

// ── 范围 → 粒度/形态 ─────────────────────────────────────────────
console.log('chartSpecFor：范围决定粒度与形态');
{
  const today = chartSpecFor(1);
  ok(today.granularity === 'hour', '今日 → 按小时（不然只有一根柱子）', JSON.stringify(today));
  ok(today.shape === 'bar', '今日 → 柱状（每小时一根，读起来自然）');
  ok(today.tickEvery === 3, '今日 → 每 3 小时一个刻度（24 个标签会糊）');

  const week = chartSpecFor(7);
  ok(week.granularity === 'day' && week.shape === 'bar' && week.tickEvery === 1,
     '近 7 天 → 按天柱状、每天都标', JSON.stringify(week));

  const month = chartSpecFor(30);
  ok(month.granularity === 'day' && month.shape === 'area',
     '近 30 天 → 面积（30 根柱子太密）', JSON.stringify(month));

  const quarter = chartSpecFor(90);
  ok(quarter.shape === 'area' && quarter.tickEvery >= 10,
     '近 90 天 → 面积 + 稀疏刻度', JSON.stringify(quarter));
}

// ── 小时序列 ─────────────────────────────────────────────────────
console.log('hourlySeries：固定 24 个点');
{
  const raw = [
    {day: '2026-09-29', hour: 0, requests: 0, prompt_tokens: 0, completion_tokens: 0, credit: 0, failed: 0},
    {day: '2026-09-29', hour: 9, requests: 3, prompt_tokens: 100, completion_tokens: 40, credit: 1, failed: 1, success: 2},
    {day: '2026-09-29', hour: 13, requests: 1, prompt_tokens: 7, completion_tokens: 3, credit: 0, failed: 0},
  ];
  const series = hourlySeries(raw, 13);
  ok(series.length === 24, '24 个点（后端补零 + 这里兜底）', `实得 ${series.length}`);
  ok(series[0].label === '00:00' && series[23].label === '23:00', '标签是 HH:00');
  ok(series[9].tokens === 140 && series[9].requests === 3 && series[9].failed === 1,
     '第 9 小时数值正确（token 为 prompt+completion）', JSON.stringify(series[9]));
  ok(series[13].isNow === true && series[9].isNow === false, '当前小时被标记出来（UI 会高亮）');
  ok(series[10].tokens === 0 && series[10].requests === 0, '没有数据的时段是 0，不是缺格');
  ok(series[9].success === 2 && series[10].success === 0,
     '成功数与失败数各自独立补齐（issue #369 趋势成功线）');
  // 乱序输入也要归位
  const shuffled = hourlySeries([raw[2], raw[0], raw[1]], 13);
  ok(shuffled[9].tokens === 140 && shuffled[13].tokens === 10, '输入乱序也按小时归位');
}

// ── 天序列（补零） ───────────────────────────────────────────────
console.log('dailySeries：窗口内定长、缺的天补零');
{
  const pts = [
    {day: '2026-09-27', requests: 2, prompt_tokens: 20, completion_tokens: 10, credit: 0, failed: 0},
    {day: '2026-09-29', requests: 5, prompt_tokens: 50, completion_tokens: 25, credit: 0, failed: 3, success: 4},
  ];
  const series = dailySeries(pts, 7, '2026-09-29');
  ok(series.length === 7, '7 天窗口 = 7 个点', `实得 ${series.length}`);
  ok(series[6].label === '09-29' && series[0].label === '09-23',
     '窗口是「今天往前数 7 天」', `${series[0].label} … ${series[6].label}`);
  ok(series[6].tokens === 75 && series[6].failed === 3, '今天的数据对得上');
  ok(series[6].success === 4 && series[5].success === 0, '每日成功数同样按日归位并补 0');
  ok(series[5].tokens === 0 && series[5].requests === 0,
     '没有请求的那天补 0（不补的话那天会从图上消失）');
  ok(series[4].tokens === 30, '中间那天有数据', JSON.stringify(series[4]));

  // 跨月/跨年：日期加减不能靠字符串拼
  const crossed = dailySeries([], 3, '2026-10-01');
  ok(crossed.map((p) => p.label).join(',') === '09-29,09-30,10-01',
     '跨月补齐正确', crossed.map((p) => p.label).join(','));
  const year = dailySeries([], 2, '2027-01-01');
  ok(year.map((p) => p.label).join(',') === '12-31,01-01',
     '跨年补齐正确', year.map((p) => p.label).join(','));
  // 窗口外的旧点不该混进来
  const stale = dailySeries(
    [{day: '2020-01-01', requests: 9, prompt_tokens: 9, completion_tokens: 9, credit: 0, failed: 0}],
    3, '2026-09-29');
  ok(stale.every((p) => p.requests === 0), '窗口之外的点被丢掉（定长承诺）');
}

console.log('todayKeyLocal：本地时区，不是 UTC');
{
  const d = new Date(2026, 8, 29, 0, 30, 0);      // 本地 2026-09-29 00:30
  ok(todayKeyLocal(d) === '2026-09-29', '本地日期（UTC 会退到 09-28）', todayKeyLocal(d));
}

if (process.exitCode) {
  console.error('\n有断言未通过');
} else {
  console.log(`\nall passed（${pass} 条）`);
}

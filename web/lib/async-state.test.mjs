/**
 * `splitSettled` / `asyncFlags` / `depMode` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/async-state.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_async_state`
 * （没有 node 时那条会 skip，不会阻塞后端测试套件）。
 *
 * 为什么值得单独测：这几个函数错起来**界面不报错**，只是变得不对劲——
 *   · 拿「请求在飞」当加载态 → 有心跳轮询的页面每 30 秒闪一次骨架；
 *   · 刷新时整体替换而不是合并 → 心跳里任何一个请求超时就把内容清空；
 *   · 首屏全失败时不报错 → 用户看到「暂无账号」，而数据其实是没取到；
 *   · 翻页被判成「换了数据上下文」→ 每翻一页闪一次骨架；
 *   · 「失败吞成兜底值」的取数函数混进主数据 → 全挂时被当成「有数据」，整页错误态
 *     反而不出现（这条是真实浏览器验收抓到的，见下方回归段）。
 * 五条都属于「看起来只是卡了一下」，没人会为此提 issue，只能靠断言钉住。
 */
import {asyncFlags, depMode, splitSettled} from './async-state.ts';

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

const ok = (value) => ({status: 'fulfilled', value});
const bad = (reason) => ({status: 'rejected', reason});

console.log('splitSettled:');

// 全部成功 → 全进 values，errors 为空
check('all fulfilled', splitSettled(['a', 'b'], [ok(1), ok(2)]), {
  values: {a: 1, b: 2}, errors: {},
});

// 逐项独立成败：2/5 失败，另外 3 份数据仍要拿得到
const mixed = splitSettled(
  ['accounts', 'summary', 'daily', 'upstream', 'credits'],
  [ok('A'), bad('boom'), ok('D'), bad('timeout'), ok('C')],
);
check('failed keys go to errors', Object.keys(mixed.errors).sort(), ['summary', 'upstream']);
check('succeeded keys keep their values', mixed.values, {accounts: 'A', daily: 'D', credits: 'C'});
check('failed keys are absent from values', 'summary' in mixed.values, false);

// 全失败 → values 为空（这正是「首屏彻底失败」的输入）
check('all rejected', splitSettled(['a'], [bad('x')]), {values: {}, errors: {a: 'x'}});

// 空输入
check('empty', splitSettled([], []), {values: {}, errors: {}});

// 结果比键多：多出来的忽略，不能塞进一个 undefined 键
check('extra results ignored', splitSettled(['a'], [ok(1), ok(2)]), {values: {a: 1}, errors: {}});

console.log('asyncFlags:');

const flags = (values, errors, pending) => {
  const f = asyncFlags(values, errors, pending);
  return [f.isInitialLoading, f.isInitialFailed, f.isRefreshing];
};

// 首屏：还没拿到任何数据，请求在飞 → 骨架
check('first load', flags({}, {}, true), [true, false, false]);

// **心跳刷新**：已有数据、请求在飞 → 不是首屏，不能显示骨架
check('heartbeat refresh', flags({accounts: []}, {}, true), [false, false, true]);

// 首屏彻底失败 → 错误态与「重试」，而不是一直转圈
check('first load failed', flags({}, {accounts: 'boom'}, false), [false, true, false]);

// 刷新失败但还有数据 → 内容保留（走顶部提示），既不 loading 也不 failed
check('refresh failed, data kept', flags({accounts: []}, {summary: 'boom'}, false), [false, false, false]);

// 一切就绪
check('ready', flags({accounts: []}, {}, false), [false, false, false]);

// 三者互斥：任何输入下最多只有一个为 true
for (const pending of [true, false]) {
  for (const values of [{}, {a: 1}]) {
    for (const errors of [{}, {a: 'x'}]) {
      const on = flags(values, errors, pending).filter(Boolean).length;
      if (on > 1) {
        console.log(`  FAIL 互斥性被破坏 pending=${pending} values=${Object.keys(values)} errors=${Object.keys(errors)}`);
        failed += 1;
      }
    }
  }
}
console.log('  ok  flags are mutually exclusive');

// 回归：把判据写回「请求在飞」会怎样。
// 显式跑一遍那个**错**判据，证明它确实会误报 —— 免得后人「顺手简化」回去。
const heartbeat = {values: {accounts: []}, errors: {}, pending: true};
const naiveLoading = heartbeat.pending; // 错判据：有请求在飞就算加载中
const realLoading = asyncFlags(heartbeat.values, heartbeat.errors, heartbeat.pending).isInitialLoading;
check('naive criterion would flash the skeleton every heartbeat', naiveLoading, true);
check('real criterion stays quiet', realLoading, false);

// 回归：「把失败吞成兜底值」的取数函数会和主数据抢「有没有数据」这个判据。
//
// 上游统计那份取数就是这种写法——失败时 `return {available: false}` 而不是抛错，
// 因为它取不到是常态、不该弹提示。代价是它**永远算一次成功的取数**：五份数据全挂
// 时它照样往 values 里塞一个键，「一份都没取到」的判据就被顶掉，页面不进整页错误
// 态，反而照常渲染四张 0 卡片和「暂无数据」。
// 这不是推演出来的，是真实浏览器验收时发现的：把五个接口全打成 500，整页错误态
// 根本没出现。所以它必须单独放一个 hook。
const swallowed = flags({upstream: {available: false}}, {}, false);
check('a fetcher that swallows its failure still counts as "has data"', swallowed[1], false);
check('…so the page renders content instead of the error state', swallowed[0], false);
check('…and the trap is gone once that fetcher is in its own group', flags({}, {summary: 'boom'}, false)[1], true);

console.log('depMode:');

const P = (context, query) => ({context, query});

// 首屏：context 从 null 变成实际值 → initial（清空旧数据 + 骨架）
check('first run', depMode(P(null, '[]'), P('["cn"]', '[]')), 'initial');

// 什么都没变 → null，不该重取
check('nothing changed', depMode(P('["cn"]', '[]'), P('["cn"]', '[]')), null);

// 切版本 = 换了数据上下文 → initial（旧数字属于旧版本，必须清掉）
check('context changed', depMode(P('["cn"]', '[]'), P('["global"]', '[]')), 'initial');

// 翻页 = 只是换了查询范围 → refresh（保留现有内容，不闪骨架）
check('query changed', depMode(P('["cn"]', '[1,"7"]'), P('["cn"]', '[2,"7"]')), 'refresh');

// 两者同时变（切版本时页码归 1）→ 按 initial 处理，只重取一次。
// 若判成 refresh，就会出现「先清空、再立刻把旧数据合并回来」的多余一轮。
check(
  'context wins when both change',
  depMode(P('["cn"]', '[5,"7"]'), P('["global"]', '[1,"7"]')),
  'initial',
);

// 回归：把两组依赖并回一组（任何变化都清空）会怎样。
// 显式跑一遍那个**错**判据，证明它确实会误报 —— 免得后人图省事合并回去。
const pagingMode = depMode(P('["cn"]', '[1,"7"]'), P('["cn"]', '[2,"7"]'));
check('naive "any dep change resets" would flash the skeleton on paging', pagingMode === 'initial', false);
check('real criterion keeps it a silent refresh', pagingMode, 'refresh');

// 组合起来才是「翻页不闪骨架」：模式是 refresh（不清空）**且**旧数据还在屏幕上，
// 于是 isInitialLoading 为 false。单独看任何一步都说明不了这件事。
const pagingLoading = asyncFlags({logs: [1, 2, 3]}, {}, true).isInitialLoading;
check('paging: rows stay on screen, no skeleton', pagingLoading, false);

console.log(failed ? `\n${failed} failed` : '\nall passed');
process.exit(failed ? 1 : 0);

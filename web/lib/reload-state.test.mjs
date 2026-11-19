/**
 * `web/lib/reload-state.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/reload-state.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_reload_state`
 * （没有 node 时那条会 skip，不会阻塞后端测试套件）。
 *
 * 为什么值得单独测：这个模块错了，界面会**说反话**——
 *   · 拿上一次的结论当成这一次的 → 刚修好配置、还没重载，就报「重载失败」；
 *   · 反过来也会：上一次成功，这一次正在跑，就先说「已生效」；
 *   · 成功不设边界 → 页面永远挂着「配置已生效」，成了新的噪音；
 *   · 失败设了边界 → 最需要人处理的那条提示，偏偏在打开页面时消失。
 * 这几种都不抛异常，而且看起来都「挺像回事」。
 */
import {
  RELOAD_PHASES,
  RELOAD_POLL_APPLYING_MS,
  RELOAD_POLL_IDLE_MS,
  reloadPhase,
  reloadPhaseVisible,
  reloadPollDelay,
} from './reload-state.ts';

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

/** 造一份状态。默认 = 「从没重载过」 */
const st = (over = {}) => ({
  running: false,
  pending: false,
  last_at: 0,
  last_ok: null,
  last_message: '',
  restart_count: 0,
  ...over,
});

/** 简写：状态 + 基线 → 相位 */
const ph = (over, baseline) => reloadPhase(st(over), baseline);

/* ── 清单本身 ─────────────────────────────────────────────────── */

check('四档相位', [...RELOAD_PHASES], ['applying', 'ok', 'failed', 'idle']);

check(
  '每一档都有确定的延迟（漏一档会拿到 undefined → setTimeout 当 0 → 疯狂轮询）',
  RELOAD_PHASES.map((p) => typeof reloadPollDelay(p)),
  ['number', 'number', 'number', 'number'],
);

check(
  '只有「应用中」需要跟紧（其余用慢档）',
  RELOAD_PHASES.map((p) => (reloadPollDelay(p) === RELOAD_POLL_APPLYING_MS ? 'fast' : 'slow')),
  ['fast', 'slow', 'slow', 'slow'],
);

check(
  '跟紧的间隔必须**严格快于**慢档（否则等于没跟）',
  RELOAD_POLL_APPLYING_MS < RELOAD_POLL_IDLE_MS,
  true,
);

check(
  'idle 不占位，其余三档都要在界面上出现',
  RELOAD_PHASES.map(reloadPhaseVisible),
  [true, true, true, false],
);

/* ── 从没重载过 ───────────────────────────────────────────────── */

check('从没重载过 → idle（不能凭空说「已生效」）', ph({}, 0), 'idle');
check('last_ok=null 且没在跑 → idle', ph({last_ok: null, last_at: 0}, 0), 'idle');

/* ── 正在跑 / 已排队 ──────────────────────────────────────────── */

check('running → applying', ph({running: true}, 0), 'applying');
check('pending → applying（合并窗口里还没轮到它）', ph({pending: true}, 0), 'applying');

check(
  '上一次失败、这一次正在跑 → applying（不能拿上一次的结论盖住这一次）',
  ph({running: true, last_ok: false, last_at: 100, last_message: 'docker 挂了'}, 100),
  'applying',
);
check(
  '上一次成功、这一次正在跑 → applying（同理，不能说「已生效」）',
  ph({running: true, last_ok: true, last_at: 100}, 100),
  'applying',
);
check(
  '排队中同样优先于上一次的结论',
  ph({pending: true, last_ok: true, last_at: 100}, 100),
  'applying',
);

/* ── 失败：常驻（有意不看基线） ───────────────────────────────── */

check(
  '上次失败、现在没在跑 → failed',
  ph({last_ok: false, last_at: 100, last_message: 'docker: command not found'}, 100),
  'failed',
);
check(
  '失败发生在**打开页面之前**也要报（基线比 last_at 大也一样）',
  ph({last_ok: false, last_at: 100}, 999),
  'failed',
);
check(
  '失败是本会话期间发生的，当然也要报',
  ph({last_ok: false, last_at: 200}, 100),
  'failed',
);

/* ── 成功：只报「本会话观察到过」的那一次 ─────────────────────── */

check(
  '本会话期间落地了一次成功 → ok',
  ph({last_ok: true, last_at: 200}, 100),
  'ok',
);
check(
  '成功发生在本会话之前 → idle（否则页面会永远挂着「配置已生效」）',
  ph({last_ok: true, last_at: 100}, 100),
  'idle',
);
check(
  '基线比 last_at 还大（时钟异常 / 状态被重置）→ idle，不能算成本次',
  ph({last_ok: true, last_at: 100}, 200),
  'idle',
);
check(
  'ok 的判据是 last_at **严格大于**基线（相等不算）',
  ph({last_ok: true, last_at: 101}, 100) === 'ok' && ph({last_ok: true, last_at: 100}, 100) === 'idle',
  true,
);

/* ── 组合优先级：running/pending > failed > ok > idle ─────────── */

check(
  '优先级顺序：正在跑盖过失败，失败盖过成功',
  [
    ph({running: true, last_ok: false, last_at: 200}, 100),
    ph({last_ok: false, last_at: 200}, 100),
    ph({last_ok: true, last_at: 200}, 100),
  ],
  ['applying', 'failed', 'ok'],
);

/* ── 纯函数 ───────────────────────────────────────────────────── */

check(
  '不改动入参',
  (() => {
    const s = st({running: true, last_ok: false, last_at: 7});
    const copy = JSON.stringify(s);
    reloadPhase(s, 0);
    reloadPollDelay('applying');
    return JSON.stringify(s) === copy;
  })(),
  true,
);

check(
  '同一个输入永远给同一个结果（没有内部状态）',
  [1, 2, 3].map(() => ph({last_ok: true, last_at: 200}, 100)),
  ['ok', 'ok', 'ok'],
);

if (failed > 0) {
  console.log(`\n${failed} 项失败`);
  process.exit(1);
}
console.log('\nall passed');

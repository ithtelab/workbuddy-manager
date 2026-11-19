/**
 * `web/lib/dock-groups.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/dock-groups.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_dock_groups`
 * （没有 node 时那条会 skip，不会阻塞后端测试套件）。
 *
 * 为什么值得单独测：分组切错了**界面一声不响**，只是底栏看起来不对——
 *   · 把「相邻」判成「相等」→ 同名的两组被并成一组，中间那两条本该分组的
 *     条目被塞进错误的组里，组名与内容对不上；
 *   · 同一键散落两处却被静默合并 → 底栏上出现两条都叫「运营」的分隔线；
 *   · 把「相邻」判成「相等」（同名分组跨距离合并）→ 本该分组的条目被塞进错误的组，
 *     组名与内容对不上。
 * 都不会报错。
 */
import {splitByGroup} from './dock-groups.ts';

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

/** 只留测试关心的字段，免得把 icon 之类的 React 元素塞进断言里 */
const item = (title, groupKey, groupLabel) => ({title, groupKey, groupLabel});

/** 切成「组名 → 组内条目名」的简写，断言读起来更像界面 */
const shape = (items) =>
  splitByGroup(items).map((g) => [g.key, g.label, g.items.map((i) => i.title)]);

/* ── splitByGroup：按相邻切 ───────────────────────────────────── */

check('空数组切出零组', splitByGroup([]), []);

check(
  '全都没写 groupKey → 一组（键为空串）',
  shape([item('a'), item('b'), item('c')]),
  [['', '', ['a', 'b', 'c']]],
);

check(
  '相邻同键并成一组',
  shape([item('a', 'ops'), item('b', 'ops'), item('c', 'ops')]),
  [['ops', '', ['a', 'b', 'c']]],
);

check(
  '相邻不同键就断开',
  shape([item('a', 'overview'), item('b', 'ops')]),
  [
    ['overview', '', ['a']],
    ['ops', '', ['b']],
  ],
);

check(
  '组名取该组第一条的 groupLabel',
  shape([item('a', 'ops', '运营'), item('b', 'ops', '运营')]),
  [['ops', '运营', ['a', 'b']]],
);

check(
  '同一键散落两处 → 切成两组，不静默合并',
  shape([item('a', 'ops', '运营'), item('b', 'gov', '治理'), item('c', 'ops', '运营')]),
  [
    ['ops', '运营', ['a']],
    ['gov', '治理', ['b']],
    ['ops', '运营', ['c']],
  ],
);

check(
  '组名只认第一条（后面几条即使写了别的标签也不改组名）',
  shape([item('a', 'ops', '运营'), item('b', 'ops', '别的')]),
  [['ops', '运营', ['a', 'b']]],
);

check(
  '不写 groupLabel 的组 → 标签为空串（调用方据此画不带标签的分隔线）',
  shape([item('a', 'actions'), item('b', 'actions')]),
  [['actions', '', ['a', 'b']]],
);

check(
  '返回的是**新数组**，不改动入参',
  (() => {
    const src = [item('a', 'ops'), item('b', 'gov')];
    const before = JSON.stringify(src);
    splitByGroup(src);
    return JSON.stringify(src) === before;
  })(),
  true,
);

check(
  '组内条目的顺序与入参一致',
  shape([item('c', 'ops'), item('a', 'ops'), item('b', 'ops')])[0][2],
  ['c', 'a', 'b'],
);

/* ── 单组不画线：渲染层用 `index > 0`，与分组数无关 ───────────── */

check('全都同组时只有一组，第一条之前不会有分隔线', splitByGroup([item('a'), item('b')]).length, 1);

/* ── 底栏真实分组（照着 ManagementBar 的口径） ─────────────────── */

// 批次 4 ② 把**目的地**从 11 项收敛到 8 项：任务记录 / 红包 / 聊天测试台
// 不再各占一个入口，改成在「账号」/「密钥」/「模型」页里用页内二级导航切换
// （清单在 `web/lib/section-nav.ts`）。所以下面这三项**故意**不在这里了——
// 它们不是「被漏掉」，而是「被吸收」。运营组因此从 6 项变成 3 项。
const dock = [
  item('dashboard', 'overview', '总览'),
  item('accounts', 'ops', '运营'),
  item('keys', 'ops', '运营'),
  item('models', 'ops', '运营'),
  item('stats', 'governance', '治理'),
  item('logs', 'governance', '治理'),
  item('security', 'governance', '治理'),
  item('settings', 'governance', '治理'),
  item('quickAdd', 'actions'),
  item('profile', 'actions'),
];

check(
  '真实底栏切成 4 组，组名依次是 总览/运营/治理/（无）',
  splitByGroup(dock).map((g) => [g.label, g.items.length]),
  [
    ['总览', 1],
    ['运营', 3],
    ['治理', 4],
    ['', 2],
  ],
);

check(
  '真实底栏共 10 项（8 个目的地 + 2 个动作），一项都不能丢',
  splitByGroup(dock).flatMap((g) => g.items).length,
  10,
);

check(
  '被吸收的三页不再出现在底栏',
  splitByGroup(dock).flatMap((g) => g.items).map((i) => i.title)
    .filter((t) => ['tasks', 'redPackets', 'playground'].includes(t)),
  [],
);

if (failed > 0) {
  console.log(`\n${failed} 项失败`);
  process.exit(1);
}
console.log('\nall passed');

/**
 * `web/lib/section-nav.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/section-nav.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_section_nav`
 * （没有 node 时那条会 skip，不会阻塞后端测试套件）。
 *
 * 为什么值得单独测：这个模块错了**界面一声不响**——
 *   · 路径认不出来 → 页面上根本没有二级导航（页面其余部分完全正常）；
 *   · 路径认错一节 → 页面顶部凭空出现两条指向别处的 Tab；
 *   · 匹配放宽成「后缀」→ `/settings/models` 以 `/models` 结尾，设置页被认成
 *     「模型」节（**默认部署形态下就会发生**，不是子路径部署才有的）；
 *   · 忘了剥部署前缀 → 子路径部署下（`/workbuddy-manager/tasks`）全部失效，
 *     而本地永远复现不出来；
 *   · 用 `startsWith` 之类 → `/tasksx` 也被当成任务记录页。
 * 这几种都不会抛异常。
 */
import {
  SECTIONS,
  SECTION_NAV,
  isSectionKey,
  sectionNavFromPath,
  sectionNavItems,
} from './section-nav.ts';

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

/** 简写：路径 → `[节, 项]`（认不出给 null），断言读起来更像界面 */
const at = (path, basePath = '') => {
  const found = sectionNavFromPath(path, basePath);
  return found ? [found.section, found.item.key] : null;
};

/** 子路径部署用的前缀（与 `deploy/README.md` 里的例子一致） */
const PREFIX = '/workbuddy-manager';

/* ── 清单本身 ─────────────────────────────────────────────────── */

check('三节', [...SECTIONS], ['accounts', 'keys', 'models']);

check(
  '每节两项（只有一项就不该叫「二级导航」）',
  SECTIONS.map((s) => SECTION_NAV[s].length),
  [2, 2, 2],
);

check(
  '每节第一项是该节的落点，且 href 与节名一致',
  SECTIONS.map((s) => SECTION_NAV[s][0].href),
  ['/accounts', '/keys', '/models'],
);

check(
  '所有 key 唯一',
  (() => {
    const all = SECTIONS.flatMap((s) => SECTION_NAV[s].map((i) => i.key));
    return all.length === new Set(all).size;
  })(),
  true,
);

check(
  '所有 href 唯一',
  (() => {
    const all = SECTIONS.flatMap((s) => SECTION_NAV[s].map((i) => i.href));
    return all.length === new Set(all).size;
  })(),
  true,
);

check(
  '所有 iconKey 唯一（映射表里不该有两项共用同一个图标）',
  (() => {
    const all = SECTIONS.flatMap((s) => SECTION_NAV[s].map((i) => i.iconKey));
    return all.length === new Set(all).size;
  })(),
  true,
);

check(
  'href 都是站内绝对路径（不带 basePath）',
  SECTIONS.flatMap((s) => SECTION_NAV[s].map((i) => i.href))
    .filter((h) => !/^\/[a-z0-9-]+$/.test(h)),
  [],
);

check(
  'labelKey 都指向 nav.* 下的键',
  SECTIONS.flatMap((s) => SECTION_NAV[s].map((i) => i.labelKey))
    .filter((k) => !/^nav\.[a-zA-Z]+$/.test(k)),
  [],
);

check('sectionNavItems 与 SECTION_NAV 一致', sectionNavItems('keys'), SECTION_NAV.keys);

/* ── isSectionKey ─────────────────────────────────────────────── */

check('isSectionKey 认三节', SECTIONS.map((s) => isSectionKey(s)), [true, true, true]);
check(
  'isSectionKey 不认别的',
  ['', 'accounts2', 'Accounts', 'settings', null, undefined, 7, {}, []]
    .map((v) => isSectionKey(v)),
  [false, false, false, false, false, false, false, false, false],
);

/* ── sectionNavFromPath：正常路径（根路径部署，basePath 为空串） ── */

check('/accounts', at('/accounts'), ['accounts', 'accounts']);
check('/tasks', at('/tasks'), ['accounts', 'tasks']);
check('/keys', at('/keys'), ['keys', 'keys']);
check('/red-packets', at('/red-packets'), ['keys', 'redPackets']);
check('/models', at('/models'), ['models', 'models']);
check('/playground', at('/playground'), ['models', 'playground']);

/* ── 尾斜杠（export 模式开了 trailingSlash） ───────────────────── */

check('/accounts/ 也认', at('/accounts/'), ['accounts', 'accounts']);
check('/tasks/ 也认', at('/tasks/'), ['accounts', 'tasks']);
check('/red-packets/// 也认', at('/red-packets///'), ['keys', 'redPackets']);

/* ── 部署前缀（子路径部署）：前缀由调用方传进来，剥掉后精确匹配 ── */

check(
  '/workbuddy-manager/tasks 也认（子路径部署）',
  at(`${PREFIX}/tasks`, PREFIX),
  ['accounts', 'tasks'],
);
check(
  '前缀 + 尾斜杠',
  at(`${PREFIX}/red-packets/`, PREFIX),
  ['keys', 'redPackets'],
);
check(
  '前缀本身不是任何一项',
  at(PREFIX, PREFIX),
  null,
);
check(
  '前缀 + 未知页',
  at(`${PREFIX}/dashboard`, PREFIX),
  null,
);

/* ── 前缀「在场才剥」；不在场时按原样比 ───────────────────────── */

check('/wb 前缀下 /wb/tasks 认', at('/wb/tasks', '/wb'), ['accounts', 'tasks']);

// 这一条钉住「前缀不在场时不判成认不出」：判成认不出的表现是**二级导航整排消失**，
// 而多显示一排只是多余。真实场景是调用方传了前缀、而应用其实部署在根路径。
check(
  '前缀不在场时按原样比（传错前缀不该让导航整排消失）',
  at('/tasks', '/wb'),
  ['accounts', 'tasks'],
);

// 这一条是「精确匹配」的推论，不是额外的守卫：剥完是 `tasks`，不等于 `/tasks`。
// 写错的表现是「`/wbtasks` 这种不存在的页面也长出二级导航」。
check(
  '/wbtasks 不是任务记录页（前缀后面不是 `/`，剥完也对不上）',
  at('/wbtasks', '/wb'),
  null,
);

/* ── 认不出 → null（调用方据此什么都不渲染） ──────────────────── */

check('/dashboard 认不出', at('/dashboard'), null);
check('/stats 认不出', at('/stats'), null);
check('/logs 认不出（它是底栏目的地，没有二级导航）', at('/logs'), null);
check('/traces 认不出（同上）', at('/traces'), null);
check('/security 认不出', at('/security'), null);
check('/ 认不出', at('/'), null);
check('空串认不出', at(''), null);
check('null 认不出', at(null), null);
check('undefined 认不出', at(undefined), null);

check(
  '设置页认不出（它有自己的 settings-tabs.ts，两套清单不混）',
  ['/settings', '/settings/users', '/settings/models'].map((p) => at(p)),
  [null, null, null],
);

/* ── 精确匹配：只认清单里那 6 条路径，别的都不认 ───────────────── */

// 这一节钉住的是一次**真实踩过的坑**：第一版用 `endsWith(item.href)` 匹配后缀，
// 于是 `/settings/models`（设置页的「模型」Tab）被认成「模型」节，设置页凭空
// 长出两条指向别处的 Tab。放宽成后缀还会顺带接受 `/some/prefix/tasks`。
check('/tasksx 不该认（不是按前缀匹配）', at('/tasksx'), null);
check('/my-tasks 不该认', at('/my-tasks'), null);
check('/keys2 不该认', at('/keys2'), null);
check('/models/playground 不该认（该页不存在，是**有意**留空）', at('/models/playground'), null);
check('/settings/models 不该认（曾因后缀匹配被误认成 models 节）', at('/settings/models'), null);
check(
  '/some/prefix/tasks 不该认（只剥调用方给的那一个前缀）',
  at('/some/prefix/tasks'),
  null,
);
check('/keys/red-packets 不该认（平级路径，没有嵌套）', at('/keys/red-packets'), null);
check('/accounts/tasks 不该认（平级路径，没有嵌套）', at('/accounts/tasks'), null);

/* ── 往返：每一项的 href 都能认出自己 ─────────────────────────── */

check(
  '每一项的 href 都能认出自己（往返一致）',
  SECTIONS.flatMap((s) => SECTION_NAV[s].map((i) => {
    const found = sectionNavFromPath(i.href, '');
    return found ? found.item.href === i.href && found.section === s : false;
  })),
  [true, true, true, true, true, true],
);

check(
  '加了部署前缀后仍然往返一致',
  SECTIONS.flatMap((s) => SECTION_NAV[s].map((i) => {
    const found = sectionNavFromPath(`${PREFIX}${i.href}`, PREFIX);
    return found ? found.item.href === i.href && found.section === s : false;
  })),
  [true, true, true, true, true, true],
);

if (failed > 0) {
  console.log(`\n${failed} 项失败`);
  process.exit(1);
}
console.log('\nall passed');

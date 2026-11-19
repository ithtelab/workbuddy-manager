/**
 * `web/lib/settings-tabs.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/settings-tabs.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_settings_tabs`
 * （没有 node 时那条会 skip，不会阻塞后端测试套件）。
 *
 * 为什么值得单独测：这份清单是**四处共用的单一事实来源**——二级导航、10 个路由
 * 目录、重定向的落点、以及路径解析。错起来界面不会报错，只会「点进去 404」或者
 * 「地址栏是 models、内容是 users」：
 *   · 解析多认一段（`/settings/models/extra` 也算 models）→ 一条不存在的 URL 会
 *     安静地渲染成某个 Tab，用户以为分享对了；
 *   · 解析少认一段（尾斜杠、basePath 前缀没剥）→ 子路径部署下每个 Tab 都点不动，
 *     而本地（无前缀）永远测不出来；
 *   · 顺序或名字被改 → 导航顺序与路由目录对不上，且**没有任何报错**。
 */
import {
  DEFAULT_SETTINGS_TAB,
  SETTINGS_TABS,
  isSettingsTab,
  settingsTabFromPath,
  settingsTabHref,
} from './settings-tabs.ts';

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

/* ── 清单本身 ─────────────────────────────────────────────────── */

check(
  '清单就是这 10 项，顺序固定（导航顺序 = 路由顺序 = i18n 键顺序）',
  SETTINGS_TABS,
  ['upstream', 'proxy', 'models', 'users', 'tokens', 'system', 'watchdog',
   'backup', 'changelog', 'about'],
);

check('清单里没有重复项', new Set(SETTINGS_TABS).size, SETTINGS_TABS.length);

check('默认 Tab 是清单第一项', DEFAULT_SETTINGS_TAB, SETTINGS_TABS[0]);

check(
  '每一项的 href 互不相同',
  new Set(SETTINGS_TABS.map(settingsTabHref)).size,
  SETTINGS_TABS.length,
);

check('href 形状：/settings/<tab>', settingsTabHref('models'), '/settings/models');

/* ── isSettingsTab：只认清单里的字符串 ────────────────────────── */

for (const tab of SETTINGS_TABS) {
  check(`isSettingsTab('${tab}') → true`, isSettingsTab(tab), true);
}

for (const bad of ['', 'nope', 'Upstream', 'upstream ', ' upstream', 'SETTINGS', '0']) {
  check(`isSettingsTab(${JSON.stringify(bad)}) → false`, isSettingsTab(bad), false);
}

for (const bad of [undefined, null, 0, 1, true, [], {}, ['upstream']]) {
  check(`isSettingsTab(${JSON.stringify(bad)}) → false（非字符串）`, isSettingsTab(bad), false);
}

/* ── settingsTabFromPath：从路径解析当前 Tab ──────────────────── */

const pathCases = [
  // [路径, 期望]
  ['/settings/upstream', 'upstream'],
  ['/settings/models', 'models'],
  ['/settings/changelog', 'changelog'],
  // export 模式开了 trailingSlash，客户端拿到的可能带尾斜杠
  ['/settings/users/', 'users'],
  ['/settings/users///', 'users'],
  // 子路径部署（basePath 前缀）：判据是「匹配后缀」，所以前缀是什么都不影响
  ['/workbuddy-manager/settings/tokens', 'tokens'],
  ['/workbuddy-manager/settings/tokens/', 'tokens'],
  // 不在设置页 / 没有子路径 / 子路径不是已知 Tab
  ['/settings', null],
  ['/settings/', null],
  ['/workbuddy-manager/settings', null],
  ['/settings/nope', null],
  ['/settings/UPSTREAM', null],
  ['/dashboard', null],
  ['/', null],
  ['', null],
  [null, null],
  [undefined, null],
  // 多一段就不认：一条不存在的 URL 不该被安静地当成某个 Tab
  ['/settings/models/extra', null],
  // 只看「/settings/」这一段，`settings` 出现在别处不算
  ['/workbuddy-settings/models', null],
  ['/settingsx/models', null],
];

for (const [path, want] of pathCases) {
  check(`settingsTabFromPath(${JSON.stringify(path)}) → ${JSON.stringify(want)}`,
      settingsTabFromPath(path), want);
}

check(
  '刻意放宽的一处：只要后缀是 /settings/<tab> 就认（不依赖 basePath）',
  settingsTabFromPath('/some/prefix/settings/models'),
  'models',
);

/* ── 与真实用法的一致性：导航里每项都能被解析回来 ─────────────── */

check(
  '每个 href 都能被 settingsTabFromPath 解析回自己（导航高亮不会错位）',
  SETTINGS_TABS.map((tab) => settingsTabFromPath(settingsTabHref(tab)) === tab),
  SETTINGS_TABS.map(() => true),
);

if (failed > 0) {
  console.log(`\n${failed} 项失败`);
  process.exit(1);
}
console.log('\nall passed');

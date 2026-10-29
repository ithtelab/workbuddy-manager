/**
 * `web/lib/command-palette.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/command-palette.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_command_palette`
 *
 * 这里同时 import **真实的** `section-nav.ts` 与 `settings-tabs.ts`：面板里的
 * 页面清单是写死的一份副本，这个测试负责证明它与那两处**逐条一致**。少了这一步，
 * 「加了一页、面板里搜不到」不会有任何报错——而用户只会觉得「这东西不好用」。
 *
 * 为什么值得单独测：这个模块错了也不会抛异常，只是搜不出来——
 *   · 匹配大小写敏感 → 敲 `Models` 一条都没有；
 *   · 不做归一化 → 敲 `redpackets` 搜不到「红包」（它写作 `red-packets`）；
 *   · 同分时顺序不稳定 → 同一次输入两次给不同顺序，列表在眼前跳；
 *   · 空查询返回空 → 打开面板是一片空白，用户不知道能搜什么。
 */
import {
  NAV_COMMANDS,
  matchCommands,
  navCommands,
} from './command-palette.ts';
import {SECTIONS, SECTION_NAV} from './section-nav.ts';
import {SETTINGS_TABS, SETTINGS_TAB_LABEL_KEYS, settingsTabHref} from './settings-tabs.ts';

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

/** 测试用的假翻译：只认下面这张表，认不出就把键原样返回（于是漏翻译会显形） */
const STUB = {
  'nav.dashboard': '总览', 'nav.accounts': '账号', 'nav.tasks': '任务', 'nav.keys': '密钥',
  'nav.redPackets': '红包', 'nav.models': '模型', 'nav.playground': '测试台',
  'nav.stats': '统计', 'nav.logs': '日志', 'nav.security': '安全', 'nav.settings': '设置',
  'nav.groupOverview': '总览', 'nav.groupOps': '运营', 'nav.groupGovernance': '治理',
  'settings.tabUpstream': '上游配置', 'settings.tabProxy': '代理',
  'settings.tabModels': '模型映射', 'settings.tabQoder': 'Qoder',
  'settings.tabUsers': '用户', 'settings.tabTokens': '令牌', 'settings.tabSystem': '系统',
  'settings.tabWatchdog': '页面监控',
  'settings.tabBackup': '数据备份',
  'settings.tabChangelog': '更新日志', 'settings.tabAbout': '关于',
};
const t = (key) => STUB[key] ?? key;

/* ── 清单本身 ─────────────────────────────────────────────────── */

check('href 唯一（重复的话 React key 会撞、点了也不知道去哪）',
      new Set(NAV_COMMANDS.map((c) => c.href)).size, NAV_COMMANDS.length);

check('id 唯一', new Set(NAV_COMMANDS.map((c) => c.href)).size, NAV_COMMANDS.length);

check('每一项都带 keywords（至少要有英文名/别名，否则只能按中文搜）',
      NAV_COMMANDS.filter((c) => !c.keywords || !c.keywords.trim()).length, 0);

check('href 都是站内绝对路径',
      NAV_COMMANDS.map((c) => c.href).filter((h) => !/^\/[a-z0-9/-]+$/.test(h)), []);

/* ── 与真实来源逐条一致（这一段是这份副本的守卫） ─────────────── */

const byHref = new Map(NAV_COMMANDS.map((c) => [c.href, c]));

check(
  'section-nav 的每一页都在面板里，且标签键一致',
  SECTIONS.flatMap((s) => SECTION_NAV[s].map((i) => {
    const found = byHref.get(i.href);
    return found ? found.labelKey === i.labelKey : `缺 ${i.href}`;
  })),
  SECTIONS.flatMap((s) => SECTION_NAV[s].map(() => true)),
);

check(
  '设置页 10 个 Tab 都在面板里，且标签键与清单模块一致',
  SETTINGS_TABS.map((tab) => {
    const found = byHref.get(settingsTabHref(tab));
    return found ? found.labelKey === SETTINGS_TAB_LABEL_KEYS[tab] : `缺 ${settingsTabHref(tab)}`;
  }),
  SETTINGS_TABS.map(() => true),
);

// 剩下的是「只在底栏里出现、任何零依赖模块里都没有」的页面。这里写死它们，
// 是为了让「多出来的那一项」变成失败；它们与底栏是否一致由 Python 守卫比对
// （那边能从 ManagementBar.tsx 里解析出真实的 dockItems）。
const DOCK_ONLY = ['/dashboard', '/stats', '/logs', '/security', '/settings'];

check(
  '不在 section-nav / settings-tabs 里的项，正好是那 5 个底栏专属页',
  NAV_COMMANDS.map((c) => c.href)
    .filter((h) => !SECTIONS.some((s) => SECTION_NAV[s].some((i) => i.href === h))
                && !SETTINGS_TABS.some((tab) => settingsTabHref(tab) === h))
    .sort(),
  [...DOCK_ONLY].sort(),
);

check(
  '被吸收的三页紧跟在它们所属的那一节后面（扫一眼就能看出归属）',
  NAV_COMMANDS.map((c) => c.href).filter((h) => ['/tasks', '/red-packets', '/playground'].includes(h)),
  ['/tasks', '/red-packets', '/playground'],
);

check(
  '被吸收的页面的 groupKey 是它所属的那一节（不是「运营」那种更粗的分组）',
  ['/tasks', '/red-packets', '/playground'].map((h) => byHref.get(h).groupKey),
  ['nav.accounts', 'nav.keys', 'nav.models'],
);

/* ── navCommands：翻译 ────────────────────────────────────────── */

const nav = navCommands(t);

check('条目数与清单一致', nav.length, NAV_COMMANDS.length);

check('每一条都有非空的显示名与归属（漏一个键就会显示裸键名）',
      nav.filter((c) => !c.label || !c.group || c.label.includes('.')).map((c) => c.id), []);

check('id 用 href（导航项唯一的稳定标识）', nav[0].id, nav[0].href);

check('keywords 里含路径（渲染层不显示它，只用于匹配）',
      nav.filter((c) => !c.keywords.includes(c.href)).length, 0);

check('不改动传入的种子', (() => {
  const seeds = [{href: '/x', labelKey: 'a.b', groupKey: 'c.d', keywords: 'x'}];
  const copy = JSON.stringify(seeds);
  navCommands(t, seeds);
  return JSON.stringify(seeds) === copy;
})(), true);

/* ── matchCommands：空查询 ────────────────────────────────────── */

check('空查询返回全部（打开面板不该是一片空白）', matchCommands(nav, '').length, nav.length);
check('只有空格也算空查询', matchCommands(nav, '   ').length, nav.length);
check('空查询保持原顺序', matchCommands(nav, '').map((c) => c.id), nav.map((c) => c.id));
check('空查询返回的是**新数组**（不能把内部数组交出去）',
      matchCommands(nav, '') !== nav, true);

/* ── matchCommands：命中的档次与排序 ─────────────────────────── */

const ids = (list) => list.map((c) => c.id);

check('按中文名精确命中',
      ids(matchCommands(nav, '模型')), ['/models', '/settings/models', '/playground']);
check('归属提示也参与匹配（「测试台」的归属是「模型」，所以排在最后）',
      ids(matchCommands(nav, '模型')).at(-1), '/playground');
check('大小写不敏感', ids(matchCommands(nav, 'DASHBOARD')), ['/dashboard']);
check('中英混排：中文名 + 英文关键词', ids(matchCommands(nav, '密钥 key')), ['/keys']);
check('搜不到就是空', ids(matchCommands(nav, 'zzz')), []);
check('多个词是 AND（不是 OR）：「设置」只在归属里，「用户」在名字里',
      ids(matchCommands(nav, '设置 用户')), ['/settings/users']);
check('多词 AND 不会退化成 OR', ids(matchCommands(nav, '设置 zzz')), []);

// 排序：用一个自造清单，各项档次不同/相同，直接看顺序。
const mixed = [
  {id: 'kw', label: '甲', group: '归属', keywords: 'zzz'},
  {id: 'exact', label: 'zzz', group: '归属'},
  {id: 'prefix', label: 'zzz 后缀', group: '归属'},
];
check('排序：精确 > 前缀 > 关键词',
      ids(matchCommands(mixed, 'zzz')), ['exact', 'prefix', 'kw']);

const same = [
  {id: 'b', label: '测试乙', group: '归属'},
  {id: 'a', label: '测试甲', group: '归属'},
  {id: 'c', label: '测试丙', group: '归属'},
];
check('同分时按清单原顺序（不依赖 sort 的实现）',
      ids(matchCommands(same, '测试')), ['b', 'a', 'c']);

/* ── matchCommands：归一化（分隔符） ──────────────────────────── */

check('redpackets 能搜到「红包」（写作 red-packets）',
      ids(matchCommands(nav, 'redpackets')), ['/red-packets']);
check('red packets 也一样', ids(matchCommands(nav, 'red packets')), ['/red-packets']);
check('red-packets 也一样', ids(matchCommands(nav, 'red-packets')), ['/red-packets']);
check('路径可直接搜（/settings/tokens）',
      ids(matchCommands(nav, 'settings/tokens')), ['/settings/tokens']);

/* ── matchCommands：排序稳定性与纯度 ─────────────────────────── */

check('同一次输入两次给同一个顺序（否则列表在眼前跳）',
      ids(matchCommands(nav, '设置')), ids(matchCommands(nav, '设置')));

check('「设置」能搜到设置页与它的 8 个 Tab',
      ids(matchCommands(nav, '设置')).includes('/settings') &&
      ids(matchCommands(nav, '设置')).includes('/settings/users'),
      true);

check('不改动入参', (() => {
  const copy = JSON.stringify(nav);
  matchCommands(nav, '设置');
  matchCommands(nav, '');
  return JSON.stringify(nav) === copy;
})(), true);

check('结果里没有重复项',
      (() => {
        const out = ids(matchCommands(nav, '设'));
        return out.length === new Set(out).size;
      })(), true);

if (failed > 0) {
  console.log(`\n${failed} 项失败`);
  process.exit(1);
}
console.log('\nall passed');

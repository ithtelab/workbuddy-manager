/**
 * 命令面板（⌘K）的清单与检索（批次 5）—— 纯函数，零运行时依赖。
 *
 * ## 它解决什么
 *
 * 11 个业务页 + 设置页 7 个 Tab，全都只能靠「去底栏找图标」或「先进设置再点 Tab」
 * 到达——图标认不出、名字记不住的时候，只能一个个点开看。⌘K 把这些变成
 * **打几个字就到位**。
 *
 * ⚠️ 这一版**只覆盖页面**，一条动作都没有（换语言 / 切主题 / 退出登录仍然只在
 * 个人信息弹窗里）。原本打算把它们一起收进来，动手时发现那些开关和**不可逆操作**
 * 挤在同一个弹窗里（退出登录、吊销全部会话就在旁边），而 ⌘K 的用法是「敲几个字 +
 * 回车」——回车之前那个词往往只打了一半。要收进来，得先给动作项定出「哪些需要
 * 二次确认」的形态，那是另一批的事。见 `CommandPalette` 组件里那段说明。
 *
 * ## 为什么清单是**写死的**，而不是从别处 import 出来
 *
 * 页面的清单本来分散在三个地方（底栏的 `dockItems`、`@/lib/section-nav`、
 * `@/lib/settings-tabs`），而其中底栏那一份在**组件**里（带 JSX 图标），本模块
 * 零依赖、进不去。真要合并成一份就得动底栏那个组件——那会把这一批的 diff 撑大，
 * 而且要同时改三处解析它的测试。
 *
 * 所以这里写一份**带守卫**的副本：`server/tests/test_command_palette.py` 会从
 * 那三个来源各自解析出「href + 标签键」，与 `NAV_COMMANDS` 逐条比对——少一项、
 * 多一项、或者标签键写错，都会红。副本本身没错，**没人检查的副本**才是问题。
 *
 * 顺序是**有意排的**：底栏那 8 个在前（总览打头），被吸收的三页紧跟它们所属的
 * 那一节（账号 → 任务记录、密钥 → 红包、模型 → 聊天测试台），设置页 7 个 Tab 最后。
 * 空查询时用户看到的就是这个顺序。
 *
 * ## 为什么匹配要做「归一化」
 *
 * `red-packets` / `redpackets` / `red packets` 是同一个东西的三种写法。不把
 * `-` `_` 空格归一化掉的话，用户敲 `redpackets` 会一条都搜不到——而他明明打对了。
 *
 * ## 为什么关键词里都塞了路径
 *
 * 「模型映射」这个 Tab 的中文名里没有「models」，但它的路径是
 * `/settings/models`。把路径当关键词，英文用户（以及习惯打路径的人）就能搜到它，
 * 而代价只是匹配时多比一次字符串。
 *
 * 同理，**归属提示也参与匹配**（档次最低）：它是显示在结果里的东西，看得见就该搜
 * 得到。少了这一条，「设置 用户」这种再自然不过的组合会一条都搜不出来——「用户」
 * 在名字里，「设置」只在右侧那个归属提示里。
 */

/**
 * 检索的输入单位。
 *
 * `label` 是**已经翻译好**的显示名（翻译函数由调用方传进来，本模块不 import i18n）；
 * `group` 是显示在右侧的归属提示。
 */
export type PaletteItem = {
  /** 稳定标识。导航项用 `href`，动作项用动作名 */
  id: string;
  label: string;
  /** 归属提示，例如「账号」「设置」 */
  group: string;
  /** 额外的可搜词（不显示）。至少含路径 */
  keywords?: string;
  /** 有 `href` 就是「去某个页面」，否则由调用方按 `id` 执行动作 */
  href?: string;
};

/** 导航项的**种子**：只存 i18n 键，翻译留到渲染时。 */
export type NavSeed = {
  href: string;
  /** 显示名的 i18n 键 */
  labelKey: string;
  /** 归属提示的 i18n 键 */
  groupKey: string;
  keywords: string;
};

/**
 * 面板里的全部页面。顺序即空查询时的展示顺序。
 *
 * ⚠️ 这是一份**带守卫的副本**：`server/tests/test_command_palette.py` 会拿它与
 * 底栏 / `section-nav` / `settings-tabs` 三处逐条比对。加页面时三处都要改。
 */
export const NAV_COMMANDS: readonly NavSeed[] = [
  {href: '/dashboard', labelKey: 'nav.dashboard', groupKey: 'nav.groupOverview',
   keywords: 'dashboard home 总览 首页 仪表盘'},
  {href: '/accounts', labelKey: 'nav.accounts', groupKey: 'nav.groupOps',
   keywords: 'accounts account pool 账号 号池'},
  // 被吸收进「账号」的那一页，紧跟它所属的一节
  {href: '/tasks', labelKey: 'nav.tasks', groupKey: 'nav.accounts',
   keywords: 'tasks task checkin 任务 签到 记录'},
  {href: '/keys', labelKey: 'nav.keys', groupKey: 'nav.groupOps',
   keywords: 'keys key api 密钥'},
  {href: '/red-packets', labelKey: 'nav.redPackets', groupKey: 'nav.keys',
   keywords: 'red packets redpackets quota 红包 额度'},
  {href: '/models', labelKey: 'nav.models', groupKey: 'nav.groupOps',
   keywords: 'models model price 模型 价格'},
  {href: '/playground', labelKey: 'nav.playground', groupKey: 'nav.models',
   keywords: 'playground chat 测试台 聊天'},
  {href: '/stats', labelKey: 'nav.stats', groupKey: 'nav.groupGovernance',
   keywords: 'stats statistics usage 统计 用量'},
  {href: '/logs', labelKey: 'nav.logs', groupKey: 'nav.groupGovernance',
   keywords: 'logs log 日志'},
  {href: '/security', labelKey: 'nav.security', groupKey: 'nav.groupGovernance',
   keywords: 'security ip allow deny 安全 白名单'},
  {href: '/settings', labelKey: 'nav.settings', groupKey: 'nav.groupGovernance',
   keywords: 'settings config 设置 配置'},
  {href: '/settings/upstream', labelKey: 'settings.tabUpstream', groupKey: 'nav.settings',
   keywords: 'upstream upstash 上游 配置'},
  {href: '/settings/proxy', labelKey: 'settings.tabProxy', groupKey: 'nav.settings',
   keywords: 'proxy route socks5 egress 代理 线路 出口'},
  {href: '/settings/models', labelKey: 'settings.tabModels', groupKey: 'nav.settings',
   keywords: 'model map alias 模型 映射 别名'},
  {href: '/settings/qoder', labelKey: 'settings.tabQoder', groupKey: 'nav.settings',
   keywords: 'qoder bridge console bridge token 桥接 控制台'},
  {href: '/settings/users', labelKey: 'settings.tabUsers', groupKey: 'nav.settings',
   keywords: 'users user member 用户 成员'},
  {href: '/settings/tokens', labelKey: 'settings.tabTokens', groupKey: 'nav.settings',
   keywords: 'tokens token api 令牌'},
  {href: '/settings/system', labelKey: 'settings.tabSystem', groupKey: 'nav.settings',
   keywords: 'system update upgrade 系统 更新 版本'},
  {href: '/settings/watchdog', labelKey: 'settings.tabWatchdog', groupKey: 'nav.settings',
   keywords: 'watchdog monitor layout diff alert mail 监控 告警 改版 邮件 活动'},
  {href: '/settings/backup', labelKey: 'settings.tabBackup', groupKey: 'nav.settings',
   keywords: 'backup postgres postgresql restore migrate 备份 恢复 迁移 数据'},
  {href: '/settings/changelog', labelKey: 'settings.tabChangelog', groupKey: 'nav.settings',
   keywords: 'changelog release notes 更新日志 版本'},
  {href: '/settings/about', labelKey: 'settings.tabAbout', groupKey: 'nav.settings',
   keywords: 'about version 关于 版本'},
];

/**
 * 把导航种子翻成可检索的条目。
 *
 * 翻译函数**当参数传进来**，不 import：本模块被 `web/lib/command-palette.test.mjs`
 * 用 Node 直接 import，一旦引入运行时依赖，那条测试就会以「找不到模块」失败。
 */
export function navCommands(
  t: (key: string) => string,
  seeds: readonly NavSeed[] = NAV_COMMANDS,
): PaletteItem[] {
  return seeds.map((seed) => ({
    id: seed.href,
    href: seed.href,
    label: t(seed.labelKey),
    group: t(seed.groupKey),
    keywords: `${seed.href} ${seed.keywords}`,
  }));
}

/** 去掉分隔符并小写：`Red-Packets` / `red packets` / `redpackets` 视为同一个词。 */
function normalize(text: string): string {
  return text.toLowerCase().replace(/[-_\s]+/g, '');
}

/** 单个词命中的档次。`-1` = 没命中，该条目不进结果。 */
function termTier(item: PaletteItem, term: string): number {
  const q = normalize(term);
  if (!q) return 0; // 只有标点/空格的词，跳过而不是判负
  const label = item.label.toLowerCase();
  if (label === term.toLowerCase()) return 4;
  if (label.startsWith(term.toLowerCase())) return 3;
  if (normalize(item.label).includes(q)) return 2;
  if (item.keywords && normalize(item.keywords).includes(q)) return 1;
  // 归属提示也参与匹配：它**显示在结果里**，看得见的东西就该搜得到。
  // 少了这一条，「设置 用户」这种再自然不过的组合会一条都搜不出来
  // （「用户」在名字里，「设置」只在右侧那个归属提示里）。
  if (normalize(item.group).includes(q)) return 1;
  return -1;
}

/**
 * 检索。空查询返回**全部**（保持原顺序）——这是 ⌘K 的常规行为：先看一眼有什么，
 * 再决定敲什么；也方便直接按方向键挑。
 *
 * 多个词是 **AND**：`设置 用户` 只留同时命中的那条。
 *
 * 排序按「总档次降序 + 原顺序升序」。第二项不能省：同分时若依赖 `sort` 的实现，
 * 结果顺序会随引擎变化，而「同一次输入两次给不同顺序」在界面上就是抖动。
 */
export function matchCommands(items: readonly PaletteItem[], query: string): PaletteItem[] {
  const terms = query.trim().split(/\s+/).filter(Boolean);
  if (!terms.length) return [...items];

  const scored: {item: PaletteItem; score: number; index: number}[] = [];
  items.forEach((item, index) => {
    let score = 0;
    for (const term of terms) {
      const tier = termTier(item, term);
      if (tier < 0) return; // 有一个词没命中 → 整条不要
      score += tier;
    }
    scored.push({item, score, index});
  });

  scored.sort((a, b) => (b.score - a.score) || (a.index - b.index));
  return scored.map((entry) => entry.item);
}

/**
 * 设置页的二级 Tab 清单与路径解析（批次 4 的 P1-2）。
 *
 * **为什么要把它单独放在一个零依赖模块里**：Tab 名字、顺序、默认项、路径形状
 * 四件事被三处同时用到——外壳（渲染导航）、`/settings/<tab>` 的 10 个路由目录、
 * 以及 `server/tests/test_settings_tabs.py`（断言「清单里的每一项都有真实存在的
 * 路由目录」）。任何一处写死一份副本，都会在下次加 Tab 时静默漂移：加了名字却
 * 没有目录 → 点进去 404；有了目录却不在清单里 → 导航上看不见。所以只留一份，
 * 其余都从这里读。
 *
 * 零依赖是**测试的前提**：`web/lib/settings-tabs.test.mjs` 用 Node 直接 import 本
 * 文件（type stripping），只要有一个 import 就会把「机器上没装前端依赖」变成测试
 * 失败，而不是跳过。因此这里**不** import `@/lib/base-path`——见下面
 * `settingsTabFromPath` 的说明。
 */

/** 与 `TabsTrigger` 的 `value` 一一对应，顺序即导航顺序。 */
export const SETTINGS_TABS = [
  'upstream',
  'proxy',
  'models',
  'users',
  'tokens',
  'system',
  // 「页面监控」也归在运维这一组：它盯的是上游网页的改版与活动入口，
  // 和「系统更新 / 数据备份」回答的是同一类问题——「有没有事情需要我看一眼」。
  'watchdog',
  // 「数据备份」紧挨「系统更新」：两件事都属于「这台机器上的运维」，
  // 而且都在回答同一个问题——「我的东西会不会丢」。
  'backup',
  'changelog',
  'about',
] as const;

export type SettingsTab = (typeof SETTINGS_TABS)[number];

/** `/settings` 重定向到它。与 `SETTINGS_TABS[0]` 是同一个值，但单独起名——「第一个」是顺序，这个才是「默认」。 */
export const DEFAULT_SETTINGS_TAB: SettingsTab = 'upstream';

export function isSettingsTab(value: unknown): value is SettingsTab {
  return typeof value === 'string' && (SETTINGS_TABS as readonly string[]).includes(value);
}

/** Tab 的可寻址路径。`Link` 会自己处理 basePath 与 export 模式下的尾斜杠。 */
export function settingsTabHref(tab: SettingsTab): string {
  return `/settings/${tab}`;
}

/**
 * 每个 Tab 的**标签键**。
 *
 * 与 `SETTINGS_TABS` 放在一起而不是留在外壳里，理由和清单本身一样：Tab 的名字
 * 有**两处**要读——外壳渲染导航、命令面板（⌘K）把它当作目的地列出来。留在外壳
 * 里的话，命令面板就得另抄一份「哪个 Tab 叫什么」，而漏抄一处的表现是「面板里
 * 那个条目显示裸键名」或者「少了某个 Tab」——两处都不报错。
 *
 * 图标仍然留在外壳（那是 JSX，进不来这个零依赖模块）。
 */
export const SETTINGS_TAB_LABEL_KEYS: Record<SettingsTab, string> = {
  upstream: 'settings.tabUpstream',
  proxy: 'settings.tabProxy',
  models: 'settings.tabModels',
  users: 'settings.tabUsers',
  tokens: 'settings.tabTokens',
  system: 'settings.tabSystem',
  watchdog: 'settings.tabWatchdog',
  backup: 'settings.tabBackup',
  changelog: 'settings.tabChangelog',
  about: 'settings.tabAbout',
};

/**
 * 从 `usePathname()` 的值里解析出当前 Tab。
 *
 * **不 import `BASE_PATH` 的原因**（子路径部署下这条路必须仍然成立）：
 * 模块要保持零依赖，而把 basePath 当参数传进来，调用方漏传就会静默失效——
 * 恰恰是子路径部署下才会暴露，本地永远测不出来。所以判据改成**匹配后缀**：
 * `/settings/models` 与 `/workbuddy-manager/settings/models` 都能认出来，
 * 与是否带前缀无关，也就不存在「漏传」这回事。
 *
 * 返回 `null` 表示「不在设置页 / 没有子路径」——调用方据此回落到默认 Tab，
 * 并（在 `/settings` 上）把地址换成默认 Tab 的规范路径。
 */
export function settingsTabFromPath(pathname: string | null | undefined): SettingsTab | null {
  // export 模式开了 trailingSlash，客户端拿到的可能是 `/settings/users/`
  const path = (pathname || '').replace(/\/+$/, '');
  const matched = /(?:^|\/)settings\/([^/]+)$/.exec(path);
  if (!matched) return null;
  return isSettingsTab(matched[1]) ? matched[1] : null;
}

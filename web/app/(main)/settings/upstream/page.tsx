/**
 * `/settings/upstream` —— 设置页第一个 Tab 的路由（也是 `/settings` 重定向的目标）。
 *
 * 这里**只做占位**：整个设置页的外壳（标题 + 二级导航 + 当前 Tab 的内容）挂在同级
 * `../layout.tsx` 上。原因是这一页的表单状态很重 —— 配置副本、乐观值、改动基线，
 * 而 App Router 在切子路由时会重挂载 `page`。把内容放进 page 就意味着每次切 Tab 都
 * 丢掉这些状态（或者把几十项状态提到 context 再发下去）。所以：
 *
 *   · `layout.tsx` 不随子路由重挂载 → 状态活着；
 *   · 每个 Tab 一个 `page.tsx`，只为了让地址可收藏、可分享、可后退；
 *   · 内容一律由外壳按 `usePathname()` 解析出的 Tab 渲染。
 *
 * `server/tests/test_settings_tabs.py` 钉住这条结构：每个 Tab 都要有真实路由目录、
 * 每个 `page.tsx` 都返回 null、取数只能发生在 `layout.tsx` 里。
 */
export default function SettingsUpstreamRoute() {
  return null;
}

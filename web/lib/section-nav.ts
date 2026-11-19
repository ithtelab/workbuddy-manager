/**
 * 页面内二级导航的清单与路径解析（批次 4 ②）—— 纯函数，零运行时依赖。
 *
 * ## 为什么需要它
 *
 * 底栏原来是 **11 项平铺**，其中三对其实是「同一件事的两半」——
 * 账号 / 任务记录、密钥 / 红包、模型 / 聊天测试台。用户在两分钟内必然要
 * 一起看的两页，不该各占一个一级入口：入口越多，每个入口能分到的注意力
 * 越少，最后等于没有入口。收敛到 8 项之后，被吸收的那三页改成页面内的
 * 二级导航（`PageSectionTabs` 渲染的就是本模块的清单）。
 *
 * ## 为什么 `href` 是**平级**的（`/tasks` 而不是 `/accounts/tasks`）
 *
 * 路线图里原本写的是「并入账号管理，成为 `/accounts/tasks` 子路由，
 * 旧路径做重定向」。真正动手时改成了平级路径，理由是**改名的代价**：
 * 全仓有 8 处（4 个测试 + 4 个验收脚本）按**路径**钉住这三页，搬文件就要
 * 同步改这 8 处，而漏改的表现是「某条测试报了个看不懂的错」——这一批已经
 * 因为同类原因踩过一次。而路线图真正要的结果是「底栏 8 项 + 页内二级
 * 导航」，平级路径**同样满足**，还省掉一次重定向跳转（旧深链直接可用，
 * 不必先跳一下）。URL 层级没那么好看，但这是一次**可逆**的取舍：将来真要
 * 嵌套，只需把这里的 `href` 改掉并同步那 8 处。
 *
 * ## 为什么是「剥掉部署前缀后**精确匹配**」，而不是「匹配路径后缀」
 *
 * 第一版写的是后缀匹配（`path.endsWith(item.href)`），目的是绕开 basePath 这个
 * 参数——它在任意前缀下都对。但**代价是把别的路径也吸了进来**：`/settings/models`
 * 以 `/models` 结尾，于是**设置页会被认成「模型」节**，凭空长出两条指向别处的
 * Tab（`/models/playground` 同理）。子路径部署是个**可选**形态，这个碰撞却在
 * **默认形态下就会发生**，取舍反了。
 *
 * 所以改成两步：先剥掉部署前缀，再要求路径**恰好等于** `href`。前缀由调用方
 * 传进来（`basePath` 是**必填参数**，漏传 TypeScript 会拦住；本模块不能自己
 * `import` `@/lib/base-path`，那就不是零依赖了，见下）。剥掉之后
 * `/settings/models`、`/models/playground`、`/tasksx` 一律认不出——认不出就
 * 什么都不渲染，是安全的那一侧。
 *
 * 剥前缀**不需要**再判断「前缀后面紧跟 `/`」：精确匹配本身就把它兜住了（见
 * `sectionNavFromPath` 里的注释）。
 *
 * 前缀**不在场**时按原样比：根路径部署走的就是这一支（前缀是空串）；
 * 「调用方传了前缀、路径里却没有」也照常显示，而不是判成认不出——认不出的表现
 * 是二级导航整排消失，比多显示一排更糟。
 *
 * ## 为什么图标只存名字、不存组件
 *
 * `web/lib/section-nav.test.mjs` 直接 import 本模块（Node ≥ 22.6 的 type
 * stripping）。一旦这里 `import` 了 React 或图标组件，那条测试会以「找不到
 * 模块」失败——Node 的 ESM 解析要求带扩展名，而仓库里的 app 代码一律不写扩展名。
 * 图标由 `PageSectionTabs` 按 `iconKey` 映射。
 */

/** 图标标识。名字与 `PageSectionTabs` 里的映射表一一对应。 */
export type SectionNavIconKey =
  | 'accounts'
  | 'tasks'
  | 'keys'
  | 'redPackets'
  | 'models'
  | 'playground';

export type SectionNavItem = {
  /** 稳定标识，与 `href` 一一对应。测试与调试用。 */
  key: string;
  /** 站内绝对路径，**不带** basePath（`Link` 自己会加）。 */
  href: string;
  /** 标签文案的 i18n 键。复用底栏的 `nav.*`：文案本来就短（「账号」「任务」），
   *  当页内 Tab 用不会和页面大标题（「账号管理」）读起来重复。 */
  labelKey: string;
  iconKey: SectionNavIconKey;
};

/** 有二级导航的「节」。顺序即 `sectionNavFromPath` 的匹配顺序。 */
export const SECTIONS = ['accounts', 'keys', 'models'] as const;

export type SectionKey = (typeof SECTIONS)[number];

/**
 * 每一节包含哪些页。
 *
 * 每节**第一项是该节的落点**（底栏点进去就是它）——`PageSectionTabs` 与
 * `server/tests/test_section_nav.py` 都依赖这条约定，别把顺序改了。
 */
export const SECTION_NAV: Record<SectionKey, readonly SectionNavItem[]> = {
  accounts: [
    {key: 'accounts', href: '/accounts', labelKey: 'nav.accounts', iconKey: 'accounts'},
    {key: 'tasks', href: '/tasks', labelKey: 'nav.tasks', iconKey: 'tasks'},
  ],
  keys: [
    {key: 'keys', href: '/keys', labelKey: 'nav.keys', iconKey: 'keys'},
    {key: 'redPackets', href: '/red-packets', labelKey: 'nav.redPackets', iconKey: 'redPackets'},
  ],
  models: [
    {key: 'models', href: '/models', labelKey: 'nav.models', iconKey: 'models'},
    {key: 'playground', href: '/playground', labelKey: 'nav.playground', iconKey: 'playground'},
  ],
};

export function isSectionKey(value: unknown): value is SectionKey {
  return typeof value === 'string' && (SECTIONS as readonly string[]).includes(value);
}

/** 该节的所有项（顺序即渲染顺序）。 */
export function sectionNavItems(section: SectionKey): readonly SectionNavItem[] {
  return SECTION_NAV[section];
}

export type SectionNavLocation = {
  section: SectionKey;
  /** 当前路径落在哪一项上（`href` 与路径匹配的那一项）。 */
  item: SectionNavItem;
};

/**
 * 路径 → 「它属于哪一节、停在哪一项」。认不出返回 `null`。
 *
 * `basePath` **必填**：`usePathname()` 在子路径部署下会带上部署前缀
 * （`/workbuddy-manager/tasks`），要先剥掉才能与 `href` 比。写成可选参数的话
 * 「忘了传」不会报错，只在子路径部署下悄悄失效；必填参数至少让 TypeScript
 * 在编译期拦住。
 *
 * 认不出就返回 `null`（而不是回落到第一项）：调用方据此**什么都不渲染**。
 * 回落到第一项会让一个不属于任何节的页面凭空长出两条指向别处的 Tab，
 * 那比不渲染更糟。
 */
export function sectionNavFromPath(
  pathname: string | null | undefined,
  basePath: string,
): SectionNavLocation | null {
  // 去掉尾部斜杠：静态导出模式下 Next 会给路径补尾斜杠
  const raw = (pathname || '').replace(/\/+$/, '');
  const prefix = (basePath || '').replace(/\/+$/, '');

  // 前缀「在场才剥」：不在场时按原样比——根路径部署（`prefix === ''`）走的就是
  // 这一支。反过来把「调用方传了前缀、路径里却没有」判成 null，只会让二级导航
  // 整排消失，照常显示更安全。
  //
  // 这里**不需要**再要求「前缀后面紧跟 `/`」：匹配要求 `path` 恰好等于 `href`，
  // 而 `href` 一律以 `/` 开头，所以前缀后面不是 `/` 的路径剥完必然不匹配
  // （`/wb` 剥 `/wbtasks` 得到 `tasks`，不等于 `/tasks`）。少一个看不出效果的
  // 守卫，就少一条没人能验证的规则。
  const path = prefix && raw.startsWith(prefix) ? raw.slice(prefix.length) : raw;

  if (!path) return null;

  for (const section of SECTIONS) {
    for (const item of SECTION_NAV[section]) {
      // 精确匹配：`endsWith` 会让 `/settings/models` 命中 `/models`，
      // 设置页于是长出两条指向别处的 Tab（见文件顶部说明）。
      if (path === item.href) {
        return {section, item};
      }
    }
  }
  return null;
}

/**
 * 底栏（浮动 Dock）的分组切分——纯函数，零运行时依赖。
 *
 * 为什么单独放一个模块：底栏原来靠一个**哨兵条目**表达分组——
 * `{title: 'divider', icon: <div />}` 混在 `items` 里，渲染时再靠
 * `item.title === 'divider'` 把它挑出来当分隔线用。两个后果：
 *
 *   1. 每个渲染分支都得记得跳过它（`floating-dock.tsx` 里就有一句
 *      `if (item.title === 'divider') return null`），漏一处就会渲染出一个空图标；
 *   2. **手机端因此完全没有分组语义**——移动端那个分支直接 `return null` 跳过了
 *      分隔线，于是手机上 11 项平铺，用户看不出哪几项是一类。
 *
 * 改成「分组键挂在条目上、由渲染层自己切」之后，桌面端和移动端可以各自决定
 * 怎么画分组（桌面画竖线 + hover 组名，移动端画带组名的横线），也就能被测试直接盖住。
 *
 * 之所以做成零依赖的纯函数：`web/lib/dock-groups.test.mjs` 直接 import 它，
 * 引入任何运行时 import 都会让那条测试以「找不到模块」失败（Node 的 ESM 解析
 * 要求带扩展名，而仓库里的 app 代码一律不写扩展名）。
 */

export type DockGroupItem = {
  title: string;
  /** 分组键。相邻两项的 `groupKey` 不同时，两组之间插一条分隔线。 */
  groupKey?: string;
  /** 分组名（调用方已翻译）。留空则画一条不带标签的分隔线。 */
  groupLabel?: string;
};

export type DockGroup<T> = {
  key: string;
  label: string;
  items: T[];
};

/**
 * 把扁平条目按**相邻**的 `groupKey` 切成若干组。
 *
 * 判据是「相邻」而不是「相等」：同一组必须连在一起。这样调用方不必保证
 * 同组的条目在数组里严格连续，但也**不会**把散落两处的同名分组悄悄合并——
 * 合并会让底栏上出现两个都叫「运营」的分组，读起来像重复了。
 */
export function splitByGroup<T extends DockGroupItem>(items: T[]): DockGroup<T>[] {
  const groups: DockGroup<T>[] = [];
  for (const item of items) {
    const key = item.groupKey ?? '';
    const last = groups[groups.length - 1];
    if (last && last.key === key) {
      last.items.push(item);
      continue;
    }
    groups.push({key, label: item.groupLabel ?? '', items: [item]});
  }
  return groups;
}

/*
 * 这里**刻意没有** `countGroups`。渲染层判断「要不要画分隔线」用的是
 * `index > 0`（见 `floating-dock.tsx` 的两个分支）：第一组之前没有分隔线，
 * 单组时自然一条都不画，与「分组数」无关。多一个没人调用的导出，只会让
 * 「单组也不该画线」这条规则有两个说法。
 */

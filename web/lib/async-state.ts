/**
 * 「并发取数」的状态推导 —— **纯函数，不依赖 React、不依赖任何包**。
 *
 * 为什么单独放一个模块：这里的两条规则都很容易在重构时被「顺手简化」掉，而简化
 * 之后界面不会报错、只是变得不对劲——骨架每隔一个心跳闪一次、或者刷新时某个请求
 * 超时就把已经显示出来的内容清空。这类错误没有运行时断言就只能等用户发现，
 * 所以抽成纯函数，让 `async-state.test.mjs` 能直接测**真实实现**
 * （Node 直接跑，不需要 React、DOM 或 node_modules）。
 *
 * 各状态在界面上的含义见 lib/use-async-data.ts 的注释。
 */

/** initial = 换了数据上下文（例如切换版本）；refresh = 同一上下文下重取 */
export type FetchMode = 'initial' | 'refresh';

export interface AsyncValues {
  /** 已成功取到的字段 */
  values: Record<string, unknown>;
  /** 失败的字段 → 原因。成功的字段不会出现在这里 */
  errors: Record<string, unknown>;
}

/**
 * 把一次并发取数的结果拆成「成功的字段」与「失败的字段」。
 *
 * 逐项独立成败：一份数据挂了不该拖垮其余几份（用 allSettled 而不是 all 的
 * 全部意义就在这里）。若某次刷新有 2/5 个请求失败，界面应当仍然拿得到另外
 * 3 份新数据，而不是整页变空。
 */
export function splitSettled(
  keys: readonly string[],
  settled: readonly PromiseSettledResult<unknown>[],
): AsyncValues {
  const values: Record<string, unknown> = {};
  const errors: Record<string, unknown> = {};
  settled.forEach((r, i) => {
    const key = keys[i];
    if (key === undefined) return; // 结果比键多：正常不会发生，忽略而不是塞个 undefined 键
    if (r.status === 'fulfilled') values[key] = r.value;
    else errors[key] = r.reason;
  });
  return {values, errors};
}

/**
 * 界面该显示什么。三者互斥（有数据 / 没数据，请求在飞 / 已结束）。
 *
 *  · isInitialLoading —— 一次都没成功过，且请求还在飞 → 显示骨架。
 *    **判据是「没有数据」，不是「请求在飞」**：有心跳轮询的页面若拿后者当判据，
 *    骨架会每隔一个心跳闪一次，用户以为页面在抽风。这是本模块存在的首要原因。
 *  · isInitialFailed —— 请求已结束、一个字段都没成功、且有失败 → 显示错误态与
 *    「重试」。既不能一直转圈（用户永远等不到结果），也不能显示「暂无数据」
 *    （那是撒谎：数据是没取到，不是没有）。
 *  · isRefreshing —— 已有数据、请求在飞 → 内容保持不变，只做 aria-busy 这类轻提示。
 *
 * 三者都为 false 只剩一种情形：有数据、请求结束、无失败——即一切就绪。
 */
export function asyncFlags(
  values: Record<string, unknown>,
  errors: Record<string, unknown>,
  pending: boolean,
): {isInitialLoading: boolean; isInitialFailed: boolean; isRefreshing: boolean} {
  const hasData = Object.keys(values).length > 0;
  const hasErrors = Object.keys(errors).length > 0;
  return {
    isInitialLoading: pending && !hasData,
    isInitialFailed: !pending && !hasData && hasErrors,
    isRefreshing: pending && hasData,
  };
}

/**
 * 两组依赖的指纹。
 *
 * `context` 是**数据上下文**（切换国内版/国际版这类），`query` 是**同一上下文里的
 * 查询范围**（翻页、改统计时段这类）。两者对界面的要求恰好相反，所以要分开记。
 */
export interface DepPrints {
  /** 数据上下文。用 `null` 表示「还没跑过首屏」，于是首次一定算出 initial */
  context: string | null;
  /** 查询范围 */
  query: string;
}

/**
 * 依赖变化 → 该用哪种取数模式。返回 `null` 表示什么都没变，不必重取。
 *
 * 为什么非要区分这两种依赖：翻页和切版本的要求正好相反。
 *
 *  · **切版本 = 换了数据上下文**，屏幕上的数字属于旧版本，**必须清掉**。
 *    标题已经写着国际版、数字还是国内版的，比空着更误导。
 *  · **翻页 / 改时段 = 只是换了个查询范围**，**旧内容要留在屏幕上直到新数据到达**。
 *    若也清空，每翻一页闪一次骨架——而翻页是高频操作，看起来像页面在抽搐。
 *
 * 两者同时变化时按「上下文变了」处理：先清空、再立刻把旧数据合并回来毫无意义，
 * 而且会白发一次请求（切版本时页码归 1 就会同时命中这两条）。
 */
export function depMode(prev: DepPrints, next: DepPrints): FetchMode | null {
  if (prev.context !== next.context) return 'initial';
  if (prev.query !== next.query) return 'refresh';
  return null;
}

/**
 * 账号可用性的**单一判定来源**（账号页与首页共用）。
 *
 * 为什么要有这个模块：同一个账号，首页说「在线」、账号列表说「未加载」——
 * 用户看到的是一张自相矛盾的面板。根因是两页各算各的：
 *
 *   · 首页的健康快照直接用了 `expiryVisual()` 的 `label`，而那个分档描述的是
 *     **Token 有效期**（绿=还有很久），并不是「账号可用」。于是只要 token 没过期
 *     就显示「在线」，账号在不在上游池里、有没有被禁用冷却，一概没看。
 *   · 账号列表走的是另一套：合并上游池状态后按 禁用→过期→状态未知→冷却→
 *     一直失败→未加载→在线 的顺序判定。
 *
 * 同一个事实被两处独立推导，迟早会不一致（这次就是）。所以把「合并上游状态」
 * 与「判定可用性分档」都收在这里，两页只负责**各自怎么画**，不再各自判断。
 *
 * 注意分档顺序**不可随意调整**，它编码了优先级：
 *   disabled → expired → unknown → cooling → neverSucceeded → notLoaded → online
 * 例如「上游状态取不到」必须早于「不在池里」——否则会把「看不到上游」
 * 误报成「账号文件坏了」（这个误报曾经真实发生过，见下）。
 *
 * 文件下半部分是**跨分组聚合**用的纯逻辑（`accountGroups` / `tagGroup` /
 * `accountKey` / `interleave` / `poolTotals`）：仪表盘要把每个分组各取一份的
 * 账号合成一份「全部」的视图（上游 issue #94 问题 2），而合成的每一步都要走
 * `mergePoolStatus`，所以放在同一个模块里。
 *
 * ⚠️ 本模块**不得引入任何运行时 import**（只有 `import type`，编译后被抹掉）。
 * 这是仓库给「可在 Node 里直接测的前端逻辑」定的规矩：`web/lib/*.test.mjs`
 * 用 `node --experimental-strip-types` 直接 import `.ts` 源码，Node 的 ESM
 * 解析要求带扩展名，而仓库的 app 代码一律不写扩展名——一旦这里 import 了别的
 * 模块的**值**，测试就会以「找不到模块」失败，或者逼着全仓库改成 `.ts` 后缀。
 * 要复用别的模块的函数，就把那个模块也做成零运行时依赖的。
 */
import type {Account, UpstreamEndpoint, UpstreamStatus} from './types';

/** 账号当前的可用性分档 */
export type AvailabilityTier =
  | 'disabled'
  /** 本面板主动临时禁用（issue #21）：文件名带 .disabled，上游不加载它 */
  | 'disabledByPanel'
  /** 上游手动停用位（issue #45）：还在池里、任务照常，只是不被选中转发 */
  | 'manualDisabled'
  | 'expired'
  /** 本次读不到上游状态，运行时字段全部未知（`upstream.connected !== true`） */
  | 'unknown'
  | 'cooling'
  /** 有累计错误且从未成功过——上游对这类 4xx「只换号不罚」，状态看着正常却一直失败 */
  | 'neverSucceeded'
  /** 上游明确没加载它（不在池里，永远选不中） */
  | 'notLoaded'
  | 'online';

/**
 * 把上游 `/status` 的账号池状态合并进本地账号列表。
 *
 * `poolKnown` 这层判断是必须的：上游连接失败时 `/status` 仍返回 200，只是
 * `connected:false` 且没有账号列表。若不看这个标记，池就是空的，于是**每个账号
 * 都被判成「不在池里」**——界面把「连不上上游」误报成「账号文件坏了」，
 * 而账号页有 30 秒心跳，任何一次抖动都会命中、30 秒后又自己恢复，用户会以为
 * 账号随机坏掉。故此时如实标成「未知」，而不是「未加载」。
 */
export function mergePoolStatus(
  accounts: Account[],
  upstream: UpstreamStatus | null,
): Account[] {
  const pool = new Map<string, Record<string, unknown>>();
  for (const item of upstream?.accounts ?? []) {
    const uid = String(
      (item as Record<string, unknown>).uid ?? (item as Record<string, unknown>).UID ?? '',
    );
    if (uid) pool.set(uid, item as Record<string, unknown>);
  }
  const poolKnown = upstream?.connected === true;
  return accounts.map((a) => {
    // 上游状态取不到 → 不判断它在不在池里，如实标成「未知」
    if (!poolKnown) return {...a, in_pool: undefined, poolUnknown: true};
    const p = pool.get(a.uid);
    // in_pool 由**本次这份上游快照**判定，而不是沿用后端那个标记：
    // 本页其余状态字段都取自这一份数据，若单独用另一时刻的标记，两者可能
    // 互相矛盾（它们是两次 /status 调用的结果）。没进池的账号保留本地字段
    // （含 invalid_reason），交给徽章如实展示。
    if (!p) return {...a, in_pool: false, poolUnknown: false};
    return {
      ...a,
      in_pool: true,
      poolUnknown: false,
      healthy: typeof p.healthy === 'boolean' ? p.healthy : null,
      disabled: typeof p.disabled === 'boolean' ? p.disabled : null,
      disabled_reason: typeof p.disabled_reason === 'string' ? p.disabled_reason : '',
      // 上游的手动停用状态位（issue #45）。取的是**这份快照**的值——用户点了
      // 停用后状态位由上游持有，本页靠心跳拿到新值；不取的话徽章要等下次整页
      // 刷新才更新，点了按钮看着没反应。
      manual_disabled: typeof p.manual_disabled === 'boolean' ? p.manual_disabled : null,
      manual_reason: typeof p.manual_reason === 'string' ? p.manual_reason : '',
      in_flight: typeof p.in_flight === 'number' ? p.in_flight : null,
      cooling: typeof p.cooling === 'boolean' ? p.cooling : null,
      degrade_until: typeof p.degrade_until === 'string' ? p.degrade_until : null,
      consecutive_fails:
        typeof p.consecutive_fails === 'number' ? p.consecutive_fails : null,
      last_used: typeof p.last_used === 'number' ? p.last_used : null,
    } satisfies Account;
  });
}

/**
 * 该账号是否正被**连败降权**（上游 issue #114）。
 *
 * 为什么必须单独判：上游把降权计入 `cooling`（其 `entry.healthy()` 把 until /
 * breakerUntil / degradeUntil 三个截止取或），所以只读 `cooling` 分不出两类原因
 * 完全不同的情况 —— 限流退避（等一会儿就好）与连败降权（说明这个号在**持续失败**，
 * 该去看它到底为什么失败）。用户看到「冷却中」无从判断是该等还是该处理。
 *
 * 判据是「截止时间在未来」而不是「字段存在」：上游落盘/恢复都按惰性过滤，但前端
 * 拿到的可能是几十秒前的快照，字段还在、窗口已过。
 */
export function isDegraded(a: Account): boolean {
  const until = a.degrade_until;
  if (typeof until !== 'string' || !until) return false;
  const at = Date.parse(until);
  return Number.isFinite(at) && at > Date.now();
}

/**
 * 该账号是否正被**模型级**限流（6004）——账号在线、但某个模型暂时用不了。
 *
 * 为什么需要单独判（issue #43 的现场）：腾讯对单个模型的频率限制是**按模型**
 * 生效的，账号本身仍然健康、仍能被选中转发。用户的实际观察是「换个模型就能
 * 继续用」，但面板上那个账号显示「在线」，完全看不出「这个模型被限流了」——
 * 于是只能自己去 docker 里翻 state.json 才知道。
 *
 * 上游为此单独给了一本台账（`rate_limited_models`，其注释写明用途就是让运维
 * 看到「账号 A 的模型 X 还在限额中，预计 Z 时间恢复」），到期即从台账消失。
 *
 * **不能用 `cooling` 判**：那是账号级状态，模型级限流不会置位它。所以这里读台账，
 * 并且与 `isDegraded` 同一口径——以「截止时间是否在未来」为准，而不是「字段是否
 * 存在」：前端拿到的快照可能已经过时，字段还在、窗口早过了。
 */
export function rateLimitedModels(a: Account): {model: string; reason: string}[] {
  const rows = a.rate_limited_models ?? [];
  const now = Date.now();
  return rows
    .filter((m) => {
      const until = m.until;
      if (typeof until !== 'string' || !until) return false;
      const at = Date.parse(until);
      return Number.isFinite(at) && at > now;
    })
    .map((m) => ({model: m.model, reason: String(m.reason ?? '')}));
}

/** 该账号的可用性分档（顺序即优先级，见模块注释）。 */
export function availabilityOf(a: Account): AvailabilityTier {
  // 面板主动停用要**先于**其它判定：这类账号必然不在池里（上游不加载它），
  // 若不先判就会落到 notLoaded，显示成「未加载 / 账号文件可能有问题」——
  // 而它其实是用户自己刚点的「停用」，看着像故障（实测会在界面上造成这种误导）。
  if (a.disabled_by_panel) return 'disabledByPanel';
  // 上游的手动停用位（issue #45）。也先于 disabled/cooling 判定：它是运维的
  // 明确意图，与上面那条同源（都是「我主动摘的」），只是机制不同——这条账号
  // **还在池里**，签到与保活照常，所以文案必须与改名那条区分开。
  if (a.manual_disabled === true) return 'manualDisabled';
  if (a.disabled === true) return 'disabled';
  if (a.is_expired) return 'expired';
  // 「看不到上游」必须早于「不在池里」：否则会把正常账号说成文件损坏
  if (a.poolUnknown) return 'unknown';
  if (a.cooling) return 'cooling';
  // 判据用 success_count 而不是 last_success：后者是 Go 的 time.Time 配
  // omitempty，而 omitempty 对结构体类型**不生效**——从未成功过的账号会序列化成
  // "0001-01-01T00:00:00Z"，在 JS 里是**真值**，判空永远不命中（实测确认）。
  // success_count 是 int64，omitempty 生效，0 时整个键都不出现。
  const errs = typeof a.err_total === 'number' ? a.err_total : 0;
  const oks = typeof a.success_count === 'number' ? a.success_count : 0;
  if (errs > 0 && oks === 0) return 'neverSucceeded';
  if (a.in_pool === false) return 'notLoaded';
  return 'online';
}

/**
 * 分档对应的文案键。**两页共用同一套文案**——这正是为了让两处说法逐字一致；
 * 各写各的字符串，迟早又会出现「一个叫在线、一个叫正常」这种漂移。
 */
export function availabilityLabelKey(tier: AvailabilityTier, a?: Account): string {
  switch (tier) {
    case 'disabled':
      // 上游对 11140（request illegal）是硬禁用、到期也不自愈，必须重新登录；
      // 只说「已禁用」会让人干等。
      return /11140|request illegal/i.test(String(a?.disabled_reason || ''))
        ? 'accounts.badgeDisabledRelogin'
        : 'accounts.badgeDisabled';
    case 'disabledByPanel':
      return 'accounts.badgeDisabledByPanel';
    case 'manualDisabled':
      return 'accounts.badgeManualDisabled';
    case 'expired':
      return 'accounts.badgeExpired';
    case 'unknown':
      return 'accounts.badgeUnknown';
    case 'cooling':
      // 降权与限流退避在上游同属 cooling（口径如此），但含义差很多：前者是
      // 「这个号连续失败已达阈值」，后者是「等一会儿就好」。分开说，用户才知道
      // 该干等还是该去查这个号为什么一直失败。传了账号才分得出来。
      return a && isDegraded(a) ? 'accounts.badgeDegraded' : 'accounts.badgeCooling';
    case 'neverSucceeded':
      return 'accounts.badgeNeverSucceeded';
    case 'notLoaded':
      return 'accounts.badgeNotLoaded';
    case 'online':
      return 'accounts.badgeOnline';
  }
}

/** 需要解释「为什么是这个状态」时对应的提示键；没有可解释的返回 null。 */
export function availabilityTitleKey(tier: AvailabilityTier): string | null {
  switch (tier) {
    case 'unknown':
      return 'accounts.badgeUnknownTitle';
    case 'neverSucceeded':
      return 'accounts.badgeNeverSucceededTitle';
    case 'notLoaded':
      return 'accounts.badgeNotLoadedTitle';
    // 两种停用机制的差别（任务停不停）用户看不出来，必须写清——否则他会以为
    // 点了「停用」签到也停了，或者反过来以为积分还在涨。
    case 'disabledByPanel':
      return 'accounts.badgeDisabledByPanelTitle';
    case 'manualDisabled':
      return 'accounts.badgeManualDisabledTitle';
    default:
      return null;
  }
}

/** 分档对应的文字颜色（给不用 Badge 的地方，如首页快照卡片）。 */
export function availabilityClass(tier: AvailabilityTier): string {
  switch (tier) {
    case 'disabled':
    case 'expired':
      return 'text-red-600 dark:text-red-400';
    // 主动停用是**用户自己的选择**，用中性灰而不是告警红——
    // 它不是故障，标红会让人以为出了问题。
    case 'disabledByPanel':
    case 'manualDisabled':
      return 'text-muted-foreground';
    case 'unknown':
      return 'text-muted-foreground';
    case 'cooling':
      return 'text-amber-600 dark:text-amber-400';
    case 'neverSucceeded':
    case 'notLoaded':
      return 'text-rose-600 dark:text-rose-400';
    case 'online':
      return 'text-emerald-600 dark:text-emerald-400';
  }
}

/* ──────────────────────────────────────────────────────────────
 * 跨分组聚合（仪表盘「全部」视图）
 *
 * 仪表盘原先只取默认分组（`accountApi.list()` 不传 upstreamId，后端
 * `routers/accounts.py::_group(None)` 就是默认分组），于是有多个分组时首页
 * 只报 1/N 个池子。而那个数**看起来完全正常**——数字合理、图表正常，用户
 * 不会怀疑，也永远不会自己变对（上游 issue #94 问题 2）。这比「加载中显示 0」
 * 严重：前者是延迟，后者是错误。
 *
 * 修法是逐组各取一份再合并。合并本身有几处容易写错，所以和 `mergePoolStatus`
 * 放在一起、由 `account-status.test.mjs` 直接测真实实现：
 *
 *  1. **`file` 只在分组内唯一**。两个分组可以有同名账号文件（`sub2api.json`
 *     这类是扫码登录的默认文件名）。合并后若还拿 `a.file` 当 React key，两条
 *     会撞成一个——列表少一条，或者状态串到别的号上，而两种都不会报错。
 *  2. **池状态必须逐组合并**（`tagGroup` 就是干这个的）。`mergePoolStatus` 是把
 *     「**这一份** /status 快照」并到「**这一份**账号列表」上的；若先跨组拼账号、
 *     再拿某一组的 /status 去并，其余组的账号会因为「不在这份池里」被整批判成
 *     `notLoaded`——那正是本模块开头记着的那个 P0 级误报。
 *  3. **没有账号目录的分组不该被取数**。后端对这类分组的账号接口直接 409
 *     （`routers/accounts.py::_require_dir`），列表接口给空数组。它们本来就没有
 *     账号，纳进来只会多出几个必然失败的请求，还会让「共 N 个分组」这个数虚高。
 * ────────────────────────────────────────────────────────────── */

/** 账号 + 它属于哪个分组 */
export interface GroupedAccount extends Account {
  group: {id: number | null; name: string};
}

/** 一个分组的取数结果（成功与失败都在这里；失败时 accounts 为空、error 有值） */
export interface GroupSlice {
  /** 分组 id；null = 默认分组（与后端 `upstreamsvc.DEFAULT_ID` 一致） */
  gid: number | null;
  /** 分组名。多于一个分组时，界面靠它标注「这条账号属于哪个池」 */
  name: string;
  /** 该组的账号：已合并**该组**的池状态、已打上分组标记 */
  accounts: GroupedAccount[];
  /** 该组的池状态；没取到为 null */
  upstream: UpstreamStatus | null;
  /** 该组取数失败的原因；成功为 null */
  error: unknown;
}

/**
 * 该在哪些分组上取账号数：只取**配了本地账号目录**的。
 *
 * 默认分组永远有目录（部署时的 `WB_AUTH_DIR`，见 `upstreamsvc.default_upstream`）；
 * 其余分组没配目录 = 只做密钥转发，一个账号都没有，而且它的账号接口会 409。
 */
export function accountGroups(groups: readonly UpstreamEndpoint[]): UpstreamEndpoint[] {
  return groups.filter((g) => String(g.auth_dir ?? '').trim() !== '');
}

/**
 * 把**一组**的账号与**它自己的**池状态合并，并打上分组标记。
 *
 * 参数刻意是「一组的账号 + 一组的 /status」：跨组混用会把别的组的账号判成
 * 「不在池里」（见上方第 2 条）。
 */
export function tagGroup(
  accounts: readonly Account[],
  upstream: UpstreamStatus | null,
  gid: number | null,
  name: string,
): GroupedAccount[] {
  return mergePoolStatus(accounts as Account[], upstream)
    .map((a) => ({...a, group: {id: gid, name}}));
}

/**
 * React key。**必须带分组**。
 *
 * `file` 只在分组内唯一：两个分组里的 `sub2api.json` 是两个不同的账号。
 * 只用文件名当 key，React 会把它们当成同一条——表现是列表少一条、或者某条的
 * 状态串到另一条上，而两种都不会报错。
 */
export function accountKey(a: GroupedAccount): string {
  return `${a.group.id ?? 'default'}:${a.file}`;
}

/**
 * 交错取各组的下一个账号（第 1 组第 1 条 → 第 2 组第 1 条 → … → 第 1 组第 2 条）。
 *
 * 首页快照只画前 9 条（P1-7 记录了这个硬截断）。若按「一组接一组」平铺，默认
 * 分组的 9 个账号会把格子占满，其余分组的问题一条都看不到——而「覆盖所有分组」
 * 正是这次要修的东西。交错之后每个分组都有代表，截断也不会把某一组整个吞掉。
 */
export function interleave(slices: readonly GroupSlice[]): GroupedAccount[] {
  const out: GroupedAccount[] = [];
  const longest = slices.reduce((m, s) => Math.max(m, s.accounts.length), 0);
  for (let i = 0; i < longest; i += 1) {
    for (const s of slices) {
      const a = s.accounts[i];
      if (a) out.push(a);
    }
  }
  return out;
}

/**
 * 该组里「连败降权」的账号数（只数当前版本，与卡片、快照同口径）。
 *
 * 为什么要单独数（上游 issue #114）：**上游把降权并进 `cooling`**，所以
 * `realm_totals` 里的「冷却中」是个混数——既有等一会儿就好的限流退避，也有
 * 「这个号在持续失败」的降权。面板只显示混数时用户看不出降权的存在（反馈原话：
 * 「降权统计这里根本不统计」）。从账号明细单独数一份，与上游口径不冲突：
 * 它只是把 `cooling` 里降权的那一部分显式化。
 *
 * 参数是**一个分组的切片**而不是全量账号：面板改成「每个分组一块」之后，这一格
 * 必须只数本组，否则每块都会显示全池的降权数（看起来像「四个分组各自都降权了
 * 同样多个号」）。
 */
export function degradedCount(slice: GroupSlice, realm: string): number {
  return slice.accounts.filter((a) => (a.realm ?? 'cn') === realm && isDegraded(a)).length;
}

/** 「反代上游」面板里一个分组那一块的池计数 */
export interface PoolTotals {
  total: number;
  healthy: number;
  cooling: number;
  disabled: number;
  /** 拿到了可用的计数（无论来自 realm_totals 还是顶层汇总） */
  known: boolean;
  /** 只能拿到顶层汇总（含两个版本），界面需标注 */
  globalOnly: boolean;
}

/**
 * 一个分组的池计数。
 *
 * 优先用上游按版本分好的 `realm_totals[realm]`——口径与上游状态机完全一致，
 * 也不用我们去猜 `healthy` 该怎么算（曾经从账号明细里自己数，而明细里根本
 * 没有这个字段，布尔转换恒为 false，面板因此全显示 0）。
 *
 * 老版本上游没给 realm_totals 时退回顶层汇总，并标 `globalOnly`：那时它是
 * 两个版本的**合计**，界面必须说明，否则切到国际版会看到两个版本加起来的数。
 *
 * ⚠️ 这里是**一个分组**的口径，刻意不做跨组求和：两个分组可以指向同一套上游
 * 实例（新建分组时地址默认沿用默认分组的），求和会把同一套实例数两遍。
 */
export function poolTotals(upstream: UpstreamStatus | null, realm: string): PoolTotals {
  const perRealm = upstream?.realm_totals?.[realm];
  if (perRealm) {
    return {
      total: perRealm.total,
      healthy: perRealm.healthy,
      cooling: perRealm.cooling,
      disabled: perRealm.disabled,
      known: true,
      globalOnly: false,
    };
  }
  const hasTop = typeof upstream?.total === 'number';
  return {
    total: upstream?.total ?? 0,
    healthy: upstream?.healthy ?? 0,
    cooling: upstream?.cooling ?? 0,
    disabled: upstream?.disabled ?? 0,
    known: hasTop,
    globalOnly: hasTop,
  };
}


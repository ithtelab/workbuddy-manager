/**
 * 账号列表的**检索 / 筛选 / 排序 / 分页**（纯函数）。
 *
 * 为什么单独一个模块：这几步全在**客户端**做（一次请求就把整个分组的账号都拿回来了，
 * `accountApi.list` 不分页），所以它们不需要动后端、也不该散在页面里——散着写的话
 * 「筛选之后页码没复位」「排序把没有积分的账号当成 0 分排到最前面」这类错**界面不报错**，
 * 只是列表看起来不对，没人会怀疑是排序写错了。抽成纯函数就能直接测。
 *
 * ⚠️ 本模块**不得引入任何运行时 import**（只有 `import type`，编译后被抹掉）。
 * 这是仓库给「可在 Node 里直接测的前端逻辑」定的规矩，详见 lib/account-status.ts
 * 开头的说明：`web/lib/*.test.mjs` 用 Node 的 type stripping 直接 import `.ts`，
 * 一旦这里 import 了别的模块的**值**，测试就会以「找不到模块」失败。
 * 所以「账号 → 4 组」的映射由调用方以 `groupOf` 传进来，而不是在这里 import
 * `availabilityGroup` —— 那份映射只此一份，仍在 lib/account-status.ts 里。
 */
import type {Account} from './types';
import type {AvailabilityGroup} from './account-status';

/** 排序键 */
export type SortKey =
  /** 后端返回的顺序（分组内的自然顺序），不动 */
  | 'default'
  /** 按剩余有效期排序 */
  | 'remain'
  /** 按积分排序；没有数据的始终排最后 */
  | 'credits'
  /** 按本面板记录的添加时间排序；未知历史时间的始终排最后 */
  | 'added'
  /** 按管理员拖拽保存的自定义顺序；顺序里没有的账号保持原顺序排在后面 */
  | 'manual';

export type SortDirection = 'asc' | 'desc';

/** 状态筛选值；`'all'` = 不按状态筛 */
export type GroupFilter = AvailabilityGroup | 'all';

/** 一次查询的三个条件 */
export interface ListQuery {
  /** 关键词（昵称 / uid / 文件名 / 备注） */
  q: string;
  /** 状态分组 */
  group: GroupFilter;
  /** 排序 */
  sort: SortKey;
  direction?: SortDirection;
}

/**
 * 关键词是否命中这个账号。
 *
 * 检索面刻意只覆盖**用户能对上号**的四样：昵称、uid、账号文件名、备注。
 * 不搜 token、不搜 enterprise_id —— 那些在界面上不显示，搜出来用户也不知道
 * 命中了什么。
 *
 * 空关键词命中一切（不是「什么都不命中」）：调用方因此不需要在外面判空。
 */
export function matchesQuery(a: Account, q: string): boolean {
  const kw = q.trim().toLowerCase();
  if (!kw) return true;
  return (
    (a.nickname || '').toLowerCase().includes(kw) ||
    a.uid.toLowerCase().includes(kw) ||
    a.file.toLowerCase().includes(kw) ||
    (a.note || '').toLowerCase().includes(kw)
  );
}

/** `selectAccounts` 需要的两处外部数据（都从调用方注入，见模块注释） */
export interface SelectDeps {
  /** 账号 → 展示分组（4 组语义；映射表在 lib/account-status.ts） */
  groupOf: (a: Account) => AvailabilityGroup;
  /** 账号 → 当前积分（含刚查到的实时值）；**没有数据返回 null**，不要用 0 冒充 */
  creditOf: (a: Account) => number | null;
  /** 账号 → 自定义顺序里的名次；未登记返回 null（只有 sort='manual' 用得上） */
  orderRank?: (a: Account) => number | null;
}

/**
 * 筛选 + 排序。**不**分页（分页交给 `paginate`，两者的测试才好分开写）。
 *
 * 排序的稳定性依赖 `Array.prototype.sort` 的稳定实现（ES2019 起规范要求）：
 * 键值相同的账号保持后端给的顺序，所以同一批数据每次渲染的顺序都一样，
 * 不会「刷新一下顺序就变了」。
 */
export function selectAccounts(
  accounts: readonly Account[],
  query: ListQuery,
  deps: SelectDeps,
): Account[] {
  const rows = accounts.filter(
    (a) =>
      (query.group === 'all' || deps.groupOf(a) === query.group) && matchesQuery(a, query.q),
  );
  if (query.sort === 'default') return rows;
  if (query.sort === 'manual') {
    const rankOf = deps.orderRank;
    // 没有顺序来源时退化成「后端顺序」而不是把所有账号当成未知、随机翻转：
    // 排序选项可以手动选中，但数据还没到，此时保持不动最不意外。
    if (!rankOf) return rows;
    return [...rows].sort((a, b) => {
      const ra = rankOf(a);
      const rb = rankOf(b);
      if (ra === null && rb === null) return 0;
      if (ra === null) return 1;
      if (rb === null) return -1;
      return ra - rb;
    });
  }
  const direction = (query.direction ?? (query.sort === 'added' ? 'desc' : 'asc')) === 'desc' ? -1 : 1;
  const valueOf = (a: Account) => query.sort === 'remain' ? (a.expires_at === 0 ? null : a.remain_seconds)
    : query.sort === 'added' ? a.added_at : deps.creditOf(a);
  // 未知值不参与升降序，始终排最后；同值保持原顺序。
  return [...rows].sort((a, b) => {
    const va = valueOf(a);
    const vb = valueOf(b);
    const knownA = typeof va === 'number' && Number.isFinite(va);
    const knownB = typeof vb === 'number' && Number.isFinite(vb);
    if (!knownA && !knownB) return 0;
    if (!knownA) return 1;
    if (!knownB) return -1;
    return direction * (va - vb);
  });
}

/** 各状态组（外加 `all`）的条数，给筛选按钮上的数字用 */
export function groupCounts(
  accounts: readonly Account[],
  groupOf: (a: Account) => AvailabilityGroup,
): Record<GroupFilter, number> {
  const out: Record<GroupFilter, number> = {
    all: accounts.length,
    usable: 0,
    attention: 0,
    cooling: 0,
    stopped: 0,
  };
  for (const a of accounts) out[groupOf(a)] += 1;
  return out;
}

/** 一页的结果 */
export interface Page<T> {
  rows: T[];
  total: number;
  pages: number;
  /** **夹回范围内**之后的页码，界面必须用这个而不是 state 里的值 */
  page: number;
}

/**
 * 切页。页码越界时**夹回**最后一页，而不是给一张空表。
 *
 * 越界不是异常情况：在最后一页删掉一个账号、或改筛选条件让条数变少，页码就会
 * 超出去。那时若照旧切页，用户看到的是空表 + 「第 3 / 2 页」这种自相矛盾的页码，
 * 看起来像数据丢了。
 */
export function paginate<T>(rows: readonly T[], page: number, size: number): Page<T> {
  const total = rows.length;
  const pages = Math.max(1, Math.ceil(total / size));
  const safe = Math.min(Math.max(1, page), pages);
  return {
    rows: rows.slice((safe - 1) * size, safe * size),
    total,
    pages,
    page: safe,
  };
}

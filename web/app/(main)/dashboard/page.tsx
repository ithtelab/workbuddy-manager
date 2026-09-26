'use client';

import {useEffect, useMemo, useState} from 'react';
import type {ReactNode} from 'react';
import {Users, CircleCheck, TriangleAlert, Activity, Server, Coins} from 'lucide-react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useAsyncAll} from '@/lib/use-async-data';
import {getExpiryDailyGroup, groupExpiriesByDay} from '@/lib/display-prefs';
import {accountApi, statsApi, upstreamApi, upstreamsApi} from '@/lib/api';
import {useRealm} from '@/lib/realm-context';
import type {
  CreditExpiry,
  CreditsMeta,
  StatsSummary,
  UsagePoint,
} from '@/lib/types';
import {
  expiryBarPercent,
  expiryVisual,
  fmtCompact,
  fmtDateTime,
  fmtNumber,
  fmtRemain,
} from '@/lib/format';
import {
  accountGroups,
  accountKey,
  availabilityLabelKey,
  availabilityTitleKey,
  availabilityClass,
  availabilityOf,
  degradedCount,
  interleave,
  poolTotals,
  tagGroup,
  type AvailabilityTier,
  type GroupedAccount,
  type GroupSlice,
} from '@/lib/account-status';
import {cn} from '@/lib/utils';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {StatCard, type StatTone} from '@/components/common/layout/StatCard';
import {CreditCountdown} from '@/components/common/accounts/CreditCountdown';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Badge} from '@/components/ui/badge';
import {useT} from '@/lib/i18n/provider';

/**
 * 取数完成前的空值。
 *
 * 必须是模块级的同一份：写成 `values.daily ?? []` 的话，每次渲染都会新建一个
 * 数组，而它们要进下游 useMemo 的依赖——依赖每帧都变，那几个 memo 就等于没写。
 * 共享只读常量才稳定。
 */
const EMPTY_POINTS: UsagePoint[] = [];
const EMPTY_CREDITS: Record<string, CreditsMeta> = {};
const EMPTY_LIVE_CREDITS: Record<string, number> = {};
const EMPTY_SLICES: GroupSlice[] = [];

/** `refreshCredits` 的返回体形状（扇出后要逐项相加，类型从 api 上取） */
type CreditsResponse = Awaited<ReturnType<typeof accountApi.refreshCredits>>;

/**
 * 取回**全部分组**的账号与池状态。
 *
 * 为什么是「全部」：`accountApi.list()` / `upstreamApi.status()` 不传分组时，
 * 后端 `routers/accounts.py::_group(None)` 给的就是**默认分组**。首页原先正是
 * 这么调的，于是有多个分组时它只报 1/N 个池子——而那个数看起来完全正常，
 * 用户不会怀疑，也永远不会自己变对（上游 issue #94 问题 2）。
 * 「加载中显示 0」是延迟，「加载完了显示错的数」是错误。
 *
 * 三条容易写错的地方：
 *
 *  1. **分组清单必须在这一份里一起取**，不能放在另一个 hook 里再从这里读。
 *     两个 hook 之间没有先后关系：本函数先跑完时清单还是空的，而它**不会**
 *     因为清单后到而重跑——那就静默退化成「只报默认分组」，正是要修的毛病。
 *  2. **逐组独立成败**。某个分组的上游没单独部署（issue #94 的现场就是这样）
 *     不该让整页变成错误态——那样用户连另外几组都看不到，反而更糟。失败的那几组
 *     连同原因一起带回界面，由界面如实说明「这几个分组没算进来」。
 *  3. **全部失败才抛**。一个分组都没取到 = 这份数据不存在，必须让 useAsyncAll
 *     记一笔失败；若把失败吞成空数组，「取不到」会被渲染成「暂无账号」——
 *     同一类坑在统计页已经踩过一次（见 `test_stats_keeps_upstream_out_of_the_main_group`）。
 */
async function loadPoolSnapshot(): Promise<GroupSlice[]> {
  const {items} = await upstreamsApi.list();
  const targets = accountGroups(items ?? []);
  const settled = await Promise.allSettled(
    targets.map(async (g): Promise<GroupSlice> => {
      const gid = g.id ?? null;
      // 账号与池状态一起取：两者必须来自**同一个分组**，否则会把别的组的账号
      // 标成在线 / 未加载（见 account-status 的模块注释）。
      const [acc, up] = await Promise.allSettled([
        accountApi.list(gid),
        upstreamApi.status(gid),
      ]);
      // 池状态取不到只让这一组的**状态**变成「未知」，账号数仍然算数——
      // 所以只有账号取不到才算「这一组没取到」。
      const upstream = up.status === 'fulfilled' ? up.value : null;
      if (acc.status === 'rejected') {
        // 账号没取到 → 这一组不参与统计。但池状态**若取到了就留着**：面板照常
        // 显示这一组的连接状态与池计数，比笼统的「未获取到上游状态」有用得多
        // ——上游其实是好的，坏的是账号列表这一路。丢掉它会让用户跑去查上游
        // 连接，方向是错的。（实测确认过：丢了之后面板会把「上游正常、账号接口
        // 失败」说成「未获取到上游状态」。）
        return {gid, name: g.name, accounts: [], upstream, error: acc.reason};
      }
      return {
        gid,
        name: g.name,
        accounts: tagGroup(acc.value.accounts ?? [], upstream, gid, g.name),
        upstream,
        error: null,
      };
    }),
  );
  const slices: GroupSlice[] = settled.map((r, i) => (
    r.status === 'fulfilled' ? r.value : {
      gid: targets[i].id ?? null,
      name: targets[i].name,
      accounts: [],
      upstream: null,
      error: r.reason,
    }
  ));
  if (slices.length && slices.every((s) => s.error)) throw slices[0].error;
  return slices;
}

/**
 * 取回**全部分组**的实时积分。
 *
 * 与 `loadPoolSnapshot` 同因按分组扇出：`refresh-credits` 是**分组接口**，
 * 后端按该分组的账号目录逐个查腾讯，只调一次就只能刷到默认分组。
 *
 * 积分按 **uid** 归并——uid 是腾讯侧的账号标识，跨分组唯一，不像文件名那样会撞。
 *
 * 分组清单在这里再取一次（与上面各取一次）。多一次请求是有意的：两个取数函数
 * 的失败域必须独立（useAsyncAll 逐项独立成败），共用一份清单会让「清单取不到」
 * 同时打掉账号与积分两块，而它们本来可以各自承担。代价是一次本地读库。
 */
async function loadGroupCredits(): Promise<CreditsResponse> {
  const {items} = await upstreamsApi.list();
  const targets = accountGroups(items ?? []);
  const settled = await Promise.allSettled(
    targets.map((g) => accountApi.refreshCredits(false, g.id ?? null)),
  );
  const ok = settled
    .filter((r): r is PromiseFulfilledResult<CreditsResponse> => r.status === 'fulfilled')
    .map((r) => r.value);
  // 一组都没刷到 → 抛。否则「刷不到」会被当成「这些账号没有积分」。
  if (targets.length && !ok.length) throw (settled[0] as PromiseRejectedResult).reason;
  return {
    total: ok.reduce((sum, r) => sum + (r.total ?? 0), 0),
    succeeded: ok.reduce((sum, r) => sum + (r.succeeded ?? 0), 0),
    credits: Object.assign({}, ...ok.map((r) => r.credits)),
    meta: Object.assign({}, ...ok.map((r) => r.meta)),
    failed: ok.flatMap((r) => r.failed ?? []),
  };
}

export default function DashboardPage() {
  const {realm, label: realmName} = useRealm();
  const t = useT();

  /**
   * 本页要的 4 份数据一次并发取回，成败逐项独立。
   *
   * 依赖是 realm：切换版本后**必须重取**——两个版本的账号池不同，credits 也
   * 不能混。useAsyncAll 在依赖变化时会连同旧数据一起清掉，否则会出现
   * 「标题已经写着国际版、卡片上还是国内版的数字」。
   *
   * 账号与池状态合成一份（`pool`）而不是分成两个字段：它们**逐组配对**才有
   * 意义（拿 A 组的账号配 B 组的池状态会把整组标错），合成一份就不可能配错。
   *
   * 加载态 / 失败态 / 是否正在刷新也由它给出，本页不再自己维护这几个 useState。
   */
  const {values, errors, isInitialLoading, isInitialFailed, isRefreshing, reload} = useAsyncAll({
    pool: () => loadPoolSnapshot(),
    summary: () => statsApi.summary(realm),
    daily: () => statsApi.daily(14, realm),
    credits: () => loadGroupCredits(),
  }, [realm]);

  const slices: GroupSlice[] = values.pool ?? EMPTY_SLICES;
  const summary: StatsSummary | null = values.summary ?? null;
  const daily: UsagePoint[] = values.daily ?? EMPTY_POINTS;

  /**
   * 账号那一块没取到。
   *
   * 此时**不能**渲染 `scoped.length` 推出来的数字：它会是 0，而 0 是在**断言**
   * 「一个账号都没有」——事实是没取到。同理不能渲染「暂无账号」。两者都是本页
   * 一直在修的那句谎话，只是换了个触发路径（首页四份数据里只有这一份挂掉时，
   * 整页错误态不会出现，因为其余三份有值）。
   */
  const poolFailed = 'pool' in errors;
  /** 有分组没取到：下面的账号与积分**不含**它们，必须说明，否则又是「报 1/N 个池子」 */
  const failedGroups = slices.filter((s) => s.error);
  /** 多于一个分组时才需要标注统计范围与归属——单分组时界面保持原样 */
  const multiGroup = slices.length > 1;

  /** 实时积分（按 uid），叠加到 accounts 上；上游 /status 的 credits 可能滞后数小时 */
  const liveCredits = useMemo(() => {
    const raw = values.credits?.credits;
    if (!raw) return EMPTY_LIVE_CREDITS;
    return Object.fromEntries(
      Object.entries(raw).filter(([, v]) => typeof v === 'number') as [string, number][],
    );
  }, [values.credits]);
  /** 各账号的积分套餐到期时间（按 uid），与实时积分同一次查询返回 */
  const creditsMeta: Record<string, CreditsMeta> = values.credits?.meta ?? EMPTY_CREDITS;

  // 账号健康度与用量会持续变化，用心跳刷新避免展示陈旧数据。
  // 走 reload（刷新模式）：只把新数据换上去，**不**重走首屏流程——
  // 否则骨架会每 30 秒闪一次，用户以为页面在抽风。
  useHeartbeat(reload, 30000);

  /** 有字段没刷新成功。此时至少还有一份数据在，用顶部提示如实说明，不清空内容 */
  const partialFailed = Object.keys(errors).length > 0;

  /**
   * 各分组的账号，按当前版本过滤。
   *
   * 只保留**一份**来源（`scopedSlices`）：计数与快照都从它推出来，不存在
   * 「卡片说的和快照说的不一样」这种可能。
   *
   * 版本过滤：账号池里两种版本的账号都有，不过滤的话切到国际版仍会看到国内版的
   * 账号数、积分与健康快照。realm 为空的存量账号视为国内版，与后端判定一致。
   */
  const scopedSlices = useMemo(
    () => slices.map((s) => ({
      ...s,
      accounts: s.accounts.filter((a) => (a.realm ?? 'cn') === realm),
    })),
    [slices, realm],
  );

  /** 当前版本的全部账号（含分组标记）。所有计数都走这一份 */
  const scoped: GroupedAccount[] = useMemo(
    () => scopedSlices.flatMap((s) => s.accounts),
    [scopedSlices],
  );

  /**
   * 快照格子。按分组**交错**取（见 `interleave` 的注释）：快照只画前 9 条，
   * 按分组顺序平铺的话默认分组的账号会把格子占满，其余分组的问题一条都看不到
   * ——而「覆盖所有分组」正是这次要修的东西。
   */
  const snapshot = useMemo(() => interleave(scopedSlices).slice(0, 9), [scopedSlices]);

  /** 可用性分档的汇总（只统计「能正常调用」的，与账号页说法一致） */
  const availability = useMemo(() => {
    const counts: Record<AvailabilityTier, number> = {
      disabled: 0, disabledByPanel: 0, manualDisabled: 0, expired: 0, unknown: 0, cooling: 0,
      neverSucceeded: 0, notLoaded: 0, online: 0,
    };
    for (const a of scoped) counts[availabilityOf(a)] += 1;
    return counts;
  }, [scoped]);

  /**
   * 「不可用」的账号数：未加载 / 已禁用 / 一直失败 / 冷却中——这些是**用户需要
   * 处理**的（去上游重载、重新登录、换模型等），而不是单纯「令牌快过期」。
   * 与快照卡片的分档同源，所以卡片提示与快照不会互相打架。
   *
   * 两类**刻意不计入**：
   *   · unknown（读不到上游）——那是我们看不到，不是账号有问题；
   *   · disabledByPanel / manualDisabled（主动停用）——那是用户自己的决定，
   *     不需要「处理」，计进去会让卡片一直提示「N 个不可用」而用户其实已经
   *     处理完了（两种机制同属这一类，只是实现不同）。
   */
  const unusable = availability.notLoaded + availability.disabled
    + availability.neverSucceeded + availability.cooling;

  const valid = scoped.filter((a) => !a.is_expired).length;
  const expiring = scoped.filter((a) => a.remain_seconds > 0 && a.remain_seconds < 3600).length;
  // 积分余额合计（仅统计已同步到的账号）
  // 优先用实时查询到的积分，其次上游 /status 的（可能滞后的）值
  const credOf = (a: GroupedAccount) => liveCredits[a.uid] ?? a.credits;
  const creditsKnown = scoped.filter((a) => typeof credOf(a) === 'number');
  const totalCredits = creditsKnown.reduce((sum, a) => sum + (credOf(a) || 0), 0);
  const creditsLow = creditsKnown.filter((a) => (credOf(a) || 0) < 200).length;
  /**
   * 最近一笔积分到期（全体账号里最早的那个到期时刻）。
   *
   * 为什么值得占首页一行：积分过期即作废且不可恢复，用户能采取的行动
   * （多跑点任务把它用掉）只在到期**之前**有效。汇总卡片此前只报总量，
   * 看不出「总量虽多但大部分下周就没了」这种情况。
   */
  const nextExpiry = useMemo(() => {
    let best: CreditExpiry | null = null;
    for (const m of Object.values(creditsMeta)) {
      const e = m.expiries?.[0];
      if (e && (!best || e.at < best.at)) best = e;
    }
    return best;
  }, [creditsMeta]);
  // 7 天内的到期算紧急（与倒计时胶囊的琥珀/红分档同一条线）。取渲染时刻即可：
  // 阈值是 7 天，而本页每 30 秒重渲染一次，几秒的偏差不影响分档。
  const expiryUrgent = !!nextExpiry && nextExpiry.at - Date.now() / 1000 < 7 * 86400;

  /** 「到期积分按天模糊统计」开关（界面偏好，存在浏览器本地）。
   *  首屏后读：直接读会让服务端渲染与客户端不一致（与 realm-context 同理）。 */
  const [dailyGroup, setDailyGroup] = useState(false);
  useEffect(() => {
    setDailyGroup(getExpiryDailyGroup());
  }, []);

  /**
   * 全体账号的到期明细（按时刻升序）。
   *
   * 与上面 nextExpiry 的区别：那个只看每个账号的**第一笔**，回答「最近一笔什么时候
   * 没了」；这里收全量，供「按天归并」用——同一天到期的多笔要能相加，
   * 只看第一笔永远算不出那一天的总和。
   */
  const allExpiries = useMemo(() => {
    const out: CreditExpiry[] = [];
    for (const m of Object.values(creditsMeta)) {
      for (const e of m.expiries ?? []) out.push(e);
    }
    return out.sort((a, b) => a.at - b.at);
  }, [creditsMeta]);

  /**
   * 卡片上实际要显示的到期条目。
   *
   * 关闭（默认）= 最近一笔；开启 = 按本地日历日归并后的第一条（最近那一天，
   * 金额是那天到期的**总和**）。两者数量级不同（一笔 vs 一天），所以开关默认
   * 关闭——升级后看到的还是原来那个数，想按天看的自己打开。
   */
  const expiryShown = useMemo(
    () => (dailyGroup
      ? groupExpiriesByDay(allExpiries)
      : (nextExpiry ? [nextExpiry] : [])),
    [dailyGroup, allExpiries, nextExpiry],
  );

  const chartData = daily.map((d) => ({
    day: d.day.slice(5),
    requests: d.requests,
    tokens: d.prompt_tokens + d.completion_tokens,
    // 失败数与请求数是**两条独立的数据来源**（前者取自请求日志，后者取自用量
    // 汇总），所以曲线不会互相覆盖：某天「requests=0 但 failed=31」是完全
    // 可能的形态（全池中断），图上看得出「那天出过事」而不是「没有请求」。
    failed: d.failed ?? 0,
  }));

  /** 今日失败总数（4xx + 5xx）。用于今天那张卡片的提示与色调。 */
  const todayFailed =
    (summary?.failures?.today_4xx ?? 0) + (summary?.failures?.today_5xx ?? 0);
  const weekFailed =
    (summary?.failures?.week_4xx ?? 0) + (summary?.failures?.week_5xx ?? 0);

  /**
   * 账号类卡片的取值。
   *
   * 账号那一块没取到时统一给「—」、中性色、**不带提示**：0 是在断言「一个账号
   * 都没有」，而「全部正常」「N 个不可用」这类提示同样是凭一份不存在的名单下的
   * 结论。发生了什么由顶部那条说明负责，四张卡片各说一遍只是噪音。
   */
  const acctCard = (
    value: ReactNode, hint: string | undefined, tone: StatTone, hintTone?: StatTone,
  ) => (poolFailed
    ? {value: '—' as ReactNode, hint: undefined, tone: 'neutral' as StatTone, hintTone: undefined}
    : {value, hint, tone, hintTone});

  /**
   * 页头。两条渲染路径（首屏 / 已就绪）都要用，抽出来免得改一处漏一处。
   * 本页 30 秒自动刷新，且没有任何会改变数据的操作，因此不放手动刷新按钮
   * （移动端还省下一行）。
   */
  const header = (
    <PageHeader
      title={t('dashboard.title')}
      description={t('dashboard.description', {realm: realmName})}
    />
  );

  // 首屏：还没拿到任何数据。此时**不能**渲染下面的卡片——它们会把「还没取到」
  // 显示成「暂无账号」「暂无调用数据」，那是在撒谎。一次都没取到（失败）同理：
  // 页面上没有任何可信数字，直接给错误态与「重试」，而不是一直转圈。
  if (isInitialFailed || isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? <LoadError variant="page" onRetry={reload} /> : <DashboardSkeleton />}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* 统计范围。**只在多于一个分组时才出现**：单分组时它只是句废话，
          而绝大多数部署就是单分组。有了它，用户才知道上面那些数是全池的，
          而不是又一个「看起来正常」的默认分组数。 */}
      {multiGroup && (
        <p className="px-1 text-[11px] leading-4 text-muted-foreground">
          {t('dashboard.scopeAllGroups', {n: slices.length})}
        </p>
      )}

      {/* 有东西没刷新成功：常驻提示，**不**顶掉已经显示出来的内容——那些数据
          仍然是对的，只是可能不是最新的。原先这里只弹一个 toast，几秒后自己
          消失，用户切回来看到的是一片「正常」的数字。

          三种失败共用一个位置、按「具体程度」从高到低取文案：整块账号没取到
          （卡片会是「—」）→ 有分组没取到（数字不含它们）→ 其余字段没刷新上。 */}
      {(poolFailed || failedGroups.length > 0 || partialFailed) && (
        <LoadError
          message={
            poolFailed
              ? t('dashboard.accountsLoadFailed')
              : failedGroups.length > 0
                ? t('dashboard.groupsPartial', {n: failedGroups.length})
                : t('state.partialFailed')
          }
          onRetry={reload}
        />
      )}

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-5 md:gap-4">
        <StatCard
          label={t('dashboard.totalAccounts')}
          {...acctCard(
            fmtNumber(scoped.length),
            t('dashboard.totalAccountsHint', {realm: realmName}),
            'neutral',
          )}
          icon={Users}
          delay={0}
        />
        <StatCard
          label={t('dashboard.valid')}
          {...acctCard(
            fmtNumber(valid),
            // 提示优先级：先说**不可用**（能真用的少了一个，需要处理），
            // 再说令牌快过期，最后才是「全部正常」。
            //
            // 此前只看 `valid === scoped.length`（令牌都在有效期内）就说
            // 「全部正常」——于是出现「卡片说全部正常、快照里却有账号未加载」的
            // 矛盾。令牌有效不等于账号可用：它可能没进上游池、被禁用或一直在
            // 失败。口径与快照卡片、账号页完全一致（同一套 availabilityOf）。
            unusable > 0
              ? t('dashboard.unusable', {count: unusable, n: unusable})
              : valid === scoped.length
                ? t('dashboard.allOk')
                : t('dashboard.abnormal', {count: scoped.length - valid, n: scoped.length - valid}),
            unusable > 0 ? 'warning' : 'success',
            unusable > 0 ? 'warning' : (valid === scoped.length ? 'success' : 'warning'),
          )}
          icon={CircleCheck}
          delay={0.05}
        />
        <StatCard
          label={t('expiry.urgent')}
          {...acctCard(
            fmtNumber(expiring),
            expiring > 0 ? t('dashboard.needRefresh') : t('dashboard.noRisk'),
            expiring > 0 ? 'warning' : 'success',
            expiring > 0 ? 'warning' : 'neutral',
          )}
          icon={TriangleAlert}
          delay={0.1}
        />
        <StatCard
          label={t('metric.credits')}
          {...acctCard(
            // 值里带上最近一笔到期：额度高但下周作废，比额度低更值得注意
            <span className="inline-flex items-baseline gap-1.5">
              <span>{creditsKnown.length ? fmtNumber(totalCredits) : '—'}</span>
              <CreditCountdown expiries={expiryShown} />
            </span>,
            // 优先级：先报「余额快见底」（这是原有的告警，不能被新信息顶掉），
            // 其次报最近一笔到期（具体日期与金额，只有这里能看到），
            // 最后才是「N 个账号余额已知」这种平淡的状态说明。
            !creditsKnown.length
              ? t('dashboard.waitingUpstream')
              : creditsLow > 0
                ? t('dashboard.creditsLow', {count: creditsLow, n: creditsLow})
                : expiryShown.length
                  ? t(dailyGroup ? 'dashboard.creditsExpiryDaily' : 'dashboard.creditsExpiry', {
                      time: fmtDateTime(expiryShown[0].at),
                      amount: fmtNumber(expiryShown[0].amount),
                    })
                  : t('dashboard.creditsCovered', {count: creditsKnown.length, n: creditsKnown.length}),
            !creditsKnown.length ? 'neutral' : creditsLow > 0 || expiryUrgent ? 'warning' : 'accent',
            creditsLow > 0 || expiryUrgent ? 'warning' : undefined,
          )}
          icon={Coins}
          delay={0.15}
        />
        <StatCard
          label={t('dashboard.todayTokens')}
          value={fmtCompact(summary?.today_tokens)}
          hint={
            // 有失败时**优先说失败**：这个数字是用户排查问题的入口，而
            // 「今天 N 次请求」是背景信息。没失败才显示请求数。
            todayFailed > 0
              ? t('dashboard.todayFailed', {n: fmtNumber(todayFailed)})
              : t('dashboard.todayRequests', {
                  n: fmtNumber(summary?.today_requests),
                  realm: realmName,
                })
          }
          icon={Activity}
          tone={todayFailed > 0 ? 'warning' : 'info'}
          hintTone={todayFailed > 0 ? 'warning' : undefined}
          delay={0.2}
        />
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-[20px] bg-muted p-4 lg:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <div className="text-sm font-medium">{t('dashboard.trend14')}</div>
            {/* 按当前版本过滤（与统计页同一口径），不再混两个版本。
                近 7 天有失败时在这里点出总数——用户看曲线只能看出"某天有虚线"，
                具体多少次要悬停；给个总数省这一步。

                ⚠️ 用量**不按分组**：上游那份 `usage_daily` 表里没有分组列，
                所以这几张卡片本来就是「全部分组」的口径。与上面的账号数正好
                一致（那里也是全部聚合），因此不需要额外标注口径差异。 */}
            <div className="text-[11px] text-muted-foreground">
              {weekFailed > 0
                ? t('dashboard.weekFailed', {n: fmtNumber(weekFailed), realm: realmName})
                : t('dashboard.requestsByRealm', {realm: realmName})}
            </div>
          </div>
          <div className="h-[220px] w-full">
            {chartData.length ? (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{top: 4, right: 8, bottom: 0, left: -16}}>
                  <defs>
                    <linearGradient id="gReq" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.35} />
                      <stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                  <XAxis dataKey="day" tickLine={false} axisLine={false} fontSize={11} stroke="var(--muted-foreground)" />
                  <YAxis tickLine={false} axisLine={false} fontSize={11} stroke="var(--muted-foreground)" />
                  <Tooltip
                    contentStyle={{
                      background: 'var(--popover)',
                      border: '1px solid var(--border)',
                      borderRadius: 12,
                      fontSize: 12,
                    }}
                  />
                  <Area
                    type="monotone"
                    dataKey="requests"
                    name={t('metric.requests')}
                    stroke="var(--chart-1)"
                    fill="url(#gReq)"
                    strokeWidth={2}
                  />
                  {/* 失败曲线单独画（虚线、不发散填充）：它来自另一份数据源，
                      与请求数不是「同一条曲线的两个部分」，不该堆叠。
                      没有失败时这条线贴着 0，不干扰读数。 */}
                  <Area
                    type="monotone"
                    dataKey="failed"
                    name={t('dashboard.failedRequests')}
                    stroke="var(--destructive)"
                    fill="none"
                    strokeWidth={1.5}
                    strokeDasharray="4 3"
                  />
                </AreaChart>
              </ResponsiveContainer>
            ) : 'daily' in errors ? null : (
              // 用量取不到时**不说**「暂无调用数据」：那是在断言「你没有调用过」，
              // 而事实是这份数据没拿到（`daily` 单独失败时整页错误态不会出现）。
              <div className="grid h-full place-items-center text-xs text-muted-foreground">{t('dashboard.noCallData')}</div>
            )}
          </div>
        </div>

        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Server className="h-4 w-4" />
            {t('dashboard.upstreamPanel')}
          </div>
          {slices.length ? (
            // **一个分组一块**，而不是把各组的数加起来：两个分组可以指向同一套
            // 上游实例（新建分组时地址默认沿用默认分组的），求和会把同一套实例
            // 数两遍——那比原来的「只报默认分组」更糟，因为它是错的而不是缺的。
            // 每组一块、各显示各自的数，两块一样就说明它们指着同一个地方。
            <div className="space-y-3">
              {slices.map((s, i) => (
                <div
                  key={s.gid ?? 'default'}
                  className={cn('space-y-3', i > 0 && multiGroup && 'border-t border-border pt-3')}
                >
                  <UpstreamBlock slice={s} realm={realm} showName={multiGroup} />
                </div>
              ))}
            </div>
          ) : (
            <div className="grid h-[160px] place-items-center text-xs text-muted-foreground">{t('dashboard.noUpstreamStatus')}</div>
          )}
        </div>
      </section>

      <section className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="text-sm font-medium">{t('dashboard.healthSnapshot')}</span>
          {/* 各档汇总：一眼看清「有几个能真用」。文案与账号页共用同一套键，
              两页不会出现「一个叫在线、一个叫正常」这种漂移。 */}
          {(['online', 'notLoaded', 'cooling', 'neverSucceeded', 'disabled', 'expired', 'unknown'] as AvailabilityTier[])
            .filter((tier) => availability[tier] > 0)
            .map((tier) => (
              <span
                key={tier}
                className={`text-[11px] tabular-nums ${availabilityClass(tier)}`}
                title={availabilityTitleKey(tier) ? t(availabilityTitleKey(tier)!) : undefined}
              >
                {t(availabilityLabelKey(tier, scoped.find((a) => availabilityOf(a) === tier)))}
                {' '}
                {availability[tier]}
              </span>
            ))}
        </div>
        {snapshot.length ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {snapshot.map((a) => {
              const pct = expiryBarPercent(a.remain_seconds, a.ttl_seconds);
              const vis = expiryVisual(a.remain_seconds);
              // 状态标签用**可用性**分档，而不是 Token 有效期分档：
              // 后者只有「有效期内」这一个概念，会把不在池里/被禁用的账号
              // 也写成「在线」（与账号页冲突）。有效期信息仍由下面的进度条
              // 与剩余天数如实呈现，两者不再混在一起说。
              const tier = availabilityOf(a);
              const statusLabel = t(availabilityLabelKey(tier, a));
              const titleKey = availabilityTitleKey(tier);
              return (
                // key **必须带分组**：两个分组可以有同名账号文件，只用 a.file
                // 会让两条撞成一条（见 accountKey 的注释）。
                <div key={accountKey(a)} className="rounded-2xl bg-background/60 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span
                      className={
                        'min-w-0 truncate text-sm font-medium ' +
                        (tier === 'expired' ? 'text-muted-foreground' : '')
                      }
                    >
                      {a.nickname || a.uid}
                    </span>
                    {/* 多于一个分组时才标归属：交错排列之后，光看昵称分不出
                        这条属于哪个池。单分组时它只是句废话。 */}
                    {multiGroup && (
                      <span className="shrink-0 text-[10px] text-muted-foreground">
                        {a.group.name}
                      </span>
                    )}
                    <span
                      className={'shrink-0 text-[10px] font-medium ' + availabilityClass(tier)}
                      title={titleKey ? t(titleKey) : undefined}
                    >
                      {statusLabel}
                    </span>
                  </div>
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
                    <div
                      className="h-full rounded-full transition-all"
                      style={{width: `${pct}%`, background: vis.barColor}}
                    />
                  </div>
                  <div className="mt-1.5 flex items-center justify-between gap-2">
                    <span className={'text-[11px] tabular-nums ' + vis.textClass} title={t('accounts.colExpiry')}>
                      {fmtRemain(a.remain_seconds)}
                    </span>
                    <span
                      className={
                        'text-[11px] font-medium tabular-nums ' +
                        (typeof credOf(a) !== 'number'
                          ? 'text-muted-foreground'
                          : (credOf(a) as number) <= 0
                            ? 'text-red-600 dark:text-red-400'
                            : (credOf(a) as number) < 200
                              ? 'text-amber-600 dark:text-amber-400'
                              : 'text-foreground')
                      }
                      title={t('metric.credits')}
                    >
                      {typeof credOf(a) === 'number'
                        ? t('metric.creditAmount', {n: fmtNumber(credOf(a))})
                        : ''}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        ) : poolFailed ? null : (
          // 账号那一块没取到时**什么都不说**：「暂无账号」等于告诉用户
          // 「你一个账号都没有」，而事实是没取到（顶部那条说明已经讲了）。
          <EmptyState
            icon={Users}
            title={t('dashboard.noAccounts')}
            description={t('dashboard.noAccountsHint')}
            className="flex flex-col items-center justify-center py-12 text-center"
          />
        )}
      </section>
    </div>
  );
}

/**
 * 「反代上游」面板里**一个分组**那一块。
 *
 * 单分组时 `showName` 为假，渲染出来与改造前**逐字相同**（连接状态那一行的
 * 标签仍是「连接状态」）；多分组时才把标签换成分组名，让每一块都说得清自己
 * 是哪个池。
 */
function UpstreamBlock({
  slice, realm, showName,
}: {slice: GroupSlice; realm: string; showName: boolean}) {
  const t = useT();
  const up = slice.upstream;
  if (!up) {
    return (
      <>
        <div className="flex items-center justify-between text-xs">
          <span className="text-muted-foreground">
            {showName ? slice.name : t('dashboard.connStatus')}
          </span>
        </div>
        <p className="text-[11px] text-muted-foreground">{t('dashboard.noUpstreamStatus')}</p>
      </>
    );
  }
  const pool = poolTotals(up, realm);
  const rows: [string, string | number][] = [
    // 账号类计数按当前版本（上游 realm_totals）；粘性会话与 Redis
    // 无版本之分，保持全局
    [t('dashboard.healthyAccounts'), pool.known ? pool.healthy : '—'],
    [t('dashboard.cooling'), pool.known ? pool.cooling : '—'],
    [t('dashboard.degraded'), pool.known ? degradedCount(slice, realm) : '—'],
    [t('dashboard.disabled'), pool.known ? pool.disabled : '—'],
    [t('dashboard.stickySessions'), up.sticky_sessions ?? 0],
    [t('dashboard.redisMode'), up.redis_mode ?? '—'],
  ];
  return (
    <>
      <div className="flex items-center justify-between text-xs">
        <span className={showName ? 'truncate font-medium' : 'text-muted-foreground'}>
          {showName ? slice.name : t('dashboard.connStatus')}
        </span>
        {up.connected ? (
          <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
            {t('dashboard.connected')}
          </Badge>
        ) : (
          <Badge variant="destructive" className="rounded-full">
            {t('dashboard.unavailable')}
          </Badge>
        )}
      </div>
      {rows.map(([k, v]) => (
        <div key={k} className="flex items-center justify-between text-xs">
          <span className="text-muted-foreground">{k}</span>
          <span className="font-medium tabular-nums">{String(v)}</span>
        </div>
      ))}
      {pool.globalOnly && !up.error && (
        <p className="text-[11px] text-muted-foreground">
          {t('dashboard.globalTotalsNote')}
        </p>
      )}
      {up.error && <p className="text-[11px] text-red-500">{up.error}</p>}
    </>
  );
}

/** 统计卡片的骨架。外壳与 StatCard 逐项对齐（同高、同圆角、同底色），到位时不跳 */
function StatCardSkeleton() {
  return (
    <div className="min-h-[88px] rounded-[20px] bg-muted px-3.5 py-3 sm:min-h-[96px] sm:px-4">
      <div className="flex items-start justify-between gap-2">
        <SkeletonBar className="h-2.5 w-16" />
        <SkeletonBar className="h-6 w-6 rounded-full" />
      </div>
      <SkeletonBar className="mt-3 h-6 w-20" />
      <SkeletonBar className="mt-2 h-2.5 w-24" />
    </div>
  );
}

/**
 * 首屏骨架。
 *
 * 结构与真实内容**逐块对应**（五张卡片 / 趋势图 + 上游面板 / 健康快照），而不是
 * 一坨居中的转圈：数据到位时版面不会整体跳一下，加载期间也不会看起来像空白页。
 * 页面每 30 秒心跳刷新一次，但只有「一次都没取到」才会走到这里（见
 * use-async-data 的 isInitialLoading），所以不会一闪一闪。
 */
function DashboardSkeleton() {
  return (
    <>
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-5 md:gap-4">
        {Array.from({length: 5}, (_, i) => <StatCardSkeleton key={i} />)}
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-[20px] bg-muted p-4 lg:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <SkeletonBar className="h-3.5 w-28" />
            <SkeletonBar className="h-3 w-20" />
          </div>
          <SkeletonBar className="h-[220px] w-full rounded-2xl" />
        </div>
        <div className="rounded-[20px] bg-muted p-4">
          <SkeletonBar className="mb-3 h-3.5 w-20" />
          <div className="space-y-3">
            {Array.from({length: 6}, (_, i) => (
              <div key={i} className="flex items-center justify-between">
                <SkeletonBar className="h-2.5 w-16" />
                <SkeletonBar className="h-2.5 w-8" />
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="rounded-[20px] bg-muted p-4">
        <SkeletonBar className="mb-3 h-3.5 w-24" />
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({length: 6}, (_, i) => (
            <div key={i} className="rounded-2xl bg-background/60 p-3">
              <div className="flex items-center justify-between gap-2">
                <SkeletonBar className="h-3.5 w-20" />
                <SkeletonBar className="h-2.5 w-10" />
              </div>
              <SkeletonBar className="mt-2 h-1.5 w-full rounded-full" />
              <div className="mt-1.5 flex items-center justify-between gap-2">
                <SkeletonBar className="h-2.5 w-12" />
                <SkeletonBar className="h-2.5 w-10" />
              </div>
            </div>
          ))}
        </div>
      </section>
    </>
  );
}

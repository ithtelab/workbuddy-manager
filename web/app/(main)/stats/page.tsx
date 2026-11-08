'use client';

import {useCallback, useState} from 'react';
import {Activity, TrendingUp, KeyRound, Cpu, Wrench, RotateCcw, Coins, AlertTriangle, Server, CalendarDays, UserRound} from 'lucide-react';
import {
  Area,
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useAsyncAll} from '@/lib/use-async-data';
import {statsApi, errText, type StatsRange} from '@/lib/api';
import type {
  AccountModelUsageRow,
  AccountUsageRow,
  StatsSummary,
  UpstreamStats,
  UsageBreakdown,
  UsagePoint,
  UsageHourPoint,
} from '@/lib/types';
import {
  chartSpecFor,
  dailySeries,
  hourlySeries,
  todayKeyLocal,
  type ChartPoint,
} from '@/lib/usage-chart';
import {fmtCompact, fmtNumber, fmtCredit, fmtMoney } from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {StatCard} from '@/components/common/layout/StatCard';
import Link from 'next/link';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {useAuth} from '@/lib/auth-context';
import {useRealm} from '@/lib/realm-context';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';

const CHART_COLORS = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
];

/**
 * 空数组的稳定引用。
 *
 * 必须是模块级常量，不能每次渲染现写一个 `[]`：它们是「还没取到」时的兜底值，
 * 每帧新建的数组会让下游任何依赖它的 `useMemo` / `useEffect` 每帧重算
 * （这里就有：`chartData` 与两张分解表的 `max` 都从它们派生）。
 */
const EMPTY_POINTS: UsagePoint[] = [];
const EMPTY_HOURS: UsageHourPoint[] = [];
const EMPTY_BREAKDOWN: UsageBreakdown[] = [];

/**
 * 统计健康提示的详情本地化。
 *
 * 服务端（`server/routers/stats.py` 的 `_usage_health`）同时返回**结构化数字**
 * `logs_today` 和一句中文 `detail`。这里用数字拼译文，**不再正则反解那句中文**。
 *
 * 为什么改掉正则：原先按 `^今天已有 (\d+) 次…` 反解，后来服务端为了标明版本
 * 把文案改成 `今天国内版已有 N 次…`，正则立刻全都不匹配 —— 结果是五种语言的
 * 译文全部失效，非中文用户看到原始中文句子，而且**不会有任何报错**。
 * 用结构化字段就没有这种「改文案即静默失效」的耦合。
 *
 * 退路：拿不到数字（服务端旧版本）时原样显示 `detail`，不显示空白。
 */
function usageHealthDetail(
  health: {detail?: string; logs_today?: number},
  t: (key: string, params?: Record<string, string>) => string,
): string {
  const n = health.logs_today;
  if (typeof n === 'number' && n > 0) {
    return t('stats.usageHealthDetail', {n: fmtNumber(n)});
  }
  return health.detail || '';
}

export default function StatsPage() {
  const {isAdmin} = useAuth();
  // 统计随顶部版本切换：两个版本走的是不同账号池，混在一起看没有意义
  const {realm, label: realmName} = useRealm();
  const t = useT();
  /**
   * 时段。默认「今日」（issue #53）。
   *
   * 为什么默认改成今日：上方四张卡片本来就是「今日 / 本周」口径，而下面的趋势图
   * 与两张分解表跟的是这个选择器——原先默认「近 30 天」，同一屏两种口径并存，
   * 很容易把 30 天的数字当成今天的（报告者的原话）。而绝大多数时候打开这页就是
   * 想看「今天用了多少」。
   *
   * 取值是**天数**而非枚举：`1` 就是今天一天（后端口径是「近 N 天且含今天」，
   * 见 `server/routers/stats.py` 的 `_since`），所以趋势图、按模型、按密钥三处
   * 与选择器天然同一口径，不需要各自翻译一遍。
   */
  const [days, setDays] = useState('1');
  /** 自定义日期（issue #257）：两端都含；只有选「自定义」且两格都填了才生效。 */
  const [customStart, setCustomStart] = useState('');
  const [customEnd, setCustomEnd] = useState('');
  const isCustom = days === 'custom';
  const range: StatsRange | undefined =
    isCustom && customStart && customEnd ? {start: customStart, end: customEnd} : undefined;
  const spanDays = (() => {
    if (!isCustom || !customStart || !customEnd) return Number(days) || 1;
    const a = Date.parse(customStart), b = Date.parse(customEnd);
    if (!Number.isFinite(a) || !Number.isFinite(b)) return 30;
    return Math.min(3650, Math.max(1, Math.round(Math.abs(b - a) / 86400000) + 1));
  })();
  const d = spanDays;

  /**
   * 本页四份统计。
   *
   * `realm` 与 `days` 都算**数据上下文**（而不是「查询范围」）：改了时段，四张卡片
   * 与两张分解表的含义整体变了，旧数字留在屏幕上就成了「选择器写着今日、数字其实
   * 是近 30 天」——所以清空重取，宁可闪一下骨架。
   */
  const {
    values,
    errors,
    isInitialLoading,
    isInitialFailed,
    isRefreshing,
    reload,
  } = useAsyncAll(
    {
      summary: () => statsApi.summary(realm),
      daily: () => statsApi.daily(d, realm, range),
      // 「今日」的图按小时画（见 lib/usage-chart.ts 的说明）：只有这个范围需要
      // 小时数据，其它范围直接给空数组，省一次请求。
      hourly: () => (days === '1' ? statsApi.hourly(undefined, realm)
                                  : Promise.resolve([] as UsageHourPoint[])),
      byModel: () => statsApi.byModel(d, realm, range),
      byKey: () => statsApi.byKey(d, realm, range),
      byAccount: () => statsApi.byAccount(d, realm, range),
      byAccountModel: () => statsApi.byAccountModel(d, realm, range),
    },
    [realm, days, customStart, customEnd],
  );

  /**
   * 上游自己那份统计（issue #59），**单独一个 hook**。改动前它也是单独拉的
   * （在原代码里不进 `allSettled`），拆开是恢复那个结构，理由有两条：
   *
   *  · 它失败时**故意退化成 `{available: false}` 而不是抛错**，好让面板就地写清
   *    原因、不弹提示（上游没起来、镜像太旧都会取不到，那是常态）。可这样一来它
   *    就成了一次「成功的取数」——若和上面那组放在一起，五份全挂时它照样有值，
   *    「一份都没取到」的判据就被顶掉了：页面不进整页错误态，反而照常渲染四张 0
   *    卡片和「暂无数据」，正是本批要修的那句谎话。
   *  · 它本来就不属于 `errors` 的语义范围——上面那条「部分数据加载失败」是给本页
   *    四份数据用的，上游取不到不该算进去。
   *
   * 依赖用 `refreshDeps` 而不是 `deps`：时段 / 版本变了要跟着重取（改动前就是
   * 这样），但**不清空**——它跟时段无关，清空只会让面板闪一下「上游统计暂不可用」。
   */
  const {values: upstreamValues, reload: reloadUpstream} = useAsyncAll(
    {
      upstream: async () => {
        try {
          return await statsApi.upstream();
        } catch {
          return {available: false} as UpstreamStats;
        }
      },
    },
    [],
    [realm, days],
  );

  const summary: StatsSummary | null = values.summary ?? null;
  const daily: UsagePoint[] = values.daily ?? EMPTY_POINTS;
  const byModel: UsageBreakdown[] = values.byModel ?? EMPTY_BREAKDOWN;
  const byKey: UsageBreakdown[] = values.byKey ?? EMPTY_BREAKDOWN;
  const byAccount: AccountUsageRow[] = values.byAccount?.items ?? [];
  const byAccountModel: AccountModelUsageRow[] = values.byAccountModel?.items ?? [];
  const upstream: UpstreamStats | null = upstreamValues.upstream ?? null;

  const failedCount = Object.keys(errors).length;

  /** 重试：两组一起重来，避免「点了重试、上游面板还是旧的」 */
  const retry = useCallback(() => {
    reload();
    reloadUpstream();
  }, [reload, reloadUpstream]);

  // 用量随调用持续累计，心跳刷新让页面保持接近实时
  useHeartbeat(retry, 60000);

  const spec = chartSpecFor(d);
  const hourly: UsageHourPoint[] = values.hourly ?? EMPTY_HOURS;
  /**
   * 趋势图的点。**粒度跟着范围走**（粒度/形态的判据在 lib/usage-chart.ts）：
   *  · 今日 → 24 个小时桶（后端已补零；这里只做标签与「当前小时」标记）；
   *  · 多日 → 按天并补齐到窗口长度（缺的那天不再是「凭空消失」而是画成 0）。
   */
  const chartData: ChartPoint[] = spec.granularity === 'hour'
    ? hourlySeries(hourly, new Date().getHours())
    : dailySeries(daily, d, todayKeyLocal());

  /** 今日失败总数（4xx + 5xx）。单独来自请求日志——用量汇总只含成功请求。 */
  const todayFailed =
    (summary?.failures?.today_4xx ?? 0) + (summary?.failures?.today_5xx ?? 0);

  /**
   * 页头抽成变量，是为了让下面「首屏骨架 / 错误态」那一段也带上它——
   * 否则加载期间连标题都没有，用户会以为跳错了页面。
   */
  const header = (
    <PageHeader
      title={t('stats.title')}
      description={t('stats.description', {realm: realmName})}
      actions={
        <>
          <Select value={days} onValueChange={setDays}>
            <SelectTrigger className="h-8 w-[130px] rounded-full" aria-label={t('logs.filterRange')}><SelectValue /></SelectTrigger>
            <SelectContent>
              {/* 「今日」排在第一位并作为默认：与上方卡片的「今日」口径对齐 */}
              <SelectItem value="1">{t('stats.today')}</SelectItem>
              <SelectItem value="7">{t('stats.last7')}</SelectItem>
              <SelectItem value="30">{t('stats.last30')}</SelectItem>
              <SelectItem value="90">{t('stats.last90')}</SelectItem>
              <SelectItem value="custom">{t('stats.customRange')}</SelectItem>
            </SelectContent>
          </Select>
          {isCustom && (
            <div className="flex items-center gap-1.5">
              <CalendarDays className="h-3.5 w-3.5 text-muted-foreground" />
              <Input type="date" value={customStart} max={customEnd || undefined}
                     aria-label={t('stats.startDate')}
                     onChange={(e) => setCustomStart(e.target.value)}
                     className="h-8 w-[140px] rounded-full text-xs" />
              <span className="text-xs text-muted-foreground">–</span>
              <Input type="date" value={customEnd} min={customStart || undefined}
                     aria-label={t('stats.endDate')}
                     onChange={(e) => setCustomEnd(e.target.value)}
                     className="h-8 w-[140px] rounded-full text-xs" />
            </div>
          )}
          {isAdmin && (
            <ConfirmDialog
              title={t('stats.repairTitle')}
              description={t('stats.repairDesc')}
              confirmText={t('stats.repairStart')}
              onConfirm={async () => {
                try {
                  const r = await statsApi.repairUsage();
                  if (r.repaired > 0) {
                    notify.ok(
                      t('stats.repaired'),
                      t('stats.repairedDetail', {
                        requests: fmtNumber(r.requests),
                        tokens: fmtNumber(r.tokens),
                      }),
                    );
                  } else {
                    notify.info(t('stats.nothingToRepair'), t('stats.nothingToRepairDesc'));
                  }
                  reload();
                } catch (e) {
                  notify.err(errText(e));
                }
              }}
              trigger={
                <Button variant="outline" size="sm" className="rounded-full">
                  <Wrench className="h-3.5 w-3.5" />
                  {t('stats.repairButton')}
                </Button>
              }
            />
          )}
          {isAdmin && (
            <ConfirmDialog
              title={t('stats.rebuildTitle')}
              description={t('stats.rebuildDesc')}
              confirmText={t('stats.rebuildStart')}
              destructive
              onConfirm={async () => {
                try {
                  const r = await statsApi.rebuildUsage();
                  const d = r.tokens_delta;
                  notify.ok(
                    t('stats.rebuilt'),
                    t('stats.rebuiltDetail', {
                      before: fmtNumber(r.rows_before),
                      after: fmtNumber(r.rows_after),
                    }) +
                      (d !== 0
                        ? t('stats.rebuiltDetailTokens', {
                            delta: `${d > 0 ? '+' : ''}${fmtNumber(d)}`,
                          })
                        : t('stats.rebuiltDetailSame')),
                  );
                  reload();
                } catch (e) {
                  notify.err(errText(e));
                }
              }}
              trigger={
                <Button variant="outline" size="sm" className="rounded-full text-amber-600 dark:text-amber-400">
                  <RotateCcw className="h-3.5 w-3.5" />
                  {t('stats.rebuildButton')}
                </Button>
              }
            />
          )}
        </>
      }
    />
  );

  /**
   * 首屏还没拿到统计：整块数据区换成骨架。
   *
   * 为什么整块换而不是各块单独兜：四张卡片与两张分解表都是「数字 / 表格」，
   * 数据没到时会渲染成 0 和「暂无数据」——看起来像「今天一次都没用」，
   * 其实只是还没取到。这和本批其它页修的是同一件事：**界面别在说谎**。
   */
  if (isInitialFailed || isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? (
          <LoadError variant="page" onRetry={retry} />
        ) : (
          <StatsSkeleton />
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* 部分失败：已经显示出来的数字仍然是对的，只是可能不是最新的。
          用一条常驻提示说明，**不把内容顶掉**；下一次刷新成功后自动消失。 */}
      {failedCount > 0 && <LoadError message={t('state.partialFailed')} onRetry={retry} />}

      {/* 统计没在累计时明确提示。
          统计是旁路写入（失败不影响转发），所以坏了以后界面看不出异常——
          数字只是停着不动、页面照常刷新。这里把「今天有请求但统计为 0」
          这种组合直接摆出来，并指向右侧的「修复统计」按钮。 */}
      {summary?.usage_health && !summary.usage_health.ok && (
        <div className="flex items-start gap-2 rounded-[16px] border border-amber-500/30 bg-amber-500/[0.07] px-3.5 py-3">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
          <div className="text-[11px] leading-5">
            <span className="font-medium text-amber-700 dark:text-amber-300">
              {t('stats.usageHealthTitle')}
            </span>
            <div className="text-muted-foreground">
              {usageHealthDetail(summary.usage_health, t)}
            </div>
          </div>
        </div>
      )}

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4 md:gap-4">
        <StatCard
          label={t('stats.todayRequests')}
          value={fmtNumber(summary?.today_requests)}
          hint={
            // 有失败时把失败数说在前面：这个卡片是用户查「今天出了什么事」的
            // 第一站，而「用量 N token」是次要信息。
            //
            // 也要说清**失败不计入上面的数字**：请求数来自用量汇总（只含成功
            // 请求），所以「今天 0 次请求 + 31 次失败」是可能的——那正是全池
            // 中断的形态。不说清的话用户会以为卡片坏了。
            todayFailed > 0
              ? t('stats.failedHint', {n: fmtNumber(todayFailed)})
              : t('stats.tokenHint', {v: fmtCompact(summary?.today_tokens)})
          }
          icon={Activity}
          tone={todayFailed > 0 ? 'warning' : 'info'}
          hintTone={todayFailed > 0 ? 'warning' : undefined}
          delay={0}
        />
        <StatCard
          label={t('stats.weekRequests')}
          value={fmtNumber(summary?.week_requests)}
          hint={t('stats.tokenHint', {v: fmtCompact(summary?.week_tokens)})}
          icon={TrendingUp}
          tone="accent"
          delay={0.05}
        />
        <StatCard
          label={t('stats.todayPaid')}
          value={fmtCredit(summary?.today_credit)}
          hint={
            summary?.today_credit
              ? t('stats.todayPaidHint', {v: fmtCredit(summary?.week_credit)})
              : t('stats.noCreditFromUpstream')
          }
          icon={Coins}
          tone="warning"
          delay={0.1}
        />
        <StatCard
          label={t('stats.activeKeys')}
          value={fmtNumber(summary?.active_keys)}
          hint={summary?.top_model ? t('stats.topModel', {model: summary.top_model}) : t('stats.distributing')}
          icon={KeyRound}
          tone="success"
          delay={0.15}
        />
      </section>

      {/* 按 DeepSeek 官方价估算的费用（issue #380）。
          和上面那张「积分」卡并排看才完整：积分是上游**真实扣**的，这里回答的是
          「同样这些 token，走官方 API 要花多少钱」。两者口径不同，所以分开放、
          分开标注，不合成一个数字。 */}
      <section className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Coins className="h-4 w-4" />
            {t('stats.costTitle')}
          </div>
          <Link
            href="/settings/models"
            className="text-[11px] text-muted-foreground underline-offset-2 hover:underline"
          >
            {t('stats.costPriceLink')}
          </Link>
        </div>
        <div className="grid grid-cols-3 gap-3">
          {([
            ['today', t('stats.costToday'), summary?.today_cost, summary?.today_unpriced],
            ['week', t('stats.costWeek'), summary?.week_cost, summary?.week_unpriced],
            ['total', t('stats.costTotal'), summary?.total_cost, summary?.total_unpriced],
          ] as const).map(([key, label, value, unpriced]) => (
            <div key={key}>
              <div className="text-[11px] text-muted-foreground">{label}</div>
              <div className="mt-0.5 text-lg font-semibold tabular-nums">
                {fmtMoney(value, summary?.cost_currency)}
              </div>
              {!!unpriced && (
                // 有没算进费用的调用时必须说出来：否则用户会拿一个悄悄缩水的
                // 金额当成「全量花费」。
                <div className="mt-0.5 text-[10px] text-amber-600 dark:text-amber-400">
                  {t('stats.costUnpriced', {n: fmtNumber(unpriced)})}
                </div>
              )}
            </div>
          ))}
        </div>
        <p className="mt-3 text-[11px] leading-5 text-muted-foreground">
          {t('stats.costNote')}
        </p>
      </section>

      <section className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex items-center justify-between">
          <div className="text-sm font-medium">{t('stats.tokenTrend')}</div>
          {/* 副标题必须与**实际粒度**一致：今日是按小时画的，写「按天聚合」就是
              在骗人（这也是最早那版的表现：选择器说今日、图是一根柱子）。 */}
          <div className="text-[11px] text-muted-foreground">
            {spec.granularity === 'hour' ? t('stats.hourlyAgg') : t('stats.dailyAgg')}
          </div>
        </div>
        <div className="h-[260px] w-full">
          {chartData.length ? (
            <ResponsiveContainer width="100%" height="100%">
              {/* 用 ComposedChart 而不是 BarChart：BarChart 会忽略非 Bar 子组件，
                  失败曲线（Line）根本不会被渲染 —— 实测踩过，图上一条线都没有。 */}
              <ComposedChart data={chartData} margin={{top: 4, right: 8, bottom: 0, left: -8}} barCategoryGap="20%">
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis
                  dataKey="label"
                  tickLine={false}
                  axisLine={false}
                  fontSize={11}
                  stroke="var(--muted-foreground)"
                  /* 24 个小时（或 90 天）标签全画出来会糊成一片，按 spec 抽稀。 */
                  interval={Math.max(0, spec.tickEvery - 1)}
                />
                <YAxis yAxisId="tok" tickLine={false} axisLine={false} fontSize={11}
                       stroke="var(--muted-foreground)" tickFormatter={(v) => fmtCompact(Number(v))} />
                {/* 成功 / 失败是「次数」，与 token 差好几个数量级；放右轴才看得见。 */}
                <YAxis yAxisId="cnt" orientation="right" tickLine={false} axisLine={false}
                       fontSize={11} stroke="var(--muted-foreground)" allowDecimals={false}
                       width={40} />
                <Tooltip
                  cursor={{fill: 'var(--accent)'}}
                  contentStyle={{
                    background: 'var(--popover)',
                    border: '1px solid var(--border)',
                    borderRadius: 12,
                    fontSize: 12,
                  }}
                  formatter={(value, name) => {
                    const label = String(name);
                    if (label === 'tokens') return [fmtNumber(Number(value)), 'Token'];
                    if (label === 'failed') {
                      return [fmtNumber(Number(value)), t('dashboard.failedRequests')];
                    }
                    if (label === 'success') {
                      return [fmtNumber(Number(value)), t('stats.successRequests')];
                    }
                    return [fmtNumber(Number(value)), t('metric.requests')];
                  }}
                />
                {spec.shape === 'bar' ? (
                  <Bar
                    yAxisId="tok"
                    dataKey="tokens"
                    name="tokens"
                    fill="var(--chart-1)"
                    radius={[4, 4, 0, 0]}
                    /* 限制柱宽：只有一两天数据时，柱子不会被拉伸占满整个图表 */
                    maxBarSize={48}
                  >
                    {/* 当前小时单独上色：一眼看出「现在走到哪一格」，
                        右边的空档也就自然读成「今天还没到那些时候」。 */}
                    {chartData.map((p) => (
                      <Cell
                        key={p.full}
                        fill={p.isNow ? 'var(--chart-2)' : 'var(--chart-1)'}
                      />
                    ))}
                  </Bar>
                ) : (
                  /* 30 / 90 天用面积：柱子细得看不清起伏，而面积图的形状对
                     「总量趋势」更直观（尖峰一眼可见）。 */
                  <Area
                    yAxisId="tok"
                    type="monotone"
                    dataKey="tokens"
                    name="tokens"
                    stroke="var(--chart-1)"
                    fill="var(--chart-1)"
                    fillOpacity={0.18}
                    strokeWidth={2}
                  />
                )}
                {/* 成功数（issue #369 第 3 点）：与失败同一根右轴，量级可比；
                    左轴 token 不会被次数压扁。 */}
                <Line
                  yAxisId="cnt"
                  type="monotone"
                  dataKey="success"
                  name="success"
                  stroke="var(--chart-2)"
                  strokeWidth={1.5}
                  dot={false}
                />
                {/* 失败数用一条线叠在同一张图上：它与 token 柱不同量级，做成柱子
                    会把柱形压扁。线只作「那天出过事」的信号，具体数值看悬停。
                    没有失败时贴着 0，不干扰读数。 */}
                <Line
                  yAxisId="cnt"
                  type="monotone"
                  dataKey="failed"
                  name="failed"
                  stroke="var(--destructive)"
                  strokeWidth={1.5}
                  strokeDasharray="4 3"
                  dot={false}
                />
              </ComposedChart>
            </ResponsiveContainer>
          ) : (
            <div className="grid h-full place-items-center text-xs text-muted-foreground">{t('stats.noUsageData')}</div>
          )}
        </div>
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <BreakdownPanel title={t('stats.byModel')} icon={Cpu} items={byModel} throughput />
        <BreakdownPanel title={t('stats.byKey')} icon={KeyRound} items={byKey} />
      </section>

      {/* 按「日期 × 账号」聚合（issue #402）：看每个号每天用了多少、扣了多少积分。 */}
      <AccountUsagePanel items={byAccount} />
      <AccountModelUsagePanel items={byAccountModel} />

      {/* 上游自己那份统计（issue #59）。**单独一段、口径写明**：它含直连上游的
          调用、且自上游进程启动累计，与上面的时段选择器无关——混着看会得出
          「今日请求包含了直连流量」这种错误结论。取不到时只写原因，不弹提示。 */}
      <section className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Server className="h-4 w-4" />
            {t('stats.upstreamTitle')}
          </div>
          <div className="text-[11px] text-muted-foreground">{t('stats.upstreamNote')}</div>
        </div>
        {upstream && (!upstream.available || upstream.enabled === false) ? (
          <p className="text-xs leading-5 text-muted-foreground">
            {t('stats.upstreamUnavailable')}
            {upstream.error ? `：${upstream.error}` : ''}
            {upstream.message ? `（${upstream.message}）` : ''}
          </p>
        ) : upstream?.total ? (
          <>
            <div className="mb-3 flex flex-wrap gap-x-6 gap-y-2 text-xs">
              <UpstreamMetric label={t('metric.requests')} value={fmtNumber(upstream.total.requests)} />
              <UpstreamMetric label={t('stats.upstreamSuccess')} value={fmtNumber(upstream.total.success)} />
              <UpstreamMetric
                label={t('dashboard.failedRequests')}
                value={fmtNumber(upstream.total.failed)}
                tone={upstream.total.failed ? 'warning' : undefined}
              />
              <UpstreamMetric label="Token" value={fmtCompact(upstream.total.total_tokens)} />
              <UpstreamMetric label={t('metric.paid')} value={fmtCredit(upstream.total.credit)} />
              <UpstreamMetric
                label={t('stats.cacheHitRate')}
                value={fmtPercent(upstream.total.cache_hit_rate)}
              />
            </div>
            {(upstream.models?.length ?? 0) > 0 && (
              <Table>
                <TableHeader>
                  <TableRow className="border-b border-border/60 hover:bg-transparent">
                    <TableHead className="pl-0 text-[11px] text-muted-foreground">{t('metric.name')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('metric.requestsShort')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">Token</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('metric.paid')}</TableHead>
                    <TableHead className="pr-0 text-[11px] text-muted-foreground">{t('stats.cacheHitRate')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {upstream.models?.map((m, i) => (
                    <TableRow key={`${m.model}-${i}`} className="border-b border-border/40">
                      <TableCell className="pl-0">
                        <span className="max-w-[220px] truncate text-xs font-medium" title={m.model || undefined}>{m.model || t('metric.unknown')}</span>
                      </TableCell>
                      <TableCell className="text-xs tabular-nums">{fmtNumber(m.requests)}</TableCell>
                      <TableCell className="text-xs tabular-nums">{fmtCompact(m.total_tokens)}</TableCell>
                      <TableCell className="text-xs tabular-nums">{fmtCredit(m.credit)}</TableCell>
                      <TableCell className="pr-0 text-xs tabular-nums">{fmtPercent(m.cache_hit_rate)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </>
        ) : null}
      </section>
    </div>
  );
}

/** 按「账号 × 模型」的用量表（issue #369 第 2 点）。 */
function AccountModelUsagePanel({items}: {items: AccountModelUsageRow[]}) {
  const t = useT();
  return (
    <section className="rounded-[20px] bg-muted p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        <Cpu className="h-4 w-4" />
        {t('stats.byAccountModel')}
      </div>
      {items.length ? (
        <div className="max-h-[420px] overflow-auto">
          <Table>
            <TableHeader>
              <TableRow className="border-b border-border/60 hover:bg-transparent">
                <TableHead className="text-[11px] text-muted-foreground">{t('stats.account')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('stats.model')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('metric.requestsShort')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">Token</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('metric.paid')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((row, i) => (
                <TableRow key={`${row.account}-${row.model}-${i}`} className="border-b border-border/40">
                  <TableCell className="max-w-[220px] truncate text-xs" title={row.account}>
                    {row.nickname || row.uid8 || row.account}
                  </TableCell>
                  <TableCell className="max-w-[240px] truncate text-xs font-mono" title={row.model}>
                    {row.model}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtNumber(row.requests)}</TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtCompact(row.tokens)}</TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtCredit(row.credit)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      ) : (
        <div className="text-xs text-muted-foreground">{t('stats.noUsageData')}</div>
      )}
    </section>
  );
}

/** 按「日期 × 账号」的用量表（issue #402）。 */
function AccountUsagePanel({items}: {items: AccountUsageRow[]}) {
  const t = useT();
  return (
    <section className="rounded-[20px] bg-muted p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        <UserRound className="h-4 w-4" />
        {t('stats.byAccount')}
      </div>
      {items.length ? (
        <div className="max-h-[420px] overflow-auto">
          <Table>
            <TableHeader>
              <TableRow className="border-b border-border/60 hover:bg-transparent">
                <TableHead className="text-[11px] text-muted-foreground">{t('stats.date')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('stats.account')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('metric.requestsShort')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">Token</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('metric.paid')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((row, i) => (
                <TableRow key={`${row.day}-${row.account}-${i}`} className="border-b border-border/40">
                  <TableCell className="whitespace-nowrap text-xs tabular-nums">{row.day}</TableCell>
                  <TableCell className="max-w-[240px] truncate text-xs" title={row.account}>
                    {row.nickname || row.uid8 || row.account}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtNumber(row.requests)}</TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtCompact(row.tokens)}</TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtCredit(row.credit)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      ) : (
        <div className="text-xs text-muted-foreground">{t('stats.noUsageData')}</div>
      )}
    </section>
  );
}

/** 上游统计里的一个数字（标签 + 值）。失败数用琥珀色标出来。 */
function UpstreamMetric({label, value, tone}: {label: string; value: string; tone?: 'warning'}) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-muted-foreground">{label}</span>
      <span className={`font-medium tabular-nums ${tone === 'warning' ? 'text-amber-600 dark:text-amber-400' : ''}`}>
        {value}
      </span>
    </div>
  );
}

/** 上游给的是 0~1 的比例；拿不到就显示占位符，不显示 0%（那是「一次没命中」的意思）。 */
function fmtPercent(v: number | undefined | null): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
  return `${Math.round(v * 100)}%`;
}

function BreakdownPanel({
  title,
  icon: Icon,
  items,
  throughput = false,
}: {
  title: string;
  icon: typeof Cpu;
  items: UsageBreakdown[];
  /** 显示「输出 tok/s」列（只有按模型表需要，按密钥没有这项） */
  throughput?: boolean;
}) {
  const t = useT();
  const max = Math.max(1, ...items.map((i) => i.prompt_tokens + i.completion_tokens));
  return (
    <div className="rounded-[20px] bg-muted p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        <Icon className="h-4 w-4" />
        {title}
      </div>
      {items.length ? (
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-0 text-[11px] text-muted-foreground">{t('metric.name')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('metric.requestsShort')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">Token</TableHead>
              {throughput && (
                <TableHead className="whitespace-nowrap text-[11px] text-muted-foreground">
                  {t('stats.tokensPerSec')}
                </TableHead>
              )}
              <TableHead className="whitespace-nowrap text-[11px] text-muted-foreground">{t('stats.colCost')}</TableHead>
              <TableHead className="pr-0 text-[11px] text-muted-foreground">{t('metric.paid')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((it, i) => {
              const tokens = it.prompt_tokens + it.completion_tokens;
              return (
                <TableRow key={`${it.name}-${i}`} className="border-b border-border/40">
                  <TableCell className="pl-0">
                    <div className="flex items-center gap-2">
                      <span className="h-2 w-2 rounded-full" style={{background: CHART_COLORS[i % CHART_COLORS.length]}} />
                      <span className="max-w-[140px] truncate text-xs font-medium" title={it.name || undefined}>{it.name || t('metric.unknown')}</span>
                    </div>
                    <div className="mt-1.5 h-1 w-full max-w-[140px] overflow-hidden rounded-full bg-border">
                      <div
                        className="h-full rounded-full"
                        style={{
                          width: `${(tokens / max) * 100}%`,
                          background: CHART_COLORS[i % CHART_COLORS.length],
                        }}
                      />
                    </div>
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtNumber(it.requests)}</TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtCompact(tokens)}</TableCell>
                  {throughput && (
                    <TableCell className="whitespace-nowrap text-xs tabular-nums">
                      {typeof it.tokens_per_sec === 'number' ? (
                        <span title={t('stats.throughputHint', {n: it.throughput_samples ?? 0})}>
                          {it.tokens_per_sec.toFixed(1)}
                        </span>
                      ) : (
                        <span className="text-muted-foreground/70">—</span>
                      )}
                    </TableCell>
                  )}
                  <TableCell className="whitespace-nowrap text-xs tabular-nums">
                    {it.cost != null ? fmtMoney(it.cost) : (
                      <span className="text-muted-foreground/70" title={t('stats.costUnavailableHint')}>—</span>
                    )}
                  </TableCell>
                  <TableCell className="pr-0 text-xs tabular-nums">
                    {it.credit > 0 ? fmtCredit(it.credit) : <span className="text-muted-foreground/70">—</span>}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      ) : (
        <EmptyState
          icon={Activity}
          title={t('stats.emptyTitle')}
          description={t('stats.emptyDesc')}
          className="flex flex-col items-center justify-center py-10 text-center"
        />
      )}
    </div>
  );
}

/**
 * 首屏骨架。
 *
 * 逐块对着真实版面：四张统计卡（同高 88/96px）、趋势图（同高 260px）、两张分解表、
 * 上游面板。尺寸必须对齐——骨架比真实内容矮的话，数据到位时整页会往下窜一下，
 * 比没有骨架更难受。
 */
function StatsSkeleton() {
  return (
    <>
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4 md:gap-4">
        {Array.from({length: 4}, (_, i) => (
          <div
            key={i}
            className="min-h-[88px] rounded-[20px] bg-muted px-3.5 py-3 sm:min-h-[96px] sm:px-4"
          >
            <div className="flex items-start justify-between gap-2">
              <SkeletonBar className="h-2.5 w-16" />
              <SkeletonBar className="h-6 w-6 rounded-full" />
            </div>
            <SkeletonBar className="mt-3 h-6 w-20" />
            <SkeletonBar className="mt-2 h-2.5 w-24" />
          </div>
        ))}
      </section>

      <section className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex items-center justify-between">
          <SkeletonBar className="h-3.5 w-24" />
          <SkeletonBar className="h-3 w-20" />
        </div>
        <SkeletonBar className="h-[260px] w-full rounded-2xl" />
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {Array.from({length: 2}, (_, i) => (
          <div key={i} className="rounded-[20px] bg-muted p-4">
            <div className="mb-3 flex items-center gap-2">
              <SkeletonBar className="h-4 w-4 rounded-full" />
              <SkeletonBar className="h-3.5 w-20" />
            </div>
            {Array.from({length: 4}, (_, r) => (
              <div key={r} className="flex items-center justify-between py-2.5">
                <SkeletonBar className="h-3 w-28" />
                <SkeletonBar className="h-3 w-12" />
              </div>
            ))}
          </div>
        ))}
      </section>

      <section className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <SkeletonBar className="h-3.5 w-24" />
          <SkeletonBar className="h-3 w-40" />
        </div>
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          {Array.from({length: 5}, (_, i) => (
            <SkeletonBar key={i} className="h-3 w-20" />
          ))}
        </div>
      </section>
    </>
  );
}

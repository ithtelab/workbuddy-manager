'use client';

import {useCallback, useEffect, useState} from 'react';
import {
  CalendarCheck,
  Globe,
  Cat,
  Filter,
  History,
  RefreshCw,
  Trash2,
  TriangleAlert,
} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useAsyncAll} from '@/lib/use-async-data';
import {notify} from '@/lib/toast';
import {RichText} from '@/lib/i18n/rich-text';
import {useI18n, useT} from '@/lib/i18n/provider';
import {checkinResultText, taskLogResultText} from '@/lib/tasklog-text';
import {accountApi, errText} from '@/lib/api';
import type {CheckinLog, TaskLog, TaskLogResponse} from '@/lib/types';
import {fmtDateTimeMarked, fmtNumber} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {TaskRunnerPanel} from '@/components/common/tasks/TaskRunnerPanel';
import {useAuth} from '@/lib/auth-context';
import {useRealm} from '@/lib/realm-context';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

/** 来源 → i18n 键（本端触发的签到 + 上游自动签到） */
const SOURCE_LABEL_KEYS: Record<string, string> = {
  manual: 'tasks.sourceManual',
  'manual-batch': 'tasks.sourceManualBatch',
  add: 'tasks.sourceAdd',
  auto: 'tasks.sourceAuto',
};

/**
 * 自动任务类型 → i18n 键。
 *
 * 后端 `/api/task-logs` 会返回一份 kinds 映射（id → 中文名），但那是**数据**
 * 不是文案：按 id 走本地字典，后端新增类型时回退显示后端给的名字。
 */
const KIND_LABEL_KEYS: Record<string, string> = {
  travel: 'tasks.kindTravel',
  activity: 'tasks.kindActivity',
  checkin: 'tasks.kindCheckin',
  keepalive: 'tasks.kindKeepalive',
  'user-resource': 'tasks.kindUserResource',
  credit: 'tasks.kindCredit',
  school: 'tasks.kindSchool',
  cat: 'tasks.kindCat',
  // 面板发起的一次执行（server/services/taskrun.py 写入，含 claim / full 两种模式）
  taskrun: 'tasks.kindTaskrun',
};

/**
 * 结果文案：三层接力（一键执行历史 → 积分流水 → 服务端模板 → 短语表兜底）。
 * 实现抽到 `lib/tasklog-text.ts`，好让 `dev/i18n-pipeline-check.mjs` 能真实
 * import 它逐语言验收 —— 这段接错只会安静地渲染出键名或原文，光看页面容易漏。
 */
/**
 * 单次拉取条数上限。
 *
 * 列表改为「固定高度 + 内部滚动」（与右侧「上游原始日志」一致），不再分页：
 * 滚动比翻页直观，也不会出现「一页没填满、下面留一大片空白」。
 * 上限用于控制 DOM 规模——记录只增不减，全量渲染会让页面越来越重；
 * 需要更早的记录时用时间范围筛选收窄。
 */
const CHECKIN_LIMIT = 200;
const TASK_LIMIT = 200;
/** 手机端用更小的上限：卡片行高约是表格行的两倍，滚动也更费屏幕 */
const TASK_LIMIT_MOBILE = 80;

/** 时间范围选项（与请求日志页保持一致的说法），label 为 i18n 键 */
const RANGES = [
  {value: '1', key: 'logs.last24h'},
  {value: '7', key: 'stats.last7'},
  {value: '30', key: 'stats.last30'},
  {value: '90', key: 'stats.last90'},
];

/** 任务日志的结果等级配色 */
const LEVEL_TONE: Record<string, string> = {
  credit: 'text-emerald-600 dark:text-emerald-400',
  ok: 'text-muted-foreground',
  info: 'text-muted-foreground',
  warn: 'text-amber-600 dark:text-amber-400',
  error: 'text-red-600 dark:text-red-400',
};

/**
 * 列表底部条：总数 + 命中上限时的说明。
 *
 * `totalUnknown` 为真时整条不渲染。这是「取不到 ≠ 确实没有」在**页脚**上的同一件事：
 * 总数是从响应里来的，取不到时它是 0，而「共 0 条」是在**断言**一个我们并不知道的
 * 事实——挂在「加载失败」提示下面，两句正好互相打脸。宁可什么都不说。
 *
 * 注意只在**一点数据都没有**时才传它：刷新失败时旧数据仍在屏幕上（见 use-async-data
 * 的合并策略），那时总数是上次的已知值，照常显示才对。
 */
function ListFooter({
  total,
  shown,
  truncated,
  totalUnknown = false,
}: {
  total: number;
  shown: number;
  truncated: boolean;
  totalUnknown?: boolean;
}) {
  const t = useT();
  if (totalUnknown) return null;
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-2 border-t border-border/40 px-4 py-2 text-[11px] text-muted-foreground">
      <span className="tabular-nums">{t('tasks.footerTotal', {n: fmtNumber(total)})}</span>
      {truncated && (
        <span className="text-amber-600 dark:text-amber-400">
          {t('tasks.footerTruncated', {n: fmtNumber(shown)})}
        </span>
      )}
    </div>
  );
}

/**
 * 取数完成前的空值。必须是模块级同一份：写成 `values.checkin?.items ?? []` 的话
 * 每次渲染都会新建数组 / 对象，进下游 useMemo 的依赖后每帧都变（同 dashboard 与
 * security 的处理）。
 */
const EMPTY_CHECKIN_LOGS: CheckinLog[] = [];
const EMPTY_TASK_LOGS: TaskLog[] = [];
const EMPTY_LINES: string[] = [];
const EMPTY_KINDS: Record<string, string> = {};

export default function TasksPage() {
  const {isAdmin} = useAuth();
  const {realm, label: realmName} = useRealm();
  const {t} = useI18n();

  /**
   * 筛选条件是**查询范围**，仍由本页持有；数据本身改由 useAsyncAll 管（见下方）。
   * `collectBusy` 只描述「立即采集」这一个按钮的在途状态，与取数无关。
   */
  const [checkinDays, setCheckinDays] = useState('7');
  const [taskFilter, setTaskFilter] = useState<string>('all');
  const [taskDays, setTaskDays] = useState('7');
  const [collectBusy, setCollectBusy] = useState(false);
  /**
   * 移动端把三块整合成一张卡片 + 分段切换。
   * 三块竖着堆叠时，各有表头与分页，页面又长又碎；
   * 桌面端横向空间够，仍三块并列。
   */
  const [mobileTab, setMobileTab] = useState<'checkin' | 'tasks' | 'raw'>('checkin');
  /** 手机端用更小的拉取上限（卡片行高约是表格行的两倍） */
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 767px)');
    const sync = () => setNarrow(mq.matches);
    sync();
    mq.addEventListener('change', sync);
    return () => mq.removeEventListener('change', sync);
  }, []);
  const taskLimit = narrow ? TASK_LIMIT_MOBILE : TASK_LIMIT;

  /**
   * 本页「正片」的两份数据：签到记录 + 自动任务记录。一次并发取回，成败逐项独立。
   *
   * 为什么必须换掉原先手写的 `Promise.allSettled` + 8 个 useState：那套写法把失败
   * **静默丢掉**了（`if (x.status === 'fulfilled')` 后面没有 else，只在两份**同时**
   * 失败时才弹一次几秒后消失的 toast），于是取数失败时界面照常渲染空态——
   * 「暂无签到记录」「暂无自动任务记录」。这两句话在数据还没取到时说出来，等于告诉
   * 用户「你没签过到、后台任务没跑过」，用户会去排查账号与采集器，方向完全错了。
   *
   * 依赖分两组，因为「换版本」与「换查询范围」的要求正好相反：
   *  · deps = [realm]：换了数据上下文 → 清空旧数据重取并显示骨架。留着旧数据会出现
   *    「标题写着国际版、列表还是国内版」；
   *  · refreshDeps = 四个查询范围：换了只重取、不清空。清空会让每次改时段都闪一次
   *    骨架，而改时段是高频操作。
   *
   * 已知取舍：两组列表共用这一个 hook，所以改签到时段会把任务列表也重取一次（原先
   * 一次 load 拉三份，同样是全拉）。想省这一次请求就得再拆一个 hook，代价是整页
   * 的加载 / 失败判据要手工合并——收益不值这个复杂度。
   */
  const {values, errors, isInitialLoading, isInitialFailed, isRefreshing, reload} = useAsyncAll({
    checkin: () => accountApi.checkinLogs(CHECKIN_LIMIT, 0, undefined, Number(checkinDays), realm),
    tasks: () =>
      accountApi.taskLogs(
        taskLimit,
        0,
        taskFilter === 'all' ? undefined : taskFilter,
        undefined,
        Number(taskDays),
        realm,
      ),
  }, [realm], [checkinDays, taskFilter, taskDays, taskLimit]);

  /**
   * 「上游原始日志」是**配件**面板，单独一个 hook。
   *
   * 它有意容忍失败：上游只在失败与旅行/活跃时打日志，容器重建即丢，「取不到」是常态。
   * 混在上面那一组里有两个后果：
   *  ① 它失败了会把「两份正片都成功」也拖进「部分失败」，而用户并不需要为此做什么；
   *  ② 更糟的是它若成功、两份正片全挂，`hasData` 会因为这份配件而有值——整页错误态
   *     就不再出现，用户看到的仍是「暂无签到记录」。
   * 与 stats 页把上游统计单独拆出来是同一条理由。
   *
   * 它也需要**自己的心跳**：原先它跟着主 load 一起每 30 秒重拉，拆开后若不管它，
   * 这块面板会停在进页面那一刻的内容上，之后再也不会更新。
   */
  const upstream = useAsyncAll({raw: () => accountApi.upstreamLogs(300)}, []);

  /** 两块列表都限条数：历史只增不减，全量渲染会让页面随记录数无限变长
   *  （实测 500 条任务日志 = 19 屏、9000 个 DOM 节点）。 */
  const checkinLogs: CheckinLog[] = values.checkin?.items ?? EMPTY_CHECKIN_LOGS;
  const checkinTotal = values.checkin?.total ?? 0;
  /** 其中本端触发的条数（清空按钮据此显示） */
  const checkinLocalTotal = values.checkin?.local_total ?? values.checkin?.total ?? 0;
  /** 其中上游自动签到的条数（清空按钮的说明据此措辞） */
  const checkinAutoTotal = values.checkin?.auto_total ?? 0;

  const taskLogs: TaskLog[] = values.tasks?.logs ?? EMPTY_TASK_LOGS;
  /** 当前筛选下的总条数（不是全局统计）——分页必须用它算页数 */
  const taskTotal = values.tasks?.total ?? 0;
  const taskStats: TaskLogResponse['stats'] | null = values.tasks?.stats ?? null;
  const kindLabels: Record<string, string> = values.tasks?.kinds ?? EMPTY_KINDS;

  const upstreamLines: string[] = upstream.values.raw?.lines ?? EMPTY_LINES;

  /**
   * 哪一块没取到。**必须与「确实没有记录」分开**：两者渲染出来是同一片空白，含义却
   * 正好相反。失败时这两块各自在面板内挂一条常驻的行内提示，而不是让「暂无签到记录」
   * 照常说出口。
   */
  const checkinFailed = 'checkin' in errors;
  const tasksFailed = 'tasks' in errors;

  // 两块列表都会随任务执行而变化，停留期间心跳刷新，避免一直看旧记录。
  // 走 reload（刷新模式）：只把新数据换上去，**不**重走首屏流程——否则骨架会每
  // 30 秒闪一次。
  useHeartbeat(reload, 30000);
  useHeartbeat(upstream.reload, 30000);

  /** 立即采集一次上游任务日志 */
  const collectTasks = useCallback(async () => {
    setCollectBusy(true);
    try {
      const r = await accountApi.collectTaskLogs();
      await reload();
      if (r.added > 0)
        notify.ok(t('tasks.collectNew'), t('tasks.collectNewDetail', {count: r.added, n: r.added}));
      else notify.info(t('tasks.collectNone'), t('tasks.collectNoneDetail'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setCollectBusy(false);
    }
  }, [reload]);

  function accountLabel(l: TaskLog) {
    const uid = l.uid || '';
    if (!uid) return '—';
    // 昵称由后端解析：上游 2026-09-12 起只打 uid 前 8 位，前端拿不到完整 uid
    return l.nickname || uid;
  }

  /** 签到来源显示名（后端给的是固定枚举 manual / auto / add…） */
  function sourceLabel(source: string) {
    const key = SOURCE_LABEL_KEYS[source];
    return key ? t(key) : source;
  }

  /** 自动任务类型显示名；未收录的类型回退用后端给的名字 */
  function kindLabel(kind: string, fallback?: string) {
    const key = KIND_LABEL_KEYS[kind];
    return key ? t(key) : (fallback ?? kind);
  }

  // 筛选与分页都在服务端完成，这里直接用返回的当前页
  const filteredTasks = taskLogs;
  // 命中上限说明还有更早的记录，界面上要讲清楚（避免以为是全部）
  const checkinTruncated = checkinTotal > checkinLogs.length;
  const taskTruncated = taskTotal > taskLogs.length;

  /**
   * 概览数字必须跟着筛选走。
   *
   * taskStats 是**未筛选**的区间汇总，而列表是按 kind 筛选的：筛选后若还用
   * 未筛选的数字，会出现「共 62 条」配一个空列表的矛盾显示（用户反馈的
   * 「余额查询 / 令牌保活 / 自动签到 点进去一片空白」就是这个）。
   * by_kind 里同时有 count 与 credits，正好给出该筛选下的准确值。
   */
  const activeKind = taskFilter !== 'all' ? taskStats?.by_kind?.[taskFilter] : undefined;
  const shownCount = taskFilter === 'all' ? (taskStats?.total ?? 0) : (activeKind?.count ?? 0);
  const shownCredits =
    taskFilter === 'all' ? (taskStats?.total_credits ?? 0) : (activeKind?.credits ?? 0);
  /** 区间内是否有任何记录——决定筛选栏是否渲染（不能按筛选结果判断，否则会消失） */
  const hasAnyTask = (taskStats?.total ?? 0) > 0;

  /** 页面头与「一键执行」面板都不依赖本页那两份数据，守卫前后渲染同一份，
   *  免得加载时先消失再出现（同一位置、同一类型，React 会保留实例）。 */
  const header = (
    <PageHeader
      title={t('tasks.title')}
      description={t('tasks.description', {realm: realmName})}
    />
  );
  // 成长任务一键执行（issue #19）。仅管理员：这些操作会对账号发起真实写请求，
  // 后端也以 require_admin 兜底。国际版无成长中心体系，故不显示。
  const runner = isAdmin && (realm ?? 'cn') === 'cn' ? <TaskRunnerPanel /> : null;

  /**
   * 首屏守卫：一份数据都没到（还在加载，或两份全失败）时**不渲染内容区**。
   *
   * 不这么做的后果就是本批要修的那句谎话：数据还没取到时内容区会照常渲染
   * 「暂无签到记录」「暂无自动任务记录」——用户会以为账号没签过到、采集器没跑，
   * 于是去排查账号与后台，方向完全错了。
   *
   * 判据是「有没有数据」而不是「请求在不在飞」（见 use-async-data 的
   * isInitialLoading）：本页每 30 秒心跳一次，按后者会让骨架每 30 秒闪一次。
   */
  if (isInitialFailed || isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {runner}
        {isInitialFailed ? <LoadError variant="page" onRetry={reload} /> : <TasksSkeleton />}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}
      {runner}

      {/* 移动端：三块整合成一张卡片，用分段切换，避免又长又碎 */}
      <div className="flex gap-1 rounded-full bg-muted p-1 md:hidden">
        {([
          ['checkin', t('tasks.tabCheckin')],
          ['tasks', t('tasks.tabTasks')],
          ['raw', t('tasks.tabRaw')],
        ] as const).map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setMobileTab(id)}
            className={
              'flex-1 rounded-full px-3 py-1.5 text-xs transition-colors ' +
              (mobileTab === id
                ? 'bg-background font-medium text-foreground shadow-sm'
                : 'text-muted-foreground')
            }
          >
            {label}
          </button>
        ))}
      </div>

      {/* 签到记录 + 上游原始日志 */}
      {/* 两块等高：右栏内容区 flex-1 撑满，避免卡片下方留大片空白 */}
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div
          className={
            'flex h-[calc(100dvh-200px)] min-h-[320px] flex-col overflow-hidden rounded-[20px] bg-muted ' +
            (mobileTab === 'checkin' ? '' : 'hidden ') +
            'md:h-[470px] md:flex'
          }
        >
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-x-2 gap-y-1.5 px-4 py-3">
            <div className="flex min-w-0 items-center gap-2 text-sm font-medium">
              <CalendarCheck className="h-4 w-4 shrink-0" />
              <span className="shrink-0">{t('tasks.checkinTitle')}</span>
              <span className="hidden truncate text-[11px] font-normal text-muted-foreground sm:inline">
                {t('tasks.checkinSubtitle')}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Select value={checkinDays} onValueChange={setCheckinDays}>
                <SelectTrigger className="h-7 w-[116px] rounded-full text-[11px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {RANGES.map((r) => (
                    <SelectItem key={r.value} value={r.value} className="text-xs">{t(r.key)}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            {/*
              清空只删本端记录（上游自动签到的留痕由「自动任务」侧管理），
              所以：
              - 按钮只在确实有本端记录时出现，否则点了没反应像是坏了
              - 文案写明范围，避免用户以为能把自动签到也清掉
            */}
            {isAdmin && checkinLocalTotal > 0 && (
              <ConfirmDialog
                title={t('tasks.clearCheckinTitle')}
                description={
                  checkinAutoTotal > 0
                    ? t('tasks.clearCheckinDescLocalAuto', {
                        local: fmtNumber(checkinLocalTotal),
                        auto: fmtNumber(checkinAutoTotal),
                      })
                    : t('tasks.clearCheckinDesc')
                }
                confirmText={t('common.clear')}
                destructive
                onConfirm={async () => {
                  await accountApi.clearCheckinLogs();
                  notify.ok(t('logs.cleared'));
                  await reload();
                }}
                trigger={
                  <Button variant="ghost" size="sm" className="h-7 rounded-full text-red-500">
                    <Trash2 className="h-3.5 w-3.5" />
                    {t('common.clear')}
                  </Button>
                }
              />
            )}
            </div>
          </div>

          {/* 固定高度 + 内部滚动：与右侧「上游原始日志」一致，页面高度不随记录增长 */}
          <div className="scroll-slim min-h-0 flex-1 overflow-auto">
          {/* 没取到 ≠ 没有记录：两者渲染出来是同一片空白，含义却正好相反。取不到时
              挂一条常驻提示（带重试），而不是让「暂无签到记录」照常说出口——那等于
              告诉用户「你没签过到」。已经有旧数据时这条也挂着，说明看到的是上次的结果。 */}
          {checkinFailed && (
            <div className="px-4 pt-3">
              <LoadError message={t('tasks.checkinLoadFailed')} onRetry={reload} />
            </div>
          )}
          {checkinLogs.length ? (
            <>
              {/* 手机端：卡片 */}
              <div className="space-y-1 px-3.5 pb-4 md:hidden">
                {checkinLogs.map((l) => (
                  <div key={l.id} className="rounded-xl bg-background/60 px-3 py-1.5">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-xs font-medium">{l.nickname || l.uid || '—'}</span>
                      <span
                        className={
                          'shrink-0 text-[11px] font-medium ' +
                          (l.success
                            ? 'text-emerald-600 dark:text-emerald-400'
                            : 'text-red-600 dark:text-red-400')
                        }
                        title={l.message}
                      >
                        {checkinResultText(l)}
                      </span>
                    </div>
                    <div className="mt-0.5 flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
                      <span className="truncate tabular-nums">{fmtDateTimeMarked(l.ts)}</span>
                      <span className="shrink-0">{sourceLabel(l.source)}</span>
                    </div>
                  </div>
                ))}
              </div>

              <div className="hidden md:block">
                <Table className="[&_td]:py-1 [&_th]:h-8">
                  <TableHeader>
                    <TableRow className="border-b border-border/60 hover:bg-transparent">
                      <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('logs.colTime')}</TableHead>
                      <TableHead className="text-[11px] text-muted-foreground">{t('tasks.colAccount')}</TableHead>
                      <TableHead className="text-[11px] text-muted-foreground">{t('tasks.colSource')}</TableHead>
                      <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('tasks.colResult')}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {checkinLogs.map((l) => (
                      <TableRow key={l.id} className="border-b border-border/40">
                        <TableCell className="pl-4 text-xs tabular-nums text-muted-foreground">
                          {fmtDateTimeMarked(l.ts)}
                        </TableCell>
                        <TableCell className="max-w-[120px] truncate text-xs">{l.nickname || l.uid || '—'}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {sourceLabel(l.source)}
                        </TableCell>
                        <TableCell className="pr-4">
                          <span
                            className={
                              'text-[11px] font-medium ' +
                              (l.success
                                ? 'text-emerald-600 dark:text-emerald-400'
                                : 'text-red-600 dark:text-red-400')
                            }
                            title={l.message}
                          >
                            {checkinResultText(l)}
                          </span>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </>
          ) : checkinFailed ? null : (
            <div className="px-4 py-10 text-center text-xs text-muted-foreground">
              {t('tasks.checkinEmpty')}
            </div>
          )}
          </div>

          <ListFooter
            total={checkinTotal}
            shown={checkinLogs.length}
            truncated={checkinTruncated}
            totalUnknown={checkinFailed && !checkinLogs.length}
          />
        </div>

        <div
          className={
            'flex h-[calc(100dvh-200px)] min-h-[320px] flex-col overflow-hidden rounded-[20px] bg-muted ' +
            (mobileTab === 'raw' ? '' : 'hidden ') +
            'md:h-[470px] md:flex'
          }
        >
          <div className="flex shrink-0 items-center justify-between px-4 py-3">
            <div className="flex items-center gap-2 text-sm font-medium">
              <History className="h-4 w-4" />
              {t('tasks.rawLogTitle')}
              <span className="hidden text-[11px] font-normal text-muted-foreground sm:inline">
                {t('tasks.rawLogSubtitle')}
              </span>
            </div>
            <span className="text-[11px] text-muted-foreground">{t('tasks.rawLogFrom')}</span>
          </div>
          {/* 首屏加载中先给骨架：这块面板的「空」文案是一段解释（「上游只在失败与
              旅行/活跃时打日志，所以这里没有记录不代表没执行」），数据还没到时说出来
              同样是没依据的。**失败不在这里提示**——上游取不到是常态，见上方
              `upstream` 那个 hook 的说明。 */}
          {upstream.isInitialLoading ? (
            <div className="min-h-0 flex-1 space-y-2 px-4 py-3">
              {Array.from({length: 10}, (_, i) => (
                <SkeletonBar key={i} className="h-3 w-full" />
              ))}
            </div>
          ) : upstreamLines.length ? (
            <div className="scroll-slim min-h-0 flex-1 overflow-auto px-4 pb-3">
              <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-5 text-muted-foreground">
                {upstreamLines.slice(-200).join('\n')}
              </pre>
            </div>
          ) : (
            <div className="flex min-h-0 flex-1 items-center justify-center px-4 py-6">
              <div className="text-center text-xs leading-5 text-muted-foreground">
                <TriangleAlert className="mx-auto mb-2 h-4 w-4 text-amber-500" />
                <RichText text={t('tasks.rawLogNote')} />
              </div>
            </div>
          )}
        </div>
      </section>

      {/* 自动任务与积分记录：上游把结果打在容器日志里且重建即丢，
          这里展示后台采集器落库后的长期留痕，便于核对积分收益 */}
      {/* 固定高度：内层 flex-1 才受约束（没有高度时它只会跟着内容一起变长） */}
      <section
        className={
          'flex h-[calc(100dvh-200px)] min-h-[320px] flex-col overflow-hidden rounded-[20px] bg-muted ' +
          (mobileTab === 'tasks' ? '' : 'hidden ') +
          'md:h-[560px] md:flex'
        }
      >
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Cat className="h-4 w-4" />
            {t('tasks.taskLogTitle')}
            <span className="hidden text-[11px] font-normal text-muted-foreground sm:inline">
              {t('tasks.taskLogSubtitle')}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Select value={taskDays} onValueChange={setTaskDays}>
              <SelectTrigger className="h-7 w-[116px] rounded-full text-[11px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {RANGES.map((r) => (
                  <SelectItem key={r.value} value={r.value} className="text-xs">
                    {t(r.key)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {shownCount > 0 && (
              <Badge variant="secondary" className="hidden shrink-0 rounded-full tabular-nums sm:inline-flex">
                {t('tasks.shownCount', {n: fmtNumber(shownCount)})}
                {shownCredits > 0 && (
                  <span className="ml-1 text-emerald-600 dark:text-emerald-400">
                    {t('tasks.creditsTotal', {n: fmtNumber(shownCredits)})}
                  </span>
                )}
              </Badge>
            )}
            <Button
              variant="outline"
              size="sm"
              className="h-7 rounded-full text-[11px]"
              disabled={collectBusy}
              onClick={collectTasks}
            >
              <RefreshCw className={collectBusy ? 'animate-spin' : ''} />
              {t('tasks.collectNow')}
            </Button>
            {isAdmin && taskTotal > 0 && (
              <ConfirmDialog
                title={t('tasks.clearTasksTitle')}
                description={t('tasks.clearTasksDesc')}
                confirmText={t('common.clear')}
                destructive
                onConfirm={async () => {
                  await accountApi.clearTaskLogs();
                  notify.ok(t('logs.cleared'));
                  await reload();
                }}
                trigger={
                  <Button variant="ghost" size="sm" className="h-7 rounded-full text-red-500">
                    <Trash2 className="h-3.5 w-3.5" />
                    {t('common.clear')}
                  </Button>
                }
              />
            )}
          </div>
        </div>

        {/* 筛选栏的渲染条件是「区间内有任何记录」而不是「当前筛选有记录」：
            否则点到一个没有记录的类型后，筛选栏会连同列表一起消失，
            用户再也切不回「全部」——这是被反馈的「点进去一片空白」的另一半原因。 */}
        {hasAnyTask && (
          <div className="flex shrink-0 flex-wrap items-center gap-1.5 px-4 pb-3">
            <Filter className="h-3 w-3 text-muted-foreground" />
            {[
              {id: 'all', label: t('common.all')},
              // 类型名走本地字典；后端新增的类型回退显示它给的名字
              ...Object.entries(kindLabels).map(([id, label]) => ({id, label: kindLabel(id, label)})),
            ].map((item) => {
              const active = taskFilter === item.id;
              // 每个类型都显示**自己**的条数（含 0）：这样点「余额查询」前就能
              // 看出它是空的，不会以为点进去该有内容。这正是原先让人踩空的地方。
              const n =
                item.id === 'all'
                  ? taskStats?.total
                  : taskStats?.by_kind?.[item.id]?.count;
              const isEmpty = item.id !== 'all' && !n;
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setTaskFilter(item.id)}
                  title={isEmpty ? t('tasks.filterEmptyTitle', {label: item.label}) : undefined}
                  className={
                    'rounded-full px-2.5 py-1 text-[11px] transition-colors ' +
                    (active
                      ? 'bg-foreground text-background'
                      : isEmpty
                        ? 'bg-background/40 text-muted-foreground/50 hover:text-muted-foreground'
                        : 'bg-background/60 text-muted-foreground hover:text-foreground')
                  }
                >
                  {item.label}
                  {n !== undefined && <span className="ml-1 tabular-nums">{n}</span>}
                </button>
              );
            })}
          </div>
        )}

        {/* 没取到 ≠ 没有记录。失败时先挂一条常驻提示，而不是落进下面那几个空态分支
            ——它们全都在断言「这里没有记录」，而事实是不知道有没有。
            这一条是本页最要紧的：自动任务记录正是用户核对积分收益的依据，
            把它说成「暂无」会让人以为后台采集器坏了。 */}
        {tasksFailed && (
          <div className="px-4 pb-3">
            <LoadError message={t('tasks.taskLogLoadFailed')} onRetry={reload} />
          </div>
        )}

        {/*
          空态分两种，不能混为一谈：
          - hasAnyTask：区间内有数据，只是当前筛选为空 → 提示换个类型
          - 否则：这个区间真的没记录 → 引导去采集
          若只看 taskLogs.length，筛选到空时会落到「全局无记录」的文案上，
          明明别的类型有记录却告诉用户「暂无记录」，既误导又像数据丢了。
        */}
        {taskLogs.length ? (
          <div className="scroll-slim min-h-0 flex-1 overflow-auto pb-4">
            {/* 手机端：卡片式；桌面：表格 */}
            <div className="space-y-1.5 px-3.5 md:hidden">
              {filteredTasks.map((l) => (
                <div key={l.id} className="rounded-xl bg-background/60 px-3 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-medium">{kindLabel(l.kind, kindLabels[l.kind])}</span>
                    <span className="text-xs font-semibold tabular-nums">
                      {l.credits > 0 ? (
                        <span className="text-emerald-600 dark:text-emerald-400">+{l.credits}</span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </span>
                  </div>
                  <div
                    className={'mt-1 break-words text-[11px] leading-4 ' + (LEVEL_TONE[l.level] || 'text-muted-foreground')}
                    title={l.message}
                  >
                    {taskLogResultText(l)}
                  </div>
                  <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
                    <span className="truncate" title={l.uid}>
                      {accountLabel(l)}
                    </span>
                    <span className="shrink-0 tabular-nums">{fmtDateTimeMarked(l.ts)}</span>
                  </div>
                </div>
              ))}
            </div>

            <div className="hidden md:block">
              <Table>
                <TableHeader>
                  <TableRow className="border-b border-border/60 hover:bg-transparent">
                    <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('logs.colTime')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('tasks.colKind')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('tasks.colAccount')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('tasks.colResult')}</TableHead>
                    <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">{t('tasks.colCredits')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredTasks.map((l) => (
                    <TableRow key={l.id} className="border-b border-border/40">
                      <TableCell className="pl-4 text-xs tabular-nums text-muted-foreground">
                        {fmtDateTimeMarked(l.ts)}
                      </TableCell>
                      <TableCell className="text-xs">{kindLabel(l.kind, kindLabels[l.kind])}</TableCell>
                      <TableCell className="max-w-[180px] truncate text-xs" title={l.uid}>
                        {accountLabel(l)}
                      </TableCell>
                      <TableCell
                        className={`max-w-[520px] truncate text-xs ${LEVEL_TONE[l.level] || ''}`}
                        title={l.message}
                      >
                        {taskLogResultText(l)}
                      </TableCell>
                      <TableCell className="pr-4 text-right text-xs font-medium tabular-nums">
                        {l.credits > 0 ? (
                          <span className="text-emerald-600 dark:text-emerald-400">+{l.credits}</span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </div>
        ) : tasksFailed ? null : hasAnyTask ? (
          <div className="px-4 py-10 text-center text-xs leading-5 text-muted-foreground">
            <Filter className="mx-auto mb-2 h-4 w-4" />
            {t('tasks.filterNoRecords', {label: kindLabel(taskFilter, kindLabels[taskFilter])})}
            <br />
            {t('tasks.filterHint')}
          </div>
        ) : realm === 'global' ? (
          /* 国际版没有任务体系：这不是「还没采集到」，而是上游根本不跑这些任务。
             写字说明白，否则用户会以为是采集器坏了。 */
          <div className="px-4 py-10 text-center text-xs leading-5 text-muted-foreground">
            <Globe className="mx-auto mb-2 h-4 w-4 text-sky-500/70" />
            <RichText text={t('tasks.globalNoTasks')} />
            <br />
            <RichText text={t('tasks.globalKeepalive')} />
          </div>
        ) : (
          <div className="px-4 py-10 text-center text-xs leading-5 text-muted-foreground">
            <Cat className="mx-auto mb-2 h-4 w-4" />
            {t('tasks.noTaskRecords')}
            <br />
            {t('tasks.noTaskRecordsHint')}
          </div>
        )}

        <ListFooter
          total={taskFilter === 'all' ? (taskStats?.total ?? taskTotal) : (activeKind?.count ?? taskTotal)}
          shown={taskLogs.length}
          truncated={taskTruncated}
          totalUnknown={tasksFailed && !taskLogs.length}
        />
      </section>
    </div>
  );
}

/**
 * 首屏骨架。结构与真实内容**逐块对应**（两张等高卡片 / 一张宽卡片），而不是一坨
 * 居中的转圈：数据到位时版面不会整体跳一下。
 *
 * 本页每 30 秒心跳刷新一次，但只有「一份都没取到」才会走到这里
 * （见 use-async-data 的 isInitialLoading），所以不会一闪一闪。
 *
 * 卡片高度取的是桌面端的固定值（`md:h-[470px]` / `md:h-[560px]`）——手机端真实
 * 卡片是 `calc(100dvh-200px)`，用近似高度即可，骨架只出现一瞬。
 */
function TasksSkeleton() {
  return (
    <>
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {[0, 1].map((i) => (
          <div key={i} className="flex h-[320px] flex-col rounded-[20px] bg-muted p-4 md:h-[470px]">
            <SkeletonBar className="mb-4 h-3.5 w-24" />
            <div className="space-y-2.5">
              {Array.from({length: 6}, (_, j) => (
                <SkeletonBar key={j} className="h-3 w-full" />
              ))}
            </div>
          </div>
        ))}
      </section>

      <section className="flex h-[320px] flex-col rounded-[20px] bg-muted p-4 md:h-[560px]">
        <SkeletonBar className="mb-4 h-3.5 w-32" />
        <div className="space-y-2.5">
          {Array.from({length: 9}, (_, j) => (
            <SkeletonBar key={j} className="h-3 w-full" />
          ))}
        </div>
      </section>
    </>
  );
}

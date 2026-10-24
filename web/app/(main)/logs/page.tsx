'use client';

import {useCallback, useRef, useState} from 'react';
import {ScrollText, RotateCcw, Trash2, ChevronLeft, ChevronRight} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
import {keyApi, logApi} from '@/lib/api';
import {useAsyncAll} from '@/lib/use-async-data';
import {useDebounced} from '@/lib/use-debounce';
import type {ApiKey, RequestLog} from '@/lib/types';
import {fmtCredit, fmtDateTime, fmtDateTimeMarked, fmtLatency, fmtNumber} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {useAuth} from '@/lib/auth-context';
import {useRealm} from '@/lib/realm-context';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

const PAGE_SIZE = 20;

/**
 * 默认时间范围（天）。
 *
 * 提成常量是因为有两个地方要用同一个值：「重置筛选」要把天数改回来，还要判断
 * 「用户有没有改过筛选」（决定那个按钮能不能点）。两处各写一个 `'7'` 迟早会不一致。
 */
const DEFAULT_DAYS = '7';

/**
 * 空数组的稳定引用。
 *
 * 必须是模块级常量，不能每次渲染现写一个 `[]`：它们是「还没取到」时的兜底值，
 * 每帧新建的数组会让下游任何依赖它的 `useMemo` / `useEffect` 每帧重算。
 */
const EMPTY_LOGS: RequestLog[] = [];
const EMPTY_KEYS: ApiKey[] = [];

/**
 * 本次请求的缓存命中率（issue #69）。
 *
 * 口径取 **hit / (hit + miss)** —— 与上游自己 `/v1/stats` 的 cache_hit_rate 一致
 * （两者同源：腾讯在末帧 usage 里给的三个字段）。不拿 `prompt_tokens` 当分母：
 * 它含工具定义等不一定参与缓存的部分，算出来的比例会比实际低，用户会以为缓存没生效。
 *
 * 分母为 0（只有 hit 没有 miss，或两者都 0）时返回 100% —— 命中数大于 0 才调这里。
 */
function cachePct(l: RequestLog): string {
  const hit = l.cache_hit_tokens ?? 0;
  const miss = l.cache_miss_tokens ?? 0;
  if (hit + miss <= 0) return '100';
  return String(Math.round((hit / (hit + miss)) * 100));
}

/**
 * 账号列（issue #69）：上游日志里的形状是 `昵称(uid8)`。
 *
 * 拆开显示是为了两件事都好办：昵称给人看（「这几次都落在张叔叔那个号上」），
 * uid 用于和账号页/上游日志对齐（重名的账号靠它区分）。
 * 对不上这个形状时原样显示——上游改格式我们也不该丢信息。
 */
function AccountCell({account}: {account: string | null}) {
  if (!account) {
    return <span className="text-muted-foreground/50">—</span>;
  }
  const m = account.match(/^(.*?)\(([^()]*)\)$/);
  return (
    <>
      <span>{m ? m[1] : account}</span>
      {m && m[2] && (
        <span className="ml-1 font-mono text-[11px] text-muted-foreground">{m[2]}</span>
      )}
    </>
  );
}

export default function LogsPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  // 日志随顶部版本切换：两个版本走不同账号池，混看会把两个池子的调用搅在一起
  const {realm} = useRealm();
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<RequestLog | null>(null);

  const [keyId, setKeyId] = useState('all');
  const [status, setStatus] = useState('all');
  const [days, setDays] = useState(DEFAULT_DAYS);
  /** 文本筛选的**输入值**：绑在输入框上，逐字符更新（打字本身不能卡） */
  const [model, setModel] = useState('');
  const [ip, setIp] = useState('');

  /** 筛选一变就回第 1 页：停在第 5 页上看新筛选的结果，多半是一张空表 */
  const backToFirstPage = useCallback(() => setPage(1), []);

  /**
   * 文本筛选的**落定值**（防抖 400ms）。取数与依赖数组都用它，**不用输入值**——
   * 否则输入 `kimi` 会连发 4 次请求（`k` / `ki` / `kim` / `kimi`）。
   *
   * 页码复位挂在 `onSettle` 上，而不是另写一个 `useEffect(() => setPage(1), [modelQ])`：
   * 落定值与页码必须落在**同一次更新**里，否则会先按「新筛选 + 旧页码」查一次、
   * 再回第 1 页查第二次。理由见 lib/use-debounce.ts 的注释。
   */
  const [modelQ, applyModel] = useDebounced(model, 400, backToFirstPage);
  const [ipQ, applyIp] = useDebounced(ip, 400, backToFirstPage);

  /**
   * 切版本时回到第 1 页。
   *
   * 为什么必须重置：两个版本的日志条数不同，停在「第 5 页」再切版本会去查另一个
   * 版本的第 5 页——那边可能只有 1 页，于是看到空白表格和「第 5 页 / 共 1 页」
   * 这种自相矛盾的页码。其余筛选项（天数 / 密钥 / 状态）都是这么做的，版本不该例外。
   *
   * 写法说明：用「渲染期纠正 state」而不是 `useEffect` + `setPage`。
   * 后者会先用旧页码发一次请求、再重置页码发第二次（白白多查一次，且第一份
   * 结果可能短暂显示出来）。在渲染期直接 setState，React 会在本次渲染结束前
   * 立刻用新 state 重渲染，下面的取数只跑一次、拿到的就是第 1 页。
   */
  const lastRealm = useRef(realm);
  if (lastRealm.current !== realm) {
    lastRealm.current = realm;
    if (page !== 1) setPage(1);
  }

  /**
   * 日志列表。依赖分两组，因为**翻页和切版本的要求正好相反**
   * （判据与理由都在 lib/async-state.ts 的 `depMode` 里）：
   *
   *  · `realm` 变了 = 换了一个数据上下文 → **清空重取**。旧版本的日志留在屏幕上、
   *    而标题已经写着另一个版本，比空着更误导。切版本也必须**立即**重取，
   *    不能只等 60 秒心跳——用户点一下没反应会以为功能坏了（实测反馈）。
   *  · 其余（页码 / 天数 / 密钥 / 状态 / 模型 / IP）= 只是换了个查询范围 → **保留现有
   *    内容静默重取**。翻页是高频操作，若也清空，每翻一页都会闪一次骨架，
   *    看起来像页面在抽搐。
   *
   * **六个筛选项全部进 `refreshDeps`**（这是本次修掉的自相矛盾）：原先只有
   * `page` / `days` 在依赖里，另外四个只在点「查询」时生效，于是同一排控件有两种
   * 脾气——天数改完立刻变，密钥改完要再点一下。而「在第 1 页点筛选没反应」那个
   * 更早的坑（`setPage(1)` 在页码本来就是 1 时是空操作）当时是靠显式补一次
   * `reloadLogs()` 绕过去的；现在筛选项本身就是依赖，改哪个都会重取，绕法连同
   * 那个「查询」按钮一起不需要了。
   *
   * 文本类（模型 / IP）进依赖的是**防抖落定值**，不是输入值——见上方 `useDebounced`。
   */
  const {
    values,
    errors: logErrors,
    isInitialLoading,
    isInitialFailed,
    isRefreshing,
    reload: reloadLogs,
  } = useAsyncAll(
    {
      logs: () =>
        logApi.list({
          page,
          size: PAGE_SIZE,
          key_id: keyId === 'all' ? undefined : keyId,
          model: modelQ || undefined,
          status: status === 'all' ? undefined : status,
          ip: ipQ || undefined,
          days: Number(days) || undefined,
          realm,
        }),
    },
    [realm],
    [page, days, keyId, status, modelQ, ipQ],
  );

  /**
   * 筛选下拉用的密钥列表，**单独一个 hook**。
   *
   * 它只喂那个下拉，既不随翻页也不随天数变化；若并进上面那组，每次翻页都会
   * 白打一个请求。它的失败按「部分失败」处理——下拉取不到，不该把整页日志顶掉。
   */
  const {
    values: keyValues,
    errors: keyErrors,
    reload: reloadKeys,
  } = useAsyncAll({keys: () => keyApi.list()}, []);

  const logs: RequestLog[] = values.logs?.items ?? EMPTY_LOGS;
  const total = values.logs?.total ?? 0;
  const keys: ApiKey[] = keyValues.keys ?? EMPTY_KEYS;

  const failedCount = Object.keys(logErrors).length + Object.keys(keyErrors).length;

  /** 重试：两组一起重来，避免「点了重试、密钥下拉还是空的」 */
  const retry = useCallback(() => {
    reloadLogs();
    reloadKeys();
  }, [reloadLogs, reloadKeys]);

  // 新请求会不断写入日志；心跳刷新只更新当前筛选下的列表，不会重置筛选条件
  useHeartbeat(reloadLogs, 60000);

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  /**
   * 回到第 1 页并重取。
   *
   * 给「清空日志」用：清完之后要看的是**第 1 页**，而不是刚才停留的那一页。
   * 页码本来就在第 1 页时 `setPage(1)` 是空操作、依赖没变、不会触发重取，
   * 所以得显式补一次 `reloadLogs()`；页码真的变了就交给依赖去重取，否则会连打两次。
   */
  function refetchFromFirstPage() {
    if (page === 1) reloadLogs();
    else setPage(1);
  }

  /** 有没有筛选项不是默认值（决定「重置」按钮能不能点） */
  const isFiltered =
    keyId !== 'all' || status !== 'all' || days !== DEFAULT_DAYS || model !== '' || ip !== '';

  /**
   * 一键重置全部筛选。
   *
   * 文本类必须走 `applyXxx('')`（立即落定）而不是只 `setModel('')`：后者要等 400ms
   * 防抖走完才真正生效，中间会先按「旧的文本筛选 + 新的下拉筛选」查一次，列表闪两下。
   * 这里所有 setState 都在同一个事件处理器里，React 合批成一次渲染 → 只重取一次。
   */
  function resetFilters() {
    setKeyId('all');
    setStatus('all');
    setDays(DEFAULT_DAYS);
    setModel('');
    setIp('');
    applyModel('');
    applyIp('');
    setPage(1);
  }

  const header = (
    <PageHeader
      title={t('logs.title')}
      description={t('logs.description')}
      actions={
        <>
          {isAdmin && (
            <ConfirmDialog
              title={t('logs.clearTitle')}
              description={t('logs.clearDesc')}
              confirmText={t('common.clear')}
              destructive
              onConfirm={async () => {
                await logApi.clear();
                notify.ok(t('logs.cleared'));
                refetchFromFirstPage();
              }}
              trigger={
                <Button variant="outline" size="sm" className="rounded-full text-red-500">
                  <Trash2 />
                  {t('common.clear')}
                </Button>
              }
            />
          )}
        </>
      }
    />
  );

  /**
   * 首屏还没拿到日志：先给骨架，别先渲染「第 1 页 / 共 1 页」和「暂无日志」——
   * 那两句话在数据还在路上时都是错的。整页错误态同理：一份都没取到时，
   * 让用户看到「数据加载失败 + 重试」，而不是一张空表和一句「暂无日志」。
   */
  if (isInitialFailed || isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? (
          <LoadError variant="page" onRetry={retry} />
        ) : (
          <LogsSkeleton />
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* 部分失败：已经显示出来的日志仍然是对的，只是可能不是最新的。
          用一条常驻提示说明，**不把表格顶掉**；下一次刷新成功后自动消失。 */}
      {failedCount > 0 && <LoadError message={t('state.partialFailed')} onRetry={retry} />}

      <section className="rounded-[20px] bg-muted p-4">
        <div className="grid grid-cols-2 items-end gap-3 md:grid-cols-6">
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('logs.filterRange')}</Label>
            <Select value={days} onValueChange={(v) => { setDays(v); setPage(1); }}>
              <SelectTrigger className="bg-background" aria-label={t('logs.filterRange')}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="1">{t('logs.last24h')}</SelectItem>
                <SelectItem value="7">{t('stats.last7')}</SelectItem>
                <SelectItem value="30">{t('stats.last30')}</SelectItem>
                <SelectItem value="90">{t('stats.last90')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('logs.filterKey')}</Label>
            <Select value={keyId} onValueChange={(v) => { setKeyId(v); setPage(1); }}>
              <SelectTrigger className="bg-background" aria-label={t('logs.filterKey')}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('logs.allKeys')}</SelectItem>
                {keys.map((k) => (
                  <SelectItem key={k.id} value={String(k.id)}>{k.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('accounts.colStatus')}</Label>
            <Select value={status} onValueChange={(v) => { setStatus(v); setPage(1); }}>
              <SelectTrigger className="bg-background" aria-label={t('accounts.colStatus')}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('common.all')}</SelectItem>
                <SelectItem value="ok">{t('common.success')}</SelectItem>
                <SelectItem value="error">{t('common.failure')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('nav.models')}</Label>
            {/* 文本类防抖 400ms 后自动生效，没有「查询」按钮要按 */}
            <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder={t('common.all')} className="bg-background" />
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('logs.filterIp')}</Label>
            <Input value={ip} onChange={(e) => setIp(e.target.value)} placeholder={t('common.all')} className="bg-background" />
          </div>
          {/* 原来这里是「查询」按钮。六个筛选项现在都是改完即生效，那个按钮点了
              只会把同一份查询再打一次，所以换成「重置」——它才是有用的那个动作。
              没有筛选时置灰而不是隐藏：隐藏会让这一格塌掉、整排控件左右跳。 */}
          <Button
            variant="outline"
            className="rounded-full"
            disabled={!isFiltered}
            onClick={resetFilters}
          >
            <RotateCcw />
            {t('logs.reset')}
          </Button>
        </div>
      </section>

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('logs.colTime')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('nav.keys')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">IP</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.colAccount')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('nav.models')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.colEffort')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('accounts.colStatus')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.colFirstToken')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.colLatency')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">Token</TableHead>
              <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('metric.paid')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {logs.map((l) => (
              <TableRow
                key={l.id}
                className="cursor-pointer border-b border-border/40"
                onClick={() => setDetail(l)}
              >
                <TableCell className="pl-4 text-xs text-muted-foreground">{fmtDateTimeMarked(l.ts)}</TableCell>
                <TableCell className="text-xs">{l.key_name || '—'}</TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">{l.ip}</TableCell>
                {/* 本次用的上游账号（issue #69）。账号由上游选、不在响应里回传，
                    这一列是采集它的容器日志后按时间对回来的——比请求晚几秒，
                    所以刚打完的请求可能还是「—」，稍后刷新就有。 */}
                <TableCell className="text-xs">
                  <AccountCell account={l.account} />
                </TableCell>
                <TableCell className="text-xs">
                  {l.model || '—'}
                  {l.mapped_model && l.mapped_model !== l.model && (
                    <span className="ml-1 text-[11px] text-muted-foreground">→{l.mapped_model}</span>
                  )}
                </TableCell>
                {/*
                  思考强度：客户端显式传的 reasoning_effort（low/medium/high/max…）。
                  没传就显示「—」而不是猜默认档——同一模型不同版本的默认档可能不同，
                  写死会把「客户端没传」误读成「客户端要了某个档位」。
                */}
                <TableCell className="text-xs">
                  {l.effort ? (
                    <Badge variant="secondary" className="rounded-full font-mono text-[10px]">
                      {l.effort}
                    </Badge>
                  ) : (
                    <span className="text-muted-foreground/50">—</span>
                  )}
                </TableCell>
                <TableCell>
                  {l.status >= 200 && l.status < 300 ? (
                    <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">{l.status}</Badge>
                  ) : l.status >= 400 && l.status < 500 ? (
                    <Badge variant="secondary" className="rounded-full bg-amber-500/12 text-amber-600 dark:text-amber-400">
                      {l.status || 'ERR'}
                    </Badge>
                  ) : (
                    <Badge variant="destructive" className="rounded-full">{l.status || 'ERR'}</Badge>
                  )}
                </TableCell>
                {/* 首字延迟：反映「上游多久开始回话」。回答越长总耗时越大，
                    所以判断上游快慢只看这一列。非流式请求没有中间过程，显示 —。 */}
                <TableCell
                  className={
                    'text-xs tabular-nums ' +
                    (l.first_token_ms != null && l.first_token_ms >= 3000
                      ? 'font-medium text-amber-600 dark:text-amber-400'
                      : 'text-muted-foreground')
                  }
                >
                  {l.first_token_ms != null ? (
                    fmtLatency(l.first_token_ms)
                  ) : (
                    <span className="text-muted-foreground/50">—</span>
                  )}
                </TableCell>
                <TableCell className="text-xs tabular-nums text-muted-foreground">
                  {fmtLatency(l.latency_ms)}
                </TableCell>
                <TableCell className="text-xs tabular-nums">
                  {l.prompt_tokens + l.completion_tokens > 0 ? (
                    fmtNumber(l.prompt_tokens + l.completion_tokens)
                  ) : (
                    <span className="text-muted-foreground/70">—</span>
                  )}
                  {l.stream && <span className="ml-1 text-[11px] text-muted-foreground">{t('logs.streamShort')}</span>}
                  {/* 提示词缓存（issue #69）：用户靠这条判断「缓存到底有没有起作用」。
                      上游没返回这三个字段时整块不显示（而不是显示 0%）——
                      与「确实没命中」不是一回事。 */}
                  {typeof l.cache_hit_tokens === 'number' ? (
                    <span
                      className={
                        'ml-1 text-[10px] ' +
                        (l.cache_hit_tokens > 0
                          ? 'text-emerald-600 dark:text-emerald-400'
                          : 'text-amber-600 dark:text-amber-400')
                      }
                      title={t('logs.cacheTitle', {
                        hit: fmtNumber(l.cache_hit_tokens),
                        miss: fmtNumber(l.cache_miss_tokens ?? 0),
                      })}
                    >
                      {l.cache_hit_tokens > 0
                        ? t('logs.cacheShort', {pct: cachePct(l)})
                        : t('logs.cacheMissShort')}
                    </span>
                  ) : null}
                </TableCell>
                <TableCell className="pr-4 text-xs tabular-nums">
                  {typeof l.credit === 'number' ? (
                    <span className={l.credit > 0 ? 'text-amber-600 dark:text-amber-400' : ''}>
                      {fmtCredit(l.credit)}
                    </span>
                  ) : (
                    <span className="text-muted-foreground/70" title={t('logs.noCreditTitle')}>—</span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        {/* 走到这里说明首屏已经就绪——还在取数时上面已经整块返回骨架了。
            所以「没有日志」是真的没有，不需要再拿「请求在不在飞」去兜一层
            （原先那个 `!loading` 判据正是这个意思，现在由骨架分支统一承担）。 */}
        {!logs.length && (
          <EmptyState
            icon={ScrollText}
            title={t('logs.emptyTitle')}
            description={t('logs.emptyDesc')}
            className="flex flex-col items-center justify-center py-16 text-center"
          />
        )}

        <div className="flex items-center justify-between px-4 py-3">
          <div className="text-[11px] text-muted-foreground">
            {t('logs.pageInfo', {total: fmtNumber(total), page, pages})}
          </div>
          <div className="flex gap-1">
            <Button
              variant="outline"
              size="icon"
              className="h-7 w-7 rounded-md"
              aria-label={t('common.previousPage')}
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="outline"
              size="icon"
              className="h-7 w-7 rounded-md"
              aria-label={t('common.nextPage')}
              disabled={page >= pages}
              onClick={() => setPage((p) => Math.min(pages, p + 1))}
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      </section>

      <Drawer open={!!detail} onOpenChange={(v) => !v && setDetail(null)}>
        <DrawerContent>
          <DrawerHeader>
            <DrawerTitle>{t('logs.detailTitle', {id: detail?.id ?? ''})}</DrawerTitle>
            <DrawerDescription>{detail ? fmtDateTime(detail.ts) : ''}</DrawerDescription>
          </DrawerHeader>
          {detail && (
            <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 pb-8 text-xs">
              {([
                ['ip', t('logs.rowIp'), detail.ip],
                ['key', t('logs.rowKey'), detail.key_name || '—'],
                // 账号来自上游日志回填，可能比请求本身晚几秒；这里只能说
                // “尚未采集”，不能写成“非流式请求”——那是在猜原因。
                ['account', t('logs.rowAccount'), detail.account || t('logs.accountPending')],
                ['model', t('logs.rowModel'), detail.model || '—'],
                ['mapped', t('logs.rowMappedModel'), detail.mapped_model || '—'],
                ['status', t('logs.rowStatus'), String(detail.status)],
                [
                  'firstToken',
                  t('logs.rowFirstToken'),
                  detail.first_token_ms != null
                    ? fmtLatency(detail.first_token_ms)
                    : t('logs.notCollected'),
                ],
                ['latency', t('logs.rowLatency'), fmtLatency(detail.latency_ms)],
                ['promptTokens', 'Prompt Token', fmtNumber(detail.prompt_tokens)],
                ['completionTokens', 'Completion Token', fmtNumber(detail.completion_tokens)],
                // 提示词缓存三段（issue #69）。null 显示「未采集」而不是 0：
                // 老上游不返回这三个字段，显示 0 会让人以为缓存从未生效。
                [
                  'cacheHit',
                  t('logs.rowCacheHit'),
                  detail.cache_hit_tokens != null
                    ? fmtNumber(detail.cache_hit_tokens)
                    : t('logs.notCollected'),
                ],
                [
                  'cacheMiss',
                  t('logs.rowCacheMiss'),
                  detail.cache_miss_tokens != null
                    ? fmtNumber(detail.cache_miss_tokens)
                    : t('logs.notCollected'),
                ],
                [
                  'cacheWrite',
                  t('logs.rowCacheWrite'),
                  detail.cache_write_tokens != null
                    ? fmtNumber(detail.cache_write_tokens)
                    : t('logs.notCollected'),
                ],
                [
                  'credit',
                  t('logs.rowCredit'),
                  typeof detail.credit === 'number'
                    ? fmtCredit(detail.credit) + (detail.credit > 0 ? '' : t('logs.unbilled'))
                    : t('logs.noCredit'),
                ],
                ['stream', t('logs.rowStream'), detail.stream ? t('common.yes') : t('common.no')],
                ['ua', 'User-Agent', detail.ua || '—'],
                ['error', t('logs.rowError'), detail.error || '—'],
              ] as [string, string, string][]).map(([id, k, v]) => {
                // 这些字段内容较长且常需要贴出来（排查 / 反馈），给出复制入口
                const copyable = ['ip', 'ua', 'error'].includes(id) && v !== '—';
                return (
                  <div key={id} className="flex items-start gap-3">
                    <div className="w-32 shrink-0 text-muted-foreground">{k}</div>
                    <div className="min-w-0 flex-1 break-all font-mono">{v}</div>
                    {copyable && <CopyButton value={v} title={t('logs.copyField', {field: k})} className="-mt-1" />}
                  </div>
                );
              })}
            </div>
          )}
        </DrawerContent>
      </Drawer>
    </div>
  );
}

/**
 * 首屏骨架。
 *
 * 分两块对着真实版面：筛选区（一行六个控件）与表格区（表头 + 8 行 + 页码条）。
 * 尺寸都照着真实控件给（输入框 36px、行高约 37px），数据到位时版面不跳——
 * 骨架比真实内容矮一截的话，加载完会整页往下窜一下，比没有骨架更难受。
 */
function LogsSkeleton() {
  return (
    <>
      <FilterSkeleton />
      <LogsTableSkeleton />
    </>
  );
}

/** 筛选区骨架：五个「标签 + 控件」加一个「重置」按钮，正好填满六列网格 */
function FilterSkeleton() {
  return (
    <section className="rounded-[20px] bg-muted p-4">
      <div className="grid grid-cols-2 items-end gap-3 md:grid-cols-6">
        {Array.from({length: 5}, (_, i) => (
          <div key={i} className="space-y-1.5">
            <SkeletonBar className="h-2.5 w-12" />
            <SkeletonBar className="h-9 w-full rounded-md" />
          </div>
        ))}
        <SkeletonBar className="h-9 w-full rounded-full" />
      </div>
    </section>
  );
}

/** 表格骨架：表头一行 + 8 行数据 + 底部页码条，列宽大致对着真实各列 */
function LogsTableSkeleton() {
  const headWidths = ['w-20', 'w-12', 'w-16', 'w-16', 'w-14', 'w-10', 'w-12', 'w-12', 'w-14', 'w-10'];
  const cellWidths = ['w-24', 'w-14', 'w-20', 'w-20', 'w-16', 'w-10', 'w-12', 'w-12', 'w-16', 'w-12'];
  return (
    <section className="overflow-hidden rounded-[20px] bg-muted">
      <div className="flex items-center gap-4 border-b border-border/60 px-4 py-2.5">
        {headWidths.map((w, i) => (
          <SkeletonBar key={i} className={`h-2.5 ${w}`} />
        ))}
      </div>
      {Array.from({length: 8}, (_, r) => (
        <div key={r} className="flex items-center gap-4 border-b border-border/40 px-4 py-2.5">
          {cellWidths.map((w, i) => (
            <SkeletonBar key={i} className={`h-3 ${w}`} />
          ))}
        </div>
      ))}
      <div className="flex items-center justify-between px-4 py-3">
        <SkeletonBar className="h-3 w-28" />
        <div className="flex gap-1">
          <SkeletonBar className="h-7 w-7 rounded-md" />
          <SkeletonBar className="h-7 w-7 rounded-md" />
        </div>
      </div>
    </section>
  );
}

'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  Boxes,
  Brain,
  Gauge,
  Layers,
  Loader2,
  Maximize2,
  RefreshCw,
  Search,
} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {PageSectionTabs} from '@/components/common/layout/PageSectionTabs';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {StatCard} from '@/components/common/layout/StatCard';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {modelApi} from '@/lib/api';
import {useAsyncAll} from '@/lib/use-async-data';
import {useRealm} from '@/lib/realm-context';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import type {CatalogModel, ModelCatalog} from '@/lib/types';

/** 上下文窗口显示：131072 → 128K；1048576 → 1M；0 → — */
function fmtCtx(n: number): string {
  if (!n || n <= 0) return '—';
  if (n >= 1024 * 1024) {
    const m = n / (1024 * 1024);
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
  }
  if (n >= 1024) return `${Math.round(n / 1024)}K`;
  return String(n);
}

/**
 * 系列标签配色（按系列名稳定取色，认不出的用中性色）。
 * 键是后端 modelcatalog 推导出的系列名，属固定的闭集。
 */
const SERIES_STYLE: Record<string, string> = {
  '智谱 GLM': 'border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-400',
  DeepSeek: 'border-indigo-500/30 bg-indigo-500/10 text-indigo-700 dark:text-indigo-400',
  Kimi: 'border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-400',
  MiniMax: 'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-400',
  腾讯混元: 'border-cyan-500/30 bg-cyan-500/10 text-cyan-700 dark:text-cyan-400',
  自动选择: 'border-border bg-muted text-muted-foreground',
};

function SeriesBadge({series}: {series: string}) {
  const t = useT();
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center rounded-md border px-1.5 py-0.5 text-[10px] font-medium',
        SERIES_STYLE[series] ?? 'border-border bg-muted text-muted-foreground',
      )}
    >
      {seriesLabel(series, t)}
    </span>
  );
}

/**
 * 系列名本地化。
 *
 * 后端按模型 id 前缀推导出固定的几个系列名（智谱 GLM / 腾讯混元 / 自动选择 /
 * 其他），它们是数据不是文案，所以在这里做「数据值 → 译文」的映射；
 * 未收录的系列（DeepSeek、Kimi 等本来就是英文品牌名）原样显示。
 */
const SERIES_KEYS: Record<string, string> = {
  '智谱 GLM': 'models.seriesGlm',
  腾讯混元: 'models.seriesHunyuan',
  自动选择: 'models.seriesAuto',
  其他: 'models.seriesOther',
};

function seriesLabel(series: string, t: (key: string) => string): string {
  const key = SERIES_KEYS[series];
  return key ? t(key) : series;
}

/**
 * 模型数据来源的显示名。
 *
 * 后端同时给 `source`（稳定枚举 tencent / upstream / none）和 `source_label`
 * （中文说明）。界面按枚举取译文，取不到再退回后端给的说明——这样数据源文案
 * 能跟着界面语言走，将来后端加新来源也只是回退成中文而不是空白。
 */
const SOURCE_KEYS: Record<string, string> = {
  tencent: 'models.sourceTencent',
  upstream: 'models.sourceUpstream',
  none: 'models.sourceNone',
};

function sourceLabel(source: string, fallback: string, t: (key: string) => string): string {
  const key = SOURCE_KEYS[source];
  return key ? t(key) : fallback;
}

/** 一张统计卡（统一的 StatCard）：模型页只关心数值与副文案。 */

export default function ModelsPage() {
  const t = useT();
  const {realm, label: realmName} = useRealm();

  const [q, setQ] = useState('');
  const [series, setSeries] = useState('all');
  /** 能力筛选：全部 / 支持推理 / 大上下文 / 多模态 */
  const [cap, setCap] = useState<'all' | 'reasoning' | 'large' | 'vision'>('all');
  /**
   * 排序：默认按原顺序；「积分倍率从低到高」用于挑省积分的模型。
   * 这是积分倍率最有用的用法——单看一行的倍率没概念，排一下才知道哪个最省。
   */
  const [sort, setSort] = useState<'default' | 'credits'>('default');

  /**
   * 「强制刷新」= 绕过**后端**缓存再拉一次（`modelApi.catalog` 的第二个参数）。
   *
   * 用 ref 而不是 state：它描述的是**下一次请求**要不要绕缓存，不参与渲染，也不该
   * 触发重渲染；放 state 里还容易被误放进依赖数组，造成「一改就重新取数」。
   */
  const forceNext = useRef(false);

  /**
   * 模型目录 = 这一页的**主数据**，接统一状态系统。
   *
   * `realm` 进 `deps`：两个版本的模型清单不同（后端也按版本分开缓存），换版本就是
   * 换数据集，必须清空重取并显示骨架——留着上一版的模型列表，标题已经写着新版本，
   * 列表却还是旧版本的东西。
   *
   * 原实现把「加载中」和「失败」压进两个字符串 state（`loading` / `error`），
   * 失败时渲染的是 `EmptyState`（没有「重试」），加载中是一行纯文本——两者长得
   * 都很像空态，用户分不清「在取」「取不到」「确实没有」。
   */
  const {values, isInitialLoading, isInitialFailed, isRefreshing, reload} = useAsyncAll({
    catalog: () => modelApi.catalog(realm, forceNext.current),
  }, [realm]);

  const data: ModelCatalog | null = values.catalog ?? null;

  /**
   * 切版本时清掉筛选状态，避免「上一版的系列筛选把新版过滤成空」。
   *
   * 这一步**不能**省：系列是按版本各自的清单来的，上一版选中的系列在新版里可能
   * 一个模型都没有，于是页面看起来像「新版没有模型」——而数据其实是好的。
   *
   * 依赖从原来的 `[load]` 改成 `[realm]`：取数交给 `useAsyncAll` 之后 `load` 不存在了，
   * 而这里要的本来就是「版本变了才清」，直接用 `realm` 更贴合意图。
   */
  useEffect(() => {
    setSeries('all');
    setCap('all');
    setQ('');
  }, [realm]);

  /**
   * 强制刷新：绕过服务端缓存重新拉一次（与首屏那次区分开，只有它才绕缓存）。
   *
   * **返回本次是否取到**，调用方据此提示成功还是失败。只能看返回值，不能写
   * `.catch()`：`reload()` 内部已经把异常收进 `errors` 并返回布尔值，返回的
   * Promise 永不 reject，靠 catch 判断失败的分支根本进不去——用户会永远看到
   * 「正在拉取」，无论取到没取到。（同「密钥」页建完密钥后刷新的写法。）
   */
  const refetch = useCallback(async (): Promise<boolean> => {
    forceNext.current = true;
    try {
      return await reload();
    } finally {
      forceNext.current = false;
    }
  }, [reload]);

  const models = data?.models ?? [];

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    const list = models.filter((m) => {
      if (series !== 'all' && m.series !== series) return false;
      if (cap === 'reasoning' && m.efforts.length === 0) return false;
      if (cap === 'large' && (m.context_length || 0) < 131072) return false;
      if (cap === 'vision' && m.native_modality !== 'multimodal') return false;
      if (!kw) return true;
      return (
        m.id.toLowerCase().includes(kw) ||
        (m.name || '').toLowerCase().includes(kw) ||
        m.series.toLowerCase().includes(kw)
      );
    });
    if (sort === 'credits') {
      // 倍率从低到高（越省越靠前）。**没有倍率的排在最后**——不是 0，
      // 不能当成「免费」混进最前面。
      const num = (v?: string) => {
        const m = /x?\s*([0-9]+(?:\.[0-9]+)?)/i.exec(v || '');
        return m ? Number(m[1]) : null;
      };
      return [...list].sort((a, b) => {
        const na = num(a.credits);
        const nb = num(b.credits);
        if (na === null && nb === null) return 0;
        if (na === null) return 1;
        if (nb === null) return -1;
        return na - nb;
      });
    }
    return list;
  }, [models, q, series, cap, sort]);

  const summary = data?.summary;
  const seriesOptions = summary?.series ?? [];

  const header = (
    <>
      <PageHeader
        title={t('models.title')}
        description={t('models.description', {realm: realmName})}
        actions={
          <Button
            variant="outline"
            size="sm"
            className="rounded-full"
            disabled={isRefreshing}
            title={t('models.refetchTitle')}
            onClick={async () => {
              // 「进行中」那条提示在拿到结果后**换掉**，而不是留在屏幕上：原先只弹
              // 这条、不看结果，无论成功失败都停在「正在重新拉取模型列表…」，用户
              // 分不清是还在拉、拉到了、还是根本没拉到。
              const pending = notify.info(t('models.refetching'));
              const done = await refetch();
              notify.dismiss(pending);
              if (done) {
                notify.ok(t('models.refetchDone'), t('models.refetchDoneHint'));
              } else {
                notify.err(t('models.refetchFailed'), t('models.refetchFailedHint'));
              }
            }}
          >
            {isRefreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            {t('models.refetch')}
          </Button>
        }
      />
      {/* 二级导航（批次 4 ②：本页吸收了「聊天测试台」——看模型 → 想试 →
          试完回来看价格，是同一个来回动作）。放进 `header` 而不是两个 return
          分支里各写一遍：下面的首屏守卫是**早返回**，漏一处就会出现
          「取不到目录时 Tab 不见了」。 */}
      <PageSectionTabs />
    </>
  );

  /**
   * 首屏守卫：目录还没到（或根本没取到）时不渲染列表与统计卡。
   *
   * 必须**排在**「暂无模型」之前——否则取数失败时那句空态会照常出现，等于告诉
   * 用户「腾讯那边没有可用模型」，而事实是没取到。判据用 `isInitialLoading` /
   * `isInitialFailed`（按「有没有数据」判定），不用「请求在不在飞」：后者会让
   * 「强制刷新」把整页闪成骨架。
   *
   * 只有目录这一份数据，所以可以直接早返回；统计卡与筛选条都派生自 `data`，
   * 数据没到时本来也渲染不出来。
   */
  if (isInitialFailed || isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? <LoadError variant="page" onRetry={reload} /> : <ModelsSkeleton />}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* 来源说明：如实标注，不把回退数据说成实时数据 */}
      {data && (
        <div
          className={cn(
            'flex flex-wrap items-center gap-x-2 gap-y-1 rounded-[16px] border px-3.5 py-2.5 text-[11px]',
            data.source === 'tencent'
              ? 'border-border bg-muted/60 text-muted-foreground'
              : 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400',
          )}
        >
          <span className="font-medium">
            {t('models.source', {label: sourceLabel(data.source, data.source_label, t)})}
          </span>
          {data.via && <span>{t('models.sourceVia', {via: data.via})}</span>}
          {data.cached && <span>{t('models.sourceCached', {n: data.cache_age})}</span>}
          {data.source !== 'tencent' && (
            <span className="basis-full text-[10px] leading-4 opacity-90">
              {data.source === 'upstream'
              ? t('models.fallbackNote')
                : t('models.emptyNote')}
            </span>
          )}
        </div>
      )}

      {/* 统计卡 */}
      {summary && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard icon={Boxes} label={t('models.available')} value={String(summary.total)} />
          <StatCard
            icon={Brain}
            label={t('models.reasoning')}
            value={String(summary.reasoning)}
            hint={t('models.reasoningHint')}
          />
          <StatCard
            icon={Maximize2}
            label={t('models.largeContext')}
            value={String(summary.large_context)}
            hint="≥128K"
          />
          <StatCard
            icon={Gauge}
            label={t('models.maxContext')}
            value={fmtCtx(summary.max_context)}
            hint={
              summary.series.length
                ? t('models.seriesCount', {count: summary.series.length, n: summary.series.length})
                : undefined
            }
          />
        </div>
      )}

      {/* 搜索与筛选 */}
      <section className="rounded-[20px] bg-muted p-3.5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t('models.searchPlaceholder')}
              className="h-9 bg-background pl-8"
            />
          </div>
          <div className="-mx-0.5 flex flex-wrap items-center gap-1.5 overflow-x-auto px-0.5 pb-0.5">
            <span className="flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground">
              <Layers className="h-3.5 w-3.5" />
              {t('models.series')}
            </span>
            <Button
              variant={series === 'all' ? 'default' : 'outline'}
              size="sm"
              className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
              onClick={() => setSeries('all')}
            >
              {t('common.all')}
            </Button>
            {seriesOptions.map((s) => (
              <Button
                key={s}
                variant={series === s ? 'default' : 'outline'}
                size="sm"
                className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
                onClick={() => setSeries(s)}
              >
                {seriesLabel(s, t)}
              </Button>
            ))}
            <span className="ml-1 h-4 w-px shrink-0 bg-border" />
            {(
              [
                ['all', t('common.all')],
                ['reasoning', t('models.reasoning')],
                ['large', t('models.largeContext')],
                ['vision', t('models.vision')],
              ] as const
            ).map(([k, label]) => (
              <Button
                key={k}
                variant={cap === k ? 'default' : 'outline'}
                size="sm"
                className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
                onClick={() => setCap(k)}
              >
                {label}
              </Button>
            ))}
            {/* 排序：挑省积分模型时最实用的一项 —— 单看倍率没概念，排一下才清楚 */}
            <span className="ml-1 h-4 w-px shrink-0 bg-border" />
            <Button
              variant={sort === 'credits' ? 'default' : 'outline'}
              size="sm"
              className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
              title={t('models.sortByRatioTitle')}
              onClick={() => setSort((v) => (v === 'credits' ? 'default' : 'credits'))}
            >
              {t('models.sortByRatio')}
            </Button>
          </div>
        </div>
      </section>

      {/* 列表。走到这里 `data` 一定已经取到了（首屏守卫已在上方返回）。 */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        {models.length === 0 ? (
          <EmptyState
            icon={Boxes}
            title={t('models.noModels')}
            description={t('models.noModelsDesc')}
          />
        ) : filtered.length === 0 ? (
          <EmptyState icon={Search} title={t('models.noMatch')} description={t('models.noMatchDesc')} />
        ) : (
          <>
            <div className="overflow-x-auto">
              <Table className="wb-table-left">
                <TableHeader>
                  <TableRow className="border-b border-border/60 hover:bg-transparent">
                    <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('models.colModel')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('models.colContext')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('models.colMaxOutput')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('models.colEfforts')}</TableHead>
                    <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('models.series')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((m: CatalogModel) => (
                    <TableRow key={m.id} className="border-b border-border/40">
                      <TableCell className="pl-4">
                        {/* 模型描述（腾讯的 descriptionZh）挂在名称上做悬浮提示：
                            它通常是一两句话，铺在表格里会把行高撑开 */}
                        <div className="flex flex-col gap-0.5 py-0.5" title={m.description || undefined}>
                          {m.name ? (
                            <>
                              <span className="text-xs font-medium">{m.name}</span>
                              <span className="font-mono text-[11px] text-muted-foreground">{m.id}</span>
                            </>
                          ) : (
                            <span className="font-mono text-xs font-medium">{m.id}</span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="text-xs tabular-nums text-muted-foreground">
                        {fmtCtx(m.context_length)}
                      </TableCell>
                      <TableCell className="text-xs tabular-nums text-muted-foreground">
                        {fmtCtx(m.max_output_tokens)}
                      </TableCell>
                      <TableCell>
                        {m.efforts.length ? (
                          <div className="flex flex-wrap items-center gap-1">
                            {m.efforts.map((e) => (
                              <Badge key={e} variant="secondary" className="rounded-md font-mono text-[10px]">
                                {e}
                              </Badge>
                            ))}
                            {/* 默认档位单独标出来：上游 thinking 决策用它，
                                用户据此知道不指定档位时会走哪一档 */}
                            {m.default_effort && (
                              <span
                                className="text-[11px] text-muted-foreground"
                                title={t('models.defaultEffortTitle', {effort: m.default_effort})}
                              >
                                {t('models.defaultEffort', {effort: m.default_effort})}
                              </span>
                            )}
                          </div>
                        ) : (
                          <span className="text-[11px] text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell className="pr-4">
                        <div className="flex flex-wrap items-center justify-end gap-1.5">
                          {/* 积分倍率：同一 prompt 在不同模型上的扣费倍率，
                              挑「省积分」的模型时最有用的一项。上游把它拼进
                              description 前缀，我们单独展示（更清楚） */}
                          {m.credits && (
                            <Badge
                              variant="secondary"
                              className="rounded-md font-mono text-[10px]"
                              title={t('models.creditRatioTitle')}
                            >
                              {m.credits}
                            </Badge>
                          )}
                          {(
                            <Badge variant="secondary" className="rounded-md text-[10px]" title={t('models.visionTitle') + ' · ' + (m.native_modality_source || '') + ' ' + (m.native_modality_verified_at || '')}>
                              {t(m.native_modality === 'multimodal' ? 'models.vision' : m.native_modality === 'text' ? 'models.visionNo' : m.native_modality === 'router' ? 'models.visionRouter' : 'models.visionUnknown')}
                            </Badge>
                          )}
                          {m.only_reasoning && (
                            <Badge
                              variant="secondary"
                              className="rounded-md text-[10px]"
                              title={t('models.onlyReasoningTitle')}
                            >
                              {t('models.onlyReasoning')}
                            </Badge>
                          )}
                          {m.is_default && (
                            <Badge variant="secondary" className="rounded-md text-[10px]" title={t('models.defaultTitle')}>
                              {t('models.default')}
                            </Badge>
                          )}
                          <SeriesBadge series={m.series} />
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            {(filtered.length !== models.length || q.trim() || series !== 'all' || cap !== 'all') && (
              <div className="border-t border-border/40 px-4 py-2 text-[11px] text-muted-foreground">
                {t('models.filteredCount', {n: filtered.length, total: models.length})}
              </div>
            )}
          </>
        )}
      </section>

      {/* 失败原因：默认收起，供排查（例如某个账号凭证过期） */}
      {!!data?.errors?.length && (
        <details className="rounded-[16px] bg-muted/60 px-3.5 py-2.5 text-[11px] text-muted-foreground">
          <summary className="cursor-pointer select-none">
            {t('models.failureLog', {n: data.errors.length})}
          </summary>
          <ul className="mt-2 space-y-1 font-mono text-[10px] leading-4">
            {data.errors.map((e, i) => (
              <li key={i} className="break-all">· {e}</li>
            ))}
          </ul>
        </details>
      )}

      <p className="text-[11px] leading-4 text-muted-foreground">
        {t('models.footnote')}
      </p>
    </div>
  );
}

/**
 * 首屏骨架：与真实版面逐块对应——来源说明条 + 四张统计卡 + 筛选条 + 模型表。
 *
 * 换掉原来的「转圈 + 正在读取模型列表…」：转圈只说明「在忙」，说不出「忙完这里
 * 会出现什么」；而且它与空态的位置、尺寸差得远，数据到达时整页会跳一下。骨架把
 * 版面形状先摆出来，数据到位时内容填进原位。
 *
 * 行数固定 4（统计卡固定 4 张，与真实一致）：真实行数未知，固定值只为给出形状。
 */
function ModelsSkeleton() {
  return (
    <>
      <SkeletonBar className="h-9 w-full rounded-[16px]" />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {Array.from({length: 4}, (_, i) => (
          <div key={i} className="rounded-[20px] bg-muted p-3.5">
            <SkeletonBar className="h-3 w-16" />
            <SkeletonBar className="mt-2.5 h-5 w-20" />
          </div>
        ))}
      </div>

      <section className="rounded-[20px] bg-muted px-3.5 py-3">
        <div className="flex flex-wrap gap-2">
          {Array.from({length: 5}, (_, i) => (
            <SkeletonBar key={i} className="h-7 w-20 rounded-full" />
          ))}
        </div>
      </section>

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex items-center gap-4 border-b border-border/60 px-4 py-3">
          {Array.from({length: 5}, (_, i) => (
            <SkeletonBar key={i} className="h-3 w-16" />
          ))}
        </div>
        <div className="divide-y divide-border/40">
          {Array.from({length: 6}, (_, i) => (
            <div key={i} className="flex items-center gap-4 px-4 py-3.5">
              <SkeletonBar className="h-3.5 w-36 max-w-[35%]" />
              <SkeletonBar className="h-3.5 w-14" />
              <SkeletonBar className="hidden h-3.5 w-14 sm:block" />
              <SkeletonBar className="hidden h-3.5 w-24 md:block" />
              <SkeletonBar className="hidden h-3.5 w-16 lg:block" />
            </div>
          ))}
        </div>
      </section>
    </>
  );
}

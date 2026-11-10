'use client';

import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Download,
  GitCompare,
  Radar,
  RefreshCw,
  Trash2,
} from 'lucide-react';
import {useAuth} from '@/lib/auth-context';
import {errText, tapApi} from '@/lib/api';
import {fmtDateTime, fmtMoney, fmtNumber} from '@/lib/format';
import {useAsyncAll} from '@/lib/use-async-data';
import {useDebounced} from '@/lib/use-debounce';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {
  clientLabel,
  clientVersion,
  diffIsEmpty,
  fmtBytes,
  fmtMs,
  prevTurn,
  sessionTitle,
  statusTone,
  summaryText,
} from '@/lib/tap';
import type {TapDiff, TapSessionDetail, TapTurn, TapTurnBrief} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {cn} from '@/lib/utils';

const PAGE_SIZE = 20;

/** 详情面板的四个页签。差异不是常驻请求：只在切到它时才算。 */
type Tab = 'overview' | 'request' | 'response' | 'diff';

/** 稳定空数组：每次渲染现写 `[]` 会让下游依赖它的 memo 每帧重算（同日志页）。 */
const EMPTY_TURNS: TapTurnBrief[] = [];

/**
 * 抓包回放页（Trace Tap）。
 *
 * 与 claude-tap 的对应关系：
 *  · 左侧会话列表 = 一次 agent 运行（run）；
 *  · 中间轮次条 = 该运行里的每一次请求；
 *  · 右侧详情 = 这一轮发出的上下文与模型回的内容；
 *  · 「和上一轮比」= claude-tap 的 diff 视图。
 *
 * 为什么不做成抽屉而是「左列表 + 右详情」：对比是这页的主要动作，抽屉会把
 * 右侧内容盖住，每次切轮次都要开关一次。两栏常驻时，点两下就能来回读。
 */
export default function TracesPage() {
  const t = useT();
  const {me} = useAuth();
  const isAdmin = me?.role === 'admin';

  const [days, setDays] = useState('7');
  const [client, setClient] = useState('all');
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState('');
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [turnId, setTurnId] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>('overview');
  /**
   * 视角：同一轮调用有两份报文。
   *   · client —— 客户端（下游）手里那份：请求是它原样发的，响应是它真正收到的
   *     （Anthropic / Responses 已经过网关翻译）；
   *   · gateway —— 网关（上游）那一跳：请求是改写后发出去的，响应是上游原文。
   * 默认看客户端视角：用户手里的报错、客户端里显示的回复，说的都是这一份。
   */
  const [view, setView] = useState<'client' | 'gateway'>('client');
  const [turn, setTurn] = useState<TapTurn | null>(null);
  const [diff, setDiff] = useState<TapDiff | null>(null);
  const [busy, setBusy] = useState(false);

  const [settledQuery] = useDebounced(query, 350, () => setPage(1));

  const list = useAsyncAll(
    {
      cfg: () => tapApi.config(),
      sessions: () => tapApi.sessions({
        page, size: PAGE_SIZE, days, client, q: settledQuery || undefined,
      }),
    },
    [],
    [page, client, settledQuery, days],
  );

  const cfg = list.values.cfg;
  const sessions = list.values.sessions;
  const enabled = cfg?.config.enabled ?? false;

  const detail = useAsyncAll(
    {session: async (): Promise<TapSessionDetail | null> =>
      sessionId ? tapApi.session(sessionId) : null},
    [sessionId],
  );
  const session = detail.values.session ?? null;

  /** 列表刷新后把选中会话修正到一个仍然存在的条目上。 */
  useEffect(() => {
    const items = sessions?.items ?? [];
    if (!items.length) {
      if (sessionId !== null) setSessionId(null);
      return;
    }
    if (!sessionId || !items.some((s) => s.id === sessionId)) {
      setSessionId(items[0].id);
    }
  }, [sessions, sessionId]);

  /** 会话切换后默认看**最后**一轮：人多数时候关心「刚刚发生了什么」。 */
  useEffect(() => {
    const items = session?.items ?? EMPTY_TURNS;
    if (!items.length) {
      setTurnId(null);
      return;
    }
    if (!turnId || !items.some((x) => x.id === turnId)) {
      setTurnId(items[items.length - 1].id);
    }
  }, [session, turnId]);

  /** 单轮正文按需取：一次只取一条，会话列表不会被几百 KB 的正文拖住。 */
  useEffect(() => {
    if (!turnId) {
      setTurn(null);
      return;
    }
    let alive = true;
    setTurn(null);
    tapApi.turn(turnId)
      .then((data) => {
        if (alive) setTurn(data);
      })
      .catch((e) => {
        if (alive) notify.err(errText(e));
      });
    return () => {
      alive = false;
    };
  }, [turnId]);

  /** 差异同样是按需的：切到 diff 页签才请求，且需要存在上一轮。 */
  const before = useMemo(
    () => (turn && session ? prevTurn(session.items, turn.seq) : null),
    [turn, session],
  );

  useEffect(() => {
    if (tab !== 'diff' || !turn || !before) {
      setDiff(null);
      return;
    }
    let alive = true;
    tapApi.diff(before.id, turn.id)
      .then((d) => {
        if (alive) setDiff(d);
      })
      .catch((e) => {
        if (alive) notify.err(errText(e));
      });
    return () => {
      alive = false;
    };
  }, [tab, turn, before]);

  const toggle = useCallback(async (next: boolean) => {
    setBusy(true);
    try {
      await tapApi.saveConfig({enabled: next});
      notify.ok(t('trace.saved'));
      await list.reload();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }, [list, t]);

  const total = sessions?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const items = session?.items ?? EMPTY_TURNS;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('trace.title')}
        description={t('trace.description')}
        stacked
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-2 rounded-full border px-3 py-1">
              <Switch
                id="tap-enabled"
                checked={enabled}
                disabled={busy || !isAdmin}
                onCheckedChange={toggle}
              />
              <Label htmlFor="tap-enabled" className="text-xs">
                {enabled ? t('trace.on') : t('trace.off')}
              </Label>
            </div>
            <Button variant="outline" size="sm" className="rounded-full"
                    onClick={() => void list.reload()}>
              <RefreshCw
                className={cn('h-3.5 w-3.5', list.isRefreshing && 'animate-spin')}
              />
              {t('common.refresh')}
            </Button>
            {enabled && cfg && (
              <ConfirmDialog
                title={t('trace.clearTitle')}
                description={t('trace.clearDesc')}
                confirmText={t('common.clear')}
                destructive
                onConfirm={async () => {
                  await tapApi.clear();
                  notify.ok(t('trace.cleared'));
                  setSessionId(null);
                  setTurnId(null);
                  await list.reload();
                }}
                trigger={
                  <Button variant="outline" size="sm"
                          className="rounded-full text-red-500">
                    <Trash2 className="h-3.5 w-3.5" />
                    {t('common.clear')}
                  </Button>
                }
              />
            )}
          </div>
        }
      />

      {enabled && cfg && (
        <p className="text-xs text-muted-foreground">
          {t('trace.stats', {
            sessions: fmtNumber(cfg.stats.sessions),
            turns: fmtNumber(cfg.stats.turns),
            pt: fmtNumber(cfg.stats.prompt_tokens),
            ct: fmtNumber(cfg.stats.completion_tokens),
            days: cfg.config.retention_days,
            size: fmtBytes(cfg.config.max_body),
          })}
        </p>
      )}

      {list.isInitialLoading && (
        <div className="space-y-2">
          <SkeletonBar className="h-10 w-full" />
          <SkeletonBar className="h-40 w-full" />
        </div>
      )}

      {list.isInitialFailed && (
        <LoadError variant="page" onRetry={() => void list.reload()} />
      )}

      {!list.isInitialLoading && !enabled && (
        <EmptyState
          icon={Radar}
          title={t('trace.enableTitle')}
          description={t('trace.enableDesc')}
        >
          {isAdmin ? (
            <Button size="sm" className="rounded-full" disabled={busy}
                    onClick={() => void toggle(true)}>
              <Radar className="h-3.5 w-3.5" />
              {t('trace.enable')}
            </Button>
          ) : (
            <p className="text-xs text-muted-foreground">{t('trace.needAdmin')}</p>
          )}
        </EmptyState>
      )}

      {!list.isInitialLoading && enabled && (
        <div className="grid min-h-[60vh] gap-4 lg:grid-cols-[320px_1fr]">
          {/* ── 会话列表 ── */}
          <div className="flex min-h-0 flex-col gap-3 rounded-xl border bg-muted/20 p-3">
            <div className="flex flex-col gap-2">
              <Select value={days} onValueChange={(v) => { setDays(v); setPage(1); }}>
                <SelectTrigger className="h-8 text-xs" aria-label={t('trace.filterDays')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1">{t('trace.range1')}</SelectItem>
                  <SelectItem value="7">{t('trace.range7')}</SelectItem>
                  <SelectItem value="30">{t('trace.range30')}</SelectItem>
                </SelectContent>
              </Select>
              <Select value={client} onValueChange={(v) => { setClient(v); setPage(1); }}>
                <SelectTrigger className="h-8 text-xs" aria-label={t('trace.filterClient')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t('trace.allClients')}</SelectItem>
                  <SelectItem value="claude">Claude Code</SelectItem>
                  <SelectItem value="codex">Codex</SelectItem>
                  <SelectItem value="cursor">Cursor</SelectItem>
                  <SelectItem value="cli">CLI</SelectItem>
                  <SelectItem value="other">{t('trace.otherClients')}</SelectItem>
                </SelectContent>
              </Select>
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t('trace.search')}
                className="h-8 text-xs"
              />
            </div>

            <div className="min-h-0 flex-1 space-y-1.5 overflow-auto">
              {(sessions?.items ?? []).length === 0 && (
                <p className="px-1 py-6 text-center text-xs text-muted-foreground">
                  {t('trace.emptyList')}
                </p>
              )}
              {(sessions?.items ?? []).map((s) => (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => {
                    setSessionId(s.id);
                    setTurnId(null);
                    setTab('overview');
                  }}
                  className={cn(
                    'w-full rounded-lg border px-2.5 py-2 text-left transition-colors',
                    s.id === sessionId
                      ? 'border-primary/40 bg-primary/10'
                      : 'border-transparent hover:bg-muted',
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-xs font-medium">
                      {sessionTitle(s)}
                    </span>
                    <Badge variant="outline" className="shrink-0 text-[10px]">
                      {clientLabel(s.client)}
                    </Badge>
                  </div>
                  <div className="mt-1 flex items-center justify-between text-[11px] text-muted-foreground">
                    <span>{fmtDateTime(s.updated_at)}</span>
                    <span>
                      {t('trace.tokensShort', {
                        pt: fmtNumber(s.prompt_tokens),
                        ct: fmtNumber(s.completion_tokens),
                      })}
                    </span>
                  </div>
                </button>
              ))}
            </div>

            <div className="flex items-center justify-between text-[11px] text-muted-foreground">
              <Button
                variant="ghost" size="icon" className="h-6 w-6"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                <ChevronLeft className="h-3.5 w-3.5" />
              </Button>
              <span>{t('trace.pageInfo', {total, page, pages})}</span>
              <Button
                variant="ghost" size="icon" className="h-6 w-6"
                disabled={page >= pages}
                onClick={() => setPage((p) => Math.min(pages, p + 1))}
              >
                <ChevronRight className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>

          {/* ── 会话详情 ── */}
          <div className="flex min-h-0 flex-col gap-3 rounded-xl border p-3">
            {!session && (
              <EmptyState
                icon={ArrowLeft}
                title={t('trace.pickSessionTitle')}
                description={t('trace.pickSessionDesc')}
              />
            )}

            {session && (
              <>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      {sessionTitle(session)}
                    </p>
                    <p className="mt-0.5 text-[11px] text-muted-foreground">
                      {t('trace.sessionMeta', {
                        client: clientLabel(session.client)
                          + (clientVersion(session.client_ua) ? ` ${clientVersion(session.client_ua)}` : ''),
                        protocol: session.protocol,
                        key: session.key_name || '—',
                        time: fmtDateTime(session.started_at),
                      })}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline" size="sm" className="rounded-full"
                      onClick={() => window.open(tapApi.exportUrl(session.id), '_blank')}
                    >
                      <Download className="h-3.5 w-3.5" />
                      {t('trace.export')}
                    </Button>
                    {isAdmin && (
                      <ConfirmDialog
                        title={t('trace.deleteTitle')}
                        description={t('trace.deleteDesc')}
                        confirmText={t('common.delete')}
                        destructive
                        onConfirm={async () => {
                          if (!session.id) return;
                          await tapApi.remove(session.id);
                          setSessionId(null);
                          setTurnId(null);
                          await list.reload();
                        }}
                        trigger={
                          <Button variant="outline" size="sm"
                                  className="rounded-full text-red-500">
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        }
                      />
                    )}
                  </div>
                </div>

                {/* 时间概览：把每一轮的真实开始时刻与耗时投影到一条时间轴上。
                    对照 DSH 运行轨迹视图的做法 —— 先给「这一跑有多长、卡在哪一段」，
                    再让人点进去看单轮。只有一轮时没有轨迹可言，直接省掉。 */}
                <Timeline turns={items} selected={turnId} onSelect={(id) => {
                  setTurnId(id);
                  setTab('overview');
                }} />

                {/* 轮次条：每一轮一个 chip，点开看详情 */}
                <div className="flex flex-wrap gap-1.5">
                  {items.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => {
                        setTurnId(item.id);
                        setTab('overview');
                      }}
                      className={cn(
                        'rounded-full border px-2 py-0.5 text-[11px] transition-colors',
                        item.id === turnId
                          ? 'border-primary/50 bg-primary/10'
                          : 'border-border hover:bg-muted',
                        statusTone(item.status) === 'error' && 'text-red-500',
                      )}
                      title={fmtDateTime(item.ts)}
                    >
                      #{item.seq}
                      {item.stream ? ' · stream' : ''}
                      {item.truncated ? ' · ✂' : ''}
                    </button>
                  ))}
                </div>

                <div className="flex flex-wrap items-center justify-between gap-2 border-b pb-2">
                  <div className="flex flex-wrap gap-1">
                    {(['overview', 'request', 'response', 'diff'] as Tab[]).map((key) => (
                      <button
                        key={key}
                        type="button"
                        onClick={() => setTab(key)}
                        className={cn(
                          'rounded-full px-2.5 py-1 text-xs transition-colors',
                          tab === key
                            ? 'bg-primary text-primary-foreground'
                            : 'text-muted-foreground hover:bg-muted',
                        )}
                      >
                        {t(`trace.tab.${key}`)}
                      </button>
                    ))}
                  </div>
                  {/* 视角开关**常驻**。
                      早先只在「请求 / 响应」两个页签里渲染，结果默认停在「概览」时
                      整排看不见 —— 用户以为功能没做（实测反馈）。现在无论停在哪一
                      个页签都显示：在概览 / 差异里点它会顺手切到「响应」，因为那
                      正是两份报文差别最大、最常用视角的地方，避免点了没反应。 */}
                  <div
                    role="group"
                    aria-label={t('trace.viewLabel')}
                    title={t('trace.viewHint')}
                    className="flex items-center gap-1.5 text-[11px]"
                  >
                    <span className="text-muted-foreground">{t('trace.viewLabel')}</span>
                    <div className="flex items-center rounded-full border p-0.5">
                      {(['client', 'gateway'] as const).map((key) => (
                        <button
                          key={key}
                          type="button"
                          aria-pressed={view === key}
                          onClick={() => {
                            setView(key);
                            if (tab !== 'request' && tab !== 'response') setTab('response');
                          }}
                          className={cn(
                            'rounded-full px-2 py-0.5 transition-colors',
                            view === key
                              ? 'bg-foreground/10 font-medium'
                              : 'text-muted-foreground hover:text-foreground',
                          )}
                        >
                          {t(`trace.view.${key}`)}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>

                <div className="min-h-0 flex-1 overflow-auto">
                  {!turn && <SkeletonBar className="h-40 w-full" />}
                  {turn && tab === 'overview' && <Overview turn={turn} />}
                  {turn && tab === 'request' && <RequestTab turn={turn} view={view} />}
                  {turn && tab === 'response' && <ResponseTab turn={turn} view={view} />}
                  {turn && tab === 'diff' && (
                    <DiffTab diff={diff} hasPrev={Boolean(before)} />
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** 概览：这一轮的关键指标 + 模型实际说了什么。 */
function Overview({turn}: {turn: TapTurn}) {
  const t = useT();
  const rows: [string, React.ReactNode][] = [
    [t('trace.model'), turn.model || '—'],
    [t('trace.mappedModel'), turn.mapped_model || '—'],
    [t('trace.status'), String(turn.status ?? '—')],
    [t('trace.latency'), fmtMs(turn.latency_ms)],
    [t('trace.firstToken'), fmtMs(turn.first_token_ms)],
    [t('trace.promptTokens'), fmtNumber(turn.prompt_tokens ?? 0)],
    [t('trace.completionTokens'), fmtNumber(turn.completion_tokens ?? 0)],
    [t('trace.cacheHit'), fmtNumber(turn.cache_hit_tokens ?? 0)],
    [t('trace.cacheMiss'), fmtNumber(turn.cache_miss_tokens ?? 0)],
    [t('trace.cost'), turn.cost === null ? '—' : fmtMoney(turn.cost)],
    [t('trace.summary'), summaryText(turn.summary)],
  ];
  return (
    <div className="space-y-3">
      {turn.error && (
        <p className="rounded-md border border-red-500/30 bg-red-500/5 px-2 py-1 text-xs text-red-500">
          {turn.error}
        </p>
      )}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs sm:grid-cols-3">
        {rows.map(([label, value]) => (
          <div key={label} className="flex flex-col">
            <dt className="text-[11px] text-muted-foreground">{label}</dt>
            <dd className="truncate" title={String(value)}>{value}</dd>
          </div>
        ))}
      </dl>
      {turn.summary.tool_names.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {turn.summary.tool_names.map((name) => (
            <Badge key={name} variant="outline" className="text-[10px]">{name}</Badge>
          ))}
        </div>
      )}
      {turn.recon.text && (
        <Block title={t('trace.reconText')} body={turn.recon.text} />
      )}
      {turn.recon.reasoning && (
        <Block title={t('trace.reconReasoning')} body={turn.recon.reasoning} />
      )}
      {(turn.recon.tool_calls.length > 0) && <ToolCalls calls={turn.recon.tool_calls} />}
      {!turn.recon.text && !turn.recon.reasoning && !turn.recon.tool_calls.length && (
        <p className="text-xs text-muted-foreground">{t('trace.noRecon')}</p>
      )}
    </div>
  );
}

/**
 * 请求页签：同一个请求的两份报文。
 *
 *   · 客户端视角 —— 客户端**原样**发出的体（网关还没碰它）；
 *   · 网关视角 —— 改写后真正发往上游的体（模型映射、`max_tokens` 钳制、
 *     图片归一、`stream_options` 注入）。
 *
 * 两份都留才排得动「网关有没有偷偷改我的请求」：只看一份时，改了也看不出来。
 */
function RequestTab({turn, view}: {turn: TapTurn; view: 'client' | 'gateway'}) {
  const t = useT();
  const req = turn.request && typeof turn.request === 'object'
    ? (turn.request as Record<string, unknown>)
    : null;

  if (!turn.request_ok) {
    return <Block title={t('trace.rawTruncated')} body={turn.request_raw || ''} />;
  }

  const clientBody = req ? req.body : null;
  // 没有 `upstream_body` 时说明网关没改写（或该记录写于本功能上线前）：
  // 此时两份是同一份，不该显示成「空的上游请求」。
  const upstreamBody = req && req.upstream_body !== undefined ? req.upstream_body : clientBody;
  const rewrites = view === 'gateway' ? rewriteKeys(clientBody, upstreamBody) : [];
  const shown = view === 'client' ? clientBody : upstreamBody;

  // 请求头也是两份：客户端发来的（含它自己的网关密钥）与网关发给上游的
  // （含上游密钥）。两边 Authorization 是**不同的凭证**，只看 body 永远看不出
  // 「凭证换了没换」——而这是排查 401 / 打错账号池的第一现场。
  const clientHeaders = asHeaders(req ? req.headers : null);
  const upstreamHeaders = asHeaders(req ? req.upstream_headers : null);
  const headers = view === 'client' ? clientHeaders : upstreamHeaders;

  return (
    <div className="space-y-3">
      {turn.truncated && (
        <p className="text-[11px] text-amber-600">{t('trace.truncatedHint')}</p>
      )}
      <p className="text-[11px] text-muted-foreground">
        {view === 'client' ? t('trace.clientRequestHint') : t('trace.gatewayRequestHint')}
      </p>
      {view === 'gateway' && (
        rewrites.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1 text-[11px]">
            <span className="text-muted-foreground">{t('trace.rewrites')}</span>
            {rewrites.map((key) => (
              <Badge key={key} variant="outline" className="text-[10px]">{key}</Badge>
            ))}
          </div>
        ) : (
          <p className="text-[11px] text-muted-foreground">{t('trace.noRewrite')}</p>
        )
      )}
      <Headers
        title={view === 'client' ? t('trace.headers') : t('trace.upstreamHeaders')}
        items={headers}
      />
      <Block
        title={view === 'client' ? t('trace.clientRequest') : t('trace.upstreamRequest')}
        body={pretty(shown)}
      />
    </div>
  );
}

/** 把入库的 headers 文档转成有序键值对（丢掉大小写重复项，保留原样大小写）。 */
function asHeaders(value: unknown): [string, string][] {
  if (!value || typeof value !== 'object') return [];
  const seen = new Set<string>();
  const out: [string, string][] = [];
  Object.entries(value as Record<string, unknown>).forEach(([key, raw]) => {
    const lower = key.toLowerCase();
    if (seen.has(lower)) return;
    seen.add(lower);
    out.push([key, typeof raw === 'string' ? raw : JSON.stringify(raw)]);
  });
  return out.sort((a, b) => a[0].toLowerCase().localeCompare(b[0].toLowerCase()));
}

/**
 * 请求头表格。
 *
 * 值已由服务端脱敏（Authorization / Cookie / x-api-key 等一律 `前 6 位***`），
 * 所以这里直接展示，不再二次打码 —— 打两次会把「哪个密钥」也抹掉，反而无法
 * 对照「换没换凭证」。
 */
function Headers({title, items}: {title: string; items: [string, string][]}) {
  const t = useT();
  return (
    <section>
      <h4 className="mb-1 text-xs font-medium">{title}</h4>
      {items.length === 0 ? (
        <p className="text-[11px] text-muted-foreground">{t('trace.noHeaders')}</p>
      ) : (
        <dl className="max-h-56 overflow-auto rounded-md bg-muted/40 p-2 text-[11px]">
          {items.map(([key, value]) => (
            <div key={key} className="flex gap-2 py-0.5">
              <dt className="w-40 shrink-0 truncate font-mono text-muted-foreground">{key}</dt>
              <dd className="min-w-0 flex-1 break-words font-mono">{value || '—'}</dd>
            </div>
          ))}
        </dl>
      )}
      <p className="mt-1 text-[10px] text-muted-foreground">{t('trace.headersHint')}</p>
    </section>
  );
}

/** 工具调用列表：参数按原文复制，不重新序列化（模型的空白也是证据）。 */
function ToolCalls({calls}: {calls: {id: string; name: string; arguments: string}[]}) {
  const t = useT();
  return (
    <section>
      <h4 className="mb-1 text-xs font-medium">{t('trace.reconTools')}</h4>
      <div className="space-y-1.5">
        {calls.map((call, i) => (
          <div key={`${call.id || 'call'}-${i}`} className="rounded-md border bg-muted/30 p-2">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate font-mono text-[11px]">
                {call.name || `#${i + 1}`}
              </span>
              <CopyButton value={call.arguments} label={t('trace.copyArgs')} />
            </div>
            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words text-[11px]">
              {call.arguments || '{}'}
            </pre>
          </div>
        ))}
      </div>
    </section>
  );
}

/**
 * 响应页签：同一份回答的两份报文。
 *
 *   · 客户端视角 —— 客户端真正收到的（Chat 透传；Anthropic / Responses 是网关
 *     翻译后的报文，字段名与上游完全不同）；
 *   · 网关视角 —— 上游原文（SSE 帧 / JSON），用于取证与对照。
 */
function ResponseTab({turn, view}: {turn: TapTurn; view: 'client' | 'gateway'}) {
  const t = useT();
  const recon = (view === 'client' ? turn.client_recon : turn.upstream_recon) ?? turn.recon;
  const raw = view === 'client' ? turn.client_response_raw : turn.response_raw;
  const note = view === 'client'
    ? (turn.passthrough ? t('trace.passthroughNote') : t('trace.translatedNote'))
    : t('trace.upstreamNote');

  return (
    <div className="space-y-3">
      <p className="text-[11px] text-muted-foreground">{note}</p>
      {!recon && view === 'client' && !turn.passthrough ? (
        <p className="text-xs text-muted-foreground">{t('trace.notCaptured')}</p>
      ) : (
        <Block title={t('trace.reconText')} body={recon?.text || '—'} />
      )}
      {recon?.reasoning ? (
        <Block title={t('trace.reconReasoning')} body={recon.reasoning} />
      ) : null}
      {recon && recon.tool_calls.length > 0 ? <ToolCalls calls={recon.tool_calls} /> : null}
      {raw ? (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            {view === 'client' ? t('trace.clientRawStream') : t('trace.rawStream')}
          </summary>
          <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/40 p-2 text-[11px]">
            {raw}
          </pre>
        </details>
      ) : null}
    </div>
  );
}

/**
 * 网关改写了哪些顶层字段：两份请求体逐键比较，返回**有差异**的键名。
 *
 * 只比顶层：`messages` 里的差异属于「与上一轮对比」那条线的活，堆在这里会把
 * 「网关改了 model 和 max_tokens」这么一句话埋掉。
 */
function rewriteKeys(client: unknown, upstream: unknown): string[] {
  if (!client || typeof client !== 'object' || !upstream || typeof upstream !== 'object') {
    return [];
  }
  const a = client as Record<string, unknown>;
  const b = upstream as Record<string, unknown>;
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  const out: string[] = [];
  keys.forEach((key) => {
    let same: boolean;
    try {
      same = JSON.stringify(a[key]) === JSON.stringify(b[key]);
    } catch {
      same = false;
    }
    if (!same) out.push(key);
  });
  return out.sort();
}

/** 差异页签：和上一轮比，四块分开看（参数 / system / 消息 / 工具）。 */
function DiffTab({diff, hasPrev}: {diff: TapDiff | null; hasPrev: boolean}) {
  const t = useT();
  if (!hasPrev) {
    return <p className="text-xs text-muted-foreground">{t('trace.diffFirst')}</p>;
  }
  if (!diff) return <SkeletonBar className="h-32 w-full" />;
  if (diffIsEmpty(diff)) {
    return <p className="text-xs text-muted-foreground">{t('trace.diffEmpty')}</p>;
  }
  return (
    <div className="space-y-3 text-xs">
      {diff.params.length > 0 && (
        <section>
          <h4 className="mb-1 font-medium">{t('trace.diffParams')}</h4>
          <ul className="space-y-1">
            {diff.params.map((p) => (
              <li key={p.key} className="rounded-md bg-muted/40 p-1.5">
                <span className="font-mono text-[11px]">{p.key}</span>
                <div className="mt-0.5 flex gap-2 text-[11px]">
                  <span className="text-red-500 line-through">{p.before ?? '—'}</span>
                  <span className="text-emerald-600">{p.after ?? '—'}</span>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {diff.system.changed && (
        <section>
          <h4 className="mb-1 font-medium">{t('trace.diffSystem')}</h4>
          <p className="text-[11px] text-muted-foreground">
            {diff.system.before_chars} → {diff.system.after_chars}
          </p>
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/40 p-1.5 text-[11px]">
            {diff.system.after || '—'}
          </pre>
        </section>
      )}

      {(diff.messages.added.length > 0 || diff.messages.removed.length > 0
        || diff.messages.changed.length > 0) && (
        <section>
          <h4 className="mb-1 font-medium">
            {t('trace.diffMessages', {
              before: diff.messages.before_total,
              after: diff.messages.after_total,
            })}
          </h4>
          <ul className="space-y-1">
            {diff.messages.added.map((m) => (
              <li key={`a${m.index}`} className="rounded-md border border-emerald-500/30 p-1.5">
                <Badge variant="outline" className="mr-1 text-[10px]">+ {m.role}</Badge>
                {m.preview}
              </li>
            ))}
            {diff.messages.removed.map((m) => (
              <li key={`r${m.index}`} className="rounded-md border border-red-500/30 p-1.5">
                <Badge variant="outline" className="mr-1 text-[10px]">- {m.role}</Badge>
                {m.preview}
              </li>
            ))}
            {diff.messages.changed.map((m) => (
              <li key={`c${m.index}`} className="rounded-md bg-muted/40 p-1.5">
                <Badge variant="outline" className="mr-1 text-[10px]">~ {m.role}</Badge>
                {m.after}
              </li>
            ))}
          </ul>
        </section>
      )}

      {(diff.tools.added.length > 0 || diff.tools.removed.length > 0
        || diff.tools.changed.length > 0) && (
        <section>
          <h4 className="mb-1 font-medium">{t('trace.diffTools')}</h4>
          <div className="flex flex-wrap gap-1">
            {diff.tools.added.map((n) => (
              <Badge key={`ta${n}`} className="bg-emerald-600/15 text-[10px] text-emerald-700">
                + {n}
              </Badge>
            ))}
            {diff.tools.removed.map((n) => (
              <Badge key={`tr${n}`} className="bg-red-600/15 text-[10px] text-red-600">
                - {n}
              </Badge>
            ))}
            {diff.tools.changed.map((x) => (
              <Badge key={`tc${x.name}`} variant="outline" className="text-[10px]">
                ~ {x.name}
              </Badge>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function Block({title, body}: {title: string; body: string}) {
  return (
    <section>
      <h4 className="mb-1 flex items-center gap-1 text-xs font-medium">
        <GitCompare className="h-3 w-3 text-muted-foreground" />
        {title}
      </h4>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/40 p-2 text-[11px]">
        {body}
      </pre>
    </section>
  );
}

function pretty(value: unknown): string {
  if (value === null || value === undefined) return '—';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/**
 * 时间概览（对照 DSH「运行轨迹」的时间轴）。
 *
 * 每根条的三段含义与线上的一致：
 *   · 起点 = 这一轮请求开始处理的时刻（`ts`）；
 *   · 宽度 = 总耗时（`latency_ms`）；
 *   · 条内左段 = 首字等待（`first_token_ms`），右段 = 生成。
 *
 * 为什么值得画：会话列表里每轮的耗时是一个数字，串起来才看得出「这一跑是
 * 越跑越慢还是一直很快」。首字与生成分开画，是因为它们指向完全不同的原因
 * ——首字长是上游排队/账号慢，生成长是输出多或模型慢。
 *
 * 没有首字延迟（非流式）的轮次整条按生成色画，**不**假设一个首字值。
 */
function Timeline({
  turns,
  selected,
  onSelect,
}: {
  turns: TapTurnBrief[];
  selected: number | null;
  onSelect: (id: number) => void;
}) {
  const t = useT();
  if (turns.length < 2) return null;

  const startMs = Math.min(...turns.map((x) => x.ts * 1000));
  const endMs = Math.max(...turns.map((x) => x.ts * 1000 + Math.max(20, x.latency_ms ?? 0)));
  const span = Math.max(1, endMs - startMs);

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-[11px] text-muted-foreground">
        <span>{t('trace.timeline', {n: turns.length})}</span>
        <span>{fmtMs(span)}</span>
      </div>
      <div className="relative h-9 rounded-md bg-muted/30">
        {turns.map((x) => {
          const lat = Math.max(20, x.latency_ms ?? 0);
          const left = ((x.ts * 1000 - startMs) / span) * 100;
          const width = Math.max(0.8, (lat / span) * 100);
          const ttft = Math.min(lat, x.first_token_ms ?? 0);
          const tone = statusTone(x.status);
          return (
            <button
              key={x.id}
              type="button"
              onClick={() => onSelect(x.id)}
              aria-label={`#${x.seq} ${fmtMs(x.latency_ms)}`}
              title={`#${x.seq} · ${fmtDateTime(x.ts)} · ${fmtMs(x.latency_ms)}`}
              className={cn(
                'absolute top-1 bottom-1 flex overflow-hidden rounded-sm transition-shadow',
                selected === x.id ? 'ring-2 ring-primary' : 'hover:ring-1 hover:ring-primary/40',
                tone === 'error' && 'opacity-90',
              )}
              style={{left: `${left}%`, width: `${width}%`}}
            >
              <span
                className={cn('h-full', tone === 'error' ? 'bg-red-500/70' : 'bg-amber-500/70')}
                style={{width: `${(ttft / lat) * 100}%`}}
              />
              <span
                className={cn('h-full flex-1', tone === 'error' ? 'bg-red-500/40' : 'bg-primary/60')}
              />
            </button>
          );
        })}
      </div>
    </div>
  );
}

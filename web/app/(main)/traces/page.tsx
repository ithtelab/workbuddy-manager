'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Download,
  Radar,
  RefreshCw,
  Settings2,
  Trash2,
} from 'lucide-react';
import {useAuth} from '@/lib/auth-context';
import {errText, tapApi} from '@/lib/api';
import {fmtDateTime, fmtMoney, fmtNumber} from '@/lib/format';
import {useAsyncAll} from '@/lib/use-async-data';
import {useDebounced} from '@/lib/use-debounce';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {
  DEFAULT_REFRESH_INTERVAL,
  getRefreshInterval,
  REFRESH_INTERVAL_CHOICES,
  setRefreshInterval,
  type RefreshInterval,
} from '@/lib/display-prefs';
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
import type {
  TapDiff,
  TapSessionDetail,
  TapTurn,
  TapTurnBrief,
} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {JsonPreview, salvageJson} from '@/components/common/trace/JsonView';
import type {JsonPath} from '@/lib/json-path';
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
import {Popover, PopoverContent, PopoverTrigger} from '@/components/ui/popover';
import {cn} from '@/lib/utils';

const PAGE_SIZE = 20;

/** 详情面板的四个页签。差异不是常驻请求：只在切到它时才算。 */
type Tab = 'overview' | 'request' | 'response' | 'diff';

/**
 * 报文块的**内部滚动**类。
 *
 * 为什么不能让内容自然撑开：网关视角的响应体实测到过 130 KB / 一万多行，
 * min(60vh,32rem) 用的是视口单位，但 60vh 在长内容时仍会把整页推到几千像素 ——
 * 用户要滚过整块报文才能点到下面的控件（实测反馈）。固定像素上限 + `overflow-auto`
 * 让滚动发生在块内，页面高度稳定；60vh 只对小屏起兜底作用。
 */
const BLOCK_SCROLL = 'max-h-96 overflow-auto';

/** 稳定空数组：每次渲染现写 `[]` 会让下游依赖它的 memo 每帧重算（同日志页）。 */
const EMPTY_TURNS: TapTurnBrief[] = [];

/**
 * 距上次更新过了多少秒（用于「上次更新 N 秒前」）。
 *
 * `now` 是**每秒走一格**的显示时钟，不是 Date.now() —— 直接用 Date.now()
 * 的话，这行小字只在刷新那一刻重渲染，那个数字永远停在 0 秒。
 */
function agoSeconds(ts: number, now: number): number {
  return Math.max(0, Math.round((now - ts) / 1000));
}

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
  /**
   * 是否显示辅助调用（会话标题生成这类）。默认不显示：它们属于某条主会话，
   * 单列出来会让人以为「一次对话被记了两条」——实测反馈。
   */
  const [showAux, setShowAux] = useState(false);
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
  /**
   * 视角：同一轮调用有两份报文，请求 / 响应逐段一一对应。
   *   · client —— 客户端（下游）手里那份：请求是它原样发的，响应是它真正收到的；
   *   · gateway —— 网关与上游之间那一跳：请求是改写后发出去的，响应是上游原文。
   * 两个视角共用「请求 / 响应」两个页签 —— 报文本就一一对应，单独开页签
   * 会把同一轮拆成两处看（实测反馈）。
   */
  const [view, setView] = useState<'client' | 'gateway'>('client');
  /**
   * 报文格式：Preview（可折叠树，默认）/ Raw（原文）。
   *
   * 两者都要：Preview 用来**读结构**（上百条消息时逐层点开比滚一屏 JSON 快），
   * Raw 用来**逐字节对照**（空格、键序、转义都原样，复制出去喂给别的工具）。
   */
  const [format, setFormat] = useState<'preview' | 'raw'>('preview');
  /**
   * 差异页点「定位」时带过来的 JSON 路径。切到请求页签后由 JsonPreview
   * 逐层展开并高亮，用户不用自己去树上找。
   */
  const [focusPath, setFocusPath] = useState<JsonPath | null>(null);
  /**
   * Preview 树的展开/收起信号。大报文默认**全收起**（展开两层在 500 KB 的
   * 截断体上等于整页树），用户点「展开全部」再摊开 —— 这是可逆操作，
   * 手动展开过的节点在收起信号撤销后保留原状。
   */
  const [expandAll, setExpandAll] = useState<boolean | undefined>(undefined);
  const [turn, setTurn] = useState<TapTurn | null>(null);
  /** 单轮正文取不到（404 / 网络错误）：要明确说，而不是让骨架屏一直转。 */
  const [turnFailed, setTurnFailed] = useState(false);
  const [diff, setDiff] = useState<TapDiff | null>(null);
  const [busy, setBusy] = useState(false);

  const [settledQuery] = useDebounced(query, 350, () => setPage(1));

  const list = useAsyncAll(
    {
      cfg: () => tapApi.config(),
      sessions: () => tapApi.sessions({
        page, size: PAGE_SIZE, days, client, q: settledQuery || undefined,
        include_aux: showAux ? 1 : undefined,
      }),
    },
    [],
    [page, client, settledQuery, days, showAux],
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

  /* ── 自动刷新 ───────────────────────────────────────────────
   *
   * 抓包是「别人（客户端）在写、我在读」的场景：新请求完成后，页面必须自己
   * 跟上，否则用户盯着一个静止的界面，还得手动点刷新才知道刚才发生了什么。
   *
   * 刷新分两层，各自的代价不同：
   *  · **列表层**（会话列表 + 统计）：便宜。用户最关心的是「有没有新会话 /
   *    轮数涨了没」。
   *  · **详情层**（当前会话的轮次）：贵一点（一次会话几百轮）。
   *
   * 两层用**同一个**间隔，由用户在齿轮里设置（默认 20 秒）。
   * 两层都**不打断**用户正在看的东西：刷新详情时，正在看的那一轮（turnId）
   * 保持不变，不会被「跳到最新一轮」抢走 —— 那是自动刷新最招人烦的行为。
   * 只有当用户**没在看任何一轮**时，才跟着新数据走到最后一轮。
   */

  /**
   * 自动刷新间隔（秒）：**用户可调**，默认 20 秒（见 lib/display-prefs.ts）。
   *
   * 存在浏览器本地，不落后端 —— 这是「我这块屏幕怎么看」的偏好，不是系统行为。
   */
  const [intervalSec, setIntervalSec] = useState<RefreshInterval>(DEFAULT_REFRESH_INTERVAL);
  /**
   * 首屏读一次本地偏好。
   *
   * 为什么不在 useState 的初值里直接读：那是**服务端渲染**时执行的，
   * localStorage 只在浏览器里有 —— 首屏会拿到默认值，与客户端不一致
   * （同 lib/realm-context.tsx 的处理：挂载后再读）。
   */
  useEffect(() => {
    setIntervalSec(getRefreshInterval());
  }, []);

  /**
   * 两层心跳用**同一个**间隔。
   *
   * 之前分成两层（列表 10 秒 / 详情 20 秒），是为了「详情更贵、少拉点」——
   * 但用户要的是一个能调的间隔，两个数字反而让人困惑：调了到底哪个生效？
   * 现在统一成一个值，齿轮里改一次两边都跟着变。
   *
   * 代价：间隔调小时，列表层也跟着变快 —— 但列表层本来就便宜（一次会话列表
   * + 统计），真正贵的是详情层，而详情层的轮次条只在选中会话时才拉。
   */
  const tickMs = intervalSec * 1000;

  /** 最近一次成功刷新列表的时间 —— 界面上显示「上次更新 xx 秒前」。 */
  const [lastUpdate, setLastUpdate] = useState<number | null>(null);
  /**
   * 每秒走一格的「显示用」时钟。
   *
   * 为什么需要它：「N 秒前更新」这个数字要**自己往上跳**，否则它只在刷新那一刻
   * 重渲染一次 —— 而刷新本身每 10 秒一次，于是那个数字永远停在「0 秒前」
   * （刷新完立刻算就是 0 秒），看着像坏了。
   *
   * 这个时钟只驱动那行小字的重渲染，**不触发任何请求**。
   */
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  /** 自动刷新开关：用户可能正在盯着一个值，不希望它被刷掉。 */
  const [autoRefresh, setAutoRefresh] = useState(true);
  /** 「有新轮次了」的提示条：只在用户**停在中间某轮**时出现。 */
  const [pendingTurns, setPendingTurns] = useState(0);

  /**
   * 详情刷新时的「当前会话已有多少轮」快照。
   *
   * 用它算新增了多少轮：只比**条数**不够（同一条被重写时条数不变），
   * 比最大 seq 更准 —— seq 单调递增，新增轮次必然带来更大的 seq。
   */
  const knownMaxSeq = useRef(0);

  /** 刷新列表 + 详情。 */
  const refreshAll = useCallback(async () => {
    await list.reload();
    await detail.reload();
    setLastUpdate(Date.now());
  }, [list, detail]);

  /**
   * 心跳：定时重拉。
   *
   * 分两层定时器而不是一个：详情层（一次会话几百轮）比列表层贵得多，
   * 合成一个 10 秒会把后端压得慌。
   */
  useHeartbeat(() => {
    if (!autoRefresh) return;
    void list.reload().then(() => setLastUpdate(Date.now()));
  }, tickMs);

  useHeartbeat(() => {
    if (!autoRefresh) return;
    void detail.reload();
  }, tickMs);

  /**
   * 详情刷新后：算出新增了几轮。
   *
   * **只提示，绝不自动跳转。**
   *
   * 轮次列表是「随时在长」的（客户端还在跑，轮次就一条条冒出来）—— 列表自己
   * 更新就够了，但**当前看的那一轮不能被抢走**：用户正在读第 30 轮的内容，
   * 页面却自己跳到第 31 轮，读到一半的东西就没了。
   *
   * 所以不管用户停在哪一轮，都一律给一个「有 N 轮新的，点击查看最新」的入口，
   * 跳不跳由用户自己决定。
   */
  useEffect(() => {
    const items = session?.items ?? EMPTY_TURNS;
    if (!items.length) {
      knownMaxSeq.current = 0;
      setPendingTurns(0);
      return;
    }
    const maxSeq = items.reduce((m, x) => Math.max(m, x.seq), 0);
    const prevSeq = knownMaxSeq.current;
    knownMaxSeq.current = maxSeq;
    // 首屏（prevSeq=0）不算「新增」，那是初次加载。
    if (prevSeq === 0 || maxSeq <= prevSeq) {
      setPendingTurns(0);
      return;
    }
    setPendingTurns(maxSeq - prevSeq);
  }, [session]);

  /** 切到会话时重置「已知最大 seq」，避免把上一个会话的轮数算成新增。 */
  useEffect(() => {
    knownMaxSeq.current = 0;
    setPendingTurns(0);
  }, [sessionId]);

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
    setTurnFailed(false);
    tapApi.turn(turnId)
      .then((data) => {
        if (alive) setTurn(data);
      })
      .catch((e) => {
        if (!alive) return;
        setTurnFailed(true);
        notify.err(errText(e));
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
                    onClick={() => void refreshAll()}>
              <RefreshCw
                className={cn('h-3.5 w-3.5', list.isRefreshing && 'animate-spin')}
              />
              {t('common.refresh')}
            </Button>
            {/* 自动刷新开关：抓包是「别人在写、我在读」，页面得自己跟上。
                开着时右上角显示「上次更新 N 秒前」，让人知道这不是卡住了。 */}
            <div className="flex items-center gap-2 rounded-full border px-3 py-1">
              <Switch
                id="tap-auto-refresh"
                checked={autoRefresh}
                onCheckedChange={setAutoRefresh}
              />
              <Label htmlFor="tap-auto-refresh" className="text-xs">
                {t('trace.autoRefresh')}
              </Label>
              {autoRefresh && (
                <span className="text-[11px] text-muted-foreground">
                  {lastUpdate
                    ? t('trace.updatedAgo', {s: agoSeconds(lastUpdate, now)})
                    : t('trace.updating')}
                </span>
              )}
            </div>
            {/* 齿轮：配置刷新间隔。
                单独一个按钮而不是把选择器直接摊在工具栏上 —— 间隔是偶尔才调的
                参数，常驻一排选项会挤掉「刷新 / 清空」这些更常用的按钮。 */}
            <Popover>
              <PopoverTrigger asChild>
                <Button variant="outline" size="icon"
                        className="h-8 w-8 rounded-full"
                        aria-label={t('trace.refreshSettings')}>
                  <Settings2 className="h-3.5 w-3.5" />
                </Button>
              </PopoverTrigger>
              <PopoverContent align="end" className="w-72 space-y-3">
                <div className="space-y-1">
                  <p className="text-sm font-medium">{t('trace.refreshSettings')}</p>
                  <p className="text-[11px] text-muted-foreground">
                    {t('trace.refreshIntervalHint')}
                  </p>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="tap-refresh-interval" className="text-xs">
                    {t('trace.refreshInterval')}
                  </Label>
                  <Select
                    value={String(intervalSec)}
                    onValueChange={(v) => {
                      const n = Number(v) as RefreshInterval;
                      setIntervalSec(n);
                      setRefreshInterval(n);
                    }}
                  >
                    <SelectTrigger
                      id="tap-refresh-interval"
                      size="sm"
                      className="w-full rounded-xl"
                      // 无障碍：下拉本身要有可读名称（Label 的 htmlFor 不算数，
                      // 读屏软件读到的是触发按钮）
                      aria-label={t('trace.refreshInterval')}
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {REFRESH_INTERVAL_CHOICES.map((c) => (
                        <SelectItem key={c} value={String(c)}>
                          {t('trace.refreshIntervalValue', {s: c})}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </PopoverContent>
            </Popover>
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
        // 1fr 列必须写成 minmax(0,1fr)：默认 minmax(auto,1fr) 的 min 是内容宽，
        // 长报文会把列撑到 1800+px，页面横向滚（实测）。
        <div className="grid min-h-[60vh] gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
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
              {/* 辅助调用（标题生成等）默认折叠：它们是同一次对话的一部分 */}
              <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-muted-foreground">
                <Switch checked={showAux} onCheckedChange={setShowAux}
                        aria-label={t('trace.showAux')} />
                {t('trace.showAux')}
              </label>
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
                    <span className="flex shrink-0 items-center gap-1">
                      {s.kind !== 'main' ? (
                        <Badge className="bg-amber-500/15 text-[10px] text-amber-700">
                          {t('trace.auxBadge')}
                        </Badge>
                      ) : (s.aux_count ?? 0) > 0 ? (
                        // 主会话下面挂着辅助调用时给个数字：辅助调用默认不单列，
                        // 但完全看不见会让人以为「标题没被记下来」。
                        <span className="text-[10px] text-muted-foreground">
                          {t('trace.hasAux', {n: s.aux_count ?? 0 })}
                        </span>
                      ) : null}
                      <Badge variant="outline" className="text-[10px]">
                        {clientLabel(s.client)}
                      </Badge>
                    </span>
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
                    {session.kind !== 'main' && (
                      <p className="mt-1 text-[11px] text-amber-600">
                        {session.parent_id
                          ? t('trace.auxOf', {id: session.parent_id})
                          : t('trace.auxUnlinked')}
                        {session.parent_id ? (
                          <button
                            type="button"
                            className="ml-1 underline"
                            onClick={() => { setSessionId(session.parent_id); setTurnId(null); }}
                          >
                            {t('trace.auxJump')}
                          </button>
                        ) : null}
                      </p>
                    )}
                    {(session.aux?.length ?? 0) > 0 && (
                      <p className="mt-1 flex flex-wrap items-center gap-1 text-[11px] text-muted-foreground">
                        {t('trace.auxInline', {n: session.aux?.length ?? 0})}
                        {(session.aux ?? []).map((a) => (
                          <button
                            key={a.id}
                            type="button"
                            className="rounded-full border px-1.5 py-0.5 underline decoration-dotted"
                            onClick={() => { setSessionId(a.id); setTurnId(null); }}
                          >
                            {a.kind === 'compact' ? t('trace.kindCompact') : t('trace.kindTitle')} #{a.id}
                          </button>
                        ))}
                      </p>
                    )}
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

                {/* 「有 N 轮新的」提示：只在用户停在**中间某轮**时出现。
                    自动刷新不能把正在读的内容顶掉 —— 所以这里给一个入口，
                    让用户自己决定要不要跳到最新。 */}
                {pendingTurns > 0 && (
                  <button
                    type="button"
                    onClick={() => {
                      const last = items[items.length - 1];
                      if (!last) return;
                      setTurnId(last.id);
                      setTab('overview');
                      setPendingTurns(0);
                    }}
                    className="flex w-fit items-center gap-1.5 rounded-full border border-primary/40 bg-primary/10 px-2.5 py-1 text-[11px] text-primary transition-colors hover:bg-primary/20"
                  >
                    <Radar className="h-3 w-3" />
                    {t('trace.newTurns', {n: pendingTurns})}
                  </button>
                )}

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
                            // 停在概览 / 差异时点视角：切到「响应」，那里最能看出
                            // 三个视角的差别；请求 / 响应页签则原地切换。
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

                  {/* 格式开关：Preview（可折叠树，默认）↔ Raw（原文）。
                      两个都需要 —— Preview 用来读结构，Raw 用来逐字节对照。 */}
                  {(tab === 'request' || tab === 'response') && (
                    <div
                      role="group"
                      aria-label={t('trace.formatLabel')}
                      className="flex items-center gap-1.5 text-[11px]"
                    >
                      <span className="text-muted-foreground">{t('trace.formatLabel')}</span>
                      <div className="flex items-center rounded-full border p-0.5">
                        {(['preview', 'raw'] as const).map((key) => (
                          <button
                            key={key}
                            type="button"
                            aria-pressed={format === key}
                            onClick={() => setFormat(key)}
                            className={cn(
                              'rounded-full px-2 py-0.5 transition-colors',
                              format === key
                                ? 'bg-foreground/10 font-medium'
                                : 'text-muted-foreground hover:text-foreground',
                            )}
                          >
                            {t(`trace.format.${key}`)}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                </div>

                {items.length === 0 ? (
                  // 会话在、明细没了（典型来源：数据库损坏后只恢复出汇总）。
                  // 必须说清楚 —— 早先这里只是一直转骨架屏，看起来像功能坏了。
                  <EmptyState
                    icon={Radar}
                    title={t('trace.noTurnsTitle')}
                    description={t('trace.noTurnsDesc')}
                  />
                ) : (
                <div className="min-h-0 flex-1 overflow-auto max-h-[calc(100vh-16rem)]">
                  {!turn && !turnFailed && <SkeletonBar className="h-40 w-full" />}
                  {!turn && turnFailed && (
                    <p className="text-xs text-muted-foreground">{t('trace.turnLoadFailed')}</p>
                  )}
                  {turn && tab === 'overview' && <Overview turn={turn} />}
                  {turn && tab === 'request' && (
                    <div className="min-w-0">
                      <RequestTab turn={turn} view={view}
                                  format={format} highlight={focusPath}
                                  expandAll={expandAll} focusPath={focusPath}
                                  onToggleExpand={() => setExpandAll((v) => !(v ?? false))} />
                    </div>
                  )}
                  {turn && tab === 'response' && (
                    <div className="min-w-0">
                      <ResponseTab turn={turn} view={view} format={format}
                              expandAll={expandAll}
                              onToggleExpand={() => setExpandAll((v) => !(v ?? false))} />
                    </div>
                  )}
                  {turn && tab === 'diff' && (
                    <DiffTab diff={diff} hasPrev={Boolean(before)} onLocate={(path) => {
                        // 路径前缀取决于树顶层的形状：
                        //   · 截断体：根是 {headers, upstream_headers, body}，消息在
                        //     body.messages 下 → 要补 'body'；
                        //   · 完整体：根**就是** body 本身（键是 model/messages/…）
                        //     → 不能补，否则多一层找不到（实测定位停在半路）。
                        setFocusPath(path);
                        setTab('request');
                        setView('gateway');
                        setFormat('preview');
                        // 滚动由树自己做（JsonView 的 focusPath）：它需要先**展开**
                        // 目标行所在的那几层 —— 折叠状态下目标行不在 DOM 里，
                        // 页面层 scrollIntoView 只会滚到空白处。
                      }} />
                  )}
                </div>
                )}
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
 * 请求页签：同一个请求的两份报文（按视角切换）。
 *
 *   · 客户端视角 —— 客户端**原样**发出的体（网关还没碰它）；
 *   · 网关视角 —— 改写后真正发往上游的体（模型映射、`max_tokens` 钳制、
 *     图片归一、`stream_options` 注入）。
 *
 * 都留才排得动「中间哪一环改了请求」：只看一份时，改了也看不出来。
 */
function RequestTab({turn, view, format, highlight, expandAll, focusPath,
                     onToggleExpand}: {
  turn: TapTurn;
  view: 'client' | 'gateway';
  format: 'preview' | 'raw';
  highlight: JsonPath | null;
  expandAll?: boolean;
  /** diff「定位」的目标路径：树会展开到那一行。 */
  focusPath?: JsonPath | null;
  /** 「展开全部 / 收起全部」—— 放在报文标题行里。 */
  onToggleExpand?: () => void;
}) {
  const t = useT();
  const req = turn.request && typeof turn.request === 'object'
    ? (turn.request as Record<string, unknown>)
    : null;

  // 截断的请求体：JSON 已经解析不了，但**能切出前缀树** —— 从原文里截到哪一层
  // 就展示到哪一层，比甩一坨 500 KB 的 <pre> 有用得多（实测反馈：滚动半分钟
  // 找不到重点）。解析不出来的剩余尾巴单独给一段原文。
  const salvage = useMemo(() => {
    if (turn.request_ok) return null;
    const raw = turn.request_raw || '';
    // 截断发生在 body 中段，而 headers 排在最前面 —— 几乎总是完整的。
    // 先切「body 之前」的部分（headers 等完整字段），能整体解析就直接用；
    // body 本身再单独切前缀树，两块拼起来展示。
    // `"body"` 可能出现在 headers 的**值**里（UA/cookie 都可能含这个词），
    // 所以必须找 `"body": {` 这种带值的形态，并且取最后一次出现 —— 入库结构
    // 固定是 {headers, body}，body 一定在最后。
    let bodyStart = -1;
    const needle = '"body": {';
    let scan = 0;
    while (true) {
      const at = raw.indexOf(needle, scan);
      if (at < 0) break;
      bodyStart = at;
      scan = at + 1;
    }
    const headPart = bodyStart > 0 ? raw.slice(0, bodyStart - 1) : '';
    let headersDoc: unknown = null;
    if (headPart) {
      // headPart 以逗号结尾（body 是最后一个键），补 `}` 前先去掉
      try {
        const headFixed = `${headPart.replace(/[\s,]+$/, '')}}`;
        headersDoc = JSON.parse(headFixed) as Record<string, unknown>;
      } catch { /* headers 段不完整（极少见），退回不展示 */ }
    }
    const bodyRaw = bodyStart > 0 ? raw.slice(bodyStart + '"body": '.length) : raw;
    const prefix = salvageJson(bodyRaw);
    if (prefix === null && headersDoc === null) return null;
    return {headers: headersDoc, prefix, tail: '', ok: false};
  }, [turn.request_ok, turn.request_raw]);

  if (!turn.request_ok && !salvage) {
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
  // 截断体：headers 从抢救结果里来（入库的 request_json 解析不了，但 headers
  // 段几乎总是完整的 —— 它在截断点之前）。不救的话界面上会显示「未采集到」，
  // 而下面树里明明有 headers，自相矛盾（实测反馈）。
  const clientHeaders = salvage && salvage.headers
    ? asHeaders((salvage.headers as Record<string, unknown>).headers ?? salvage.headers)
    : asHeaders(req ? req.headers : null);
  const upstreamHeaders = salvage && salvage.headers
    ? asHeaders((salvage.headers as Record<string, unknown>).upstream_headers ?? null)
    : asHeaders(req ? req.upstream_headers : null);
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
              <span key={key} className="rounded-full border px-1.5 text-[10px]">{key}</span>
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
      <section>
        <BodyHeader
          title={view === 'client' ? t('trace.requestBody') : t('trace.upstreamRequestBody')}
          format={format}
          expandAll={expandAll}
          onToggleExpand={onToggleExpand}
        />
        {!turn.request_ok && salvage ? (
          // 截断体：headers（完整）+ body 前缀树拼成一棵展示；默认全收起。
          format === 'preview' ? (
            <JsonPreview
              value={{...(salvage.headers as object), body: salvage.prefix}}
              expandAll={expandAll}
              focusPath={focusPath}
              maxHeight="max-h-[60vh]" />
          ) : (
            <pre className={BLOCK_SCROLL + " rounded-md bg-muted/40 p-2 font-mono text-[11px]"}>
              {turn.request_raw}
            </pre>
          )
        ) : format === 'preview' ? (
          <JsonPreview value={shown} highlight={highlight} expandAll={expandAll}
                       focusPath={focusPath} />
        ) : (
          <pre className={BLOCK_SCROLL + " rounded-md bg-muted/40 p-2 font-mono text-[11px]"}>
            {pretty(shown)}
          </pre>
        )}
      </section>
    </div>
  );
}

/**
 * 报文区的标题行：标题 + 格式相关的操作。
 *
 * 「展开全部 / 收起全部」放在**标题后面**而不是页面顶部：它只对下面这一段
 * JSON 生效，放在顶部工具条里离作用对象太远，用户点了不知道作用在哪
 * （实测反馈：点 Preview 后下面又冒出一个「展开全部」，位置突兀）。
 */
function BodyHeader({title, format, expandAll, onToggleExpand, extra}: {
  title: string;
  format: 'preview' | 'raw';
  expandAll?: boolean;
  onToggleExpand?: () => void;
  /** 标题右侧的附加内容（如「已解压」标记）。 */
  extra?: React.ReactNode;
}) {
  const t = useT();
  return (
    <div className="mb-1 flex flex-wrap items-center gap-2">
      <h4 className="text-xs font-medium">{title}</h4>
      {format === 'preview' && onToggleExpand && (
        <button
          type="button"
          onClick={onToggleExpand}
          className="rounded-full border px-2 py-0.5 text-[10px] text-muted-foreground hover:bg-muted"
        >
          {expandAll ? t('trace.collapseAll') : t('trace.expandAll')}
        </button>
      )}
      {extra}
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
    // min-w-0 一路锁到底：flex/grid 的 min-width:auto 会让任何一层被长值撑开，
    // 断行（break-words / truncate）全部失效 —— 页面横向滚（实测 scrollWidth 2278）。
    <section className="min-w-0">
      <h4 className="mb-1 text-xs font-medium">{title}</h4>
      {items.length === 0 ? (
        <p className="text-[11px] text-muted-foreground">{t('trace.noHeaders')}</p>
      ) : (
        <dl className="max-h-56 min-w-0 overflow-auto rounded-md bg-muted/40 p-2 text-[11px]">
          {items.map(([key, value]) => (
            <div key={key} className="flex min-w-0 gap-2 py-0.5">
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
function ResponseTab({turn, view, format, expandAll, onToggleExpand}: {
  turn: TapTurn;
  view: 'client' | 'gateway';
  format: 'preview' | 'raw';
  expandAll?: boolean;
  onToggleExpand?: () => void;
}) {
  const t = useT();

  const recon = (view === 'client' ? turn.client_recon : turn.upstream_recon) ?? turn.recon;
  const raw = view === 'client' ? turn.client_response_raw : turn.response_raw;
  // 客户端视角在**非流式**下是一份完整 JSON（翻译后的报文），可以直接上树；
  // 流式是一串 SSE，只给原文，重建结果放在上面的 recon 里。
  const structured = view === 'client' && turn.client_response_ok ? turn.client_response : null;
  const note = view === 'client'
    ? (turn.passthrough ? t('trace.passthroughNote') : t('trace.translatedNote'))
    : t('trace.upstreamNote');

  if (format === 'raw') {
    return (
      <div className="space-y-3">
        <p className="text-[11px] text-muted-foreground">{note}</p>
        <section>
          <h4 className="mb-1 text-xs font-medium">{t('trace.rawStream')}</h4>
          <pre className={BLOCK_SCROLL + " rounded-md bg-muted/40 p-2 font-mono text-[11px]"}>
            {raw || (structured ? pretty(structured) : '—')}
          </pre>
        </section>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-[11px] text-muted-foreground">{note}</p>
      {structured ? (
        <section>
          <BodyHeader title={t('trace.clientResponse')} format={format}
                     expandAll={expandAll} onToggleExpand={onToggleExpand} />
          <JsonPreview value={structured} expandAll={expandAll} />
        </section>
      ) : !recon && view === 'client' && !turn.passthrough ? (
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
          <pre className={"mt-1 " + BLOCK_SCROLL + " rounded-md bg-muted/40 p-2 text-[11px]"}>
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

/**
 * 差异页签：和上一轮比，四块分开看（参数 / system / 消息 / 工具）。
 *
 * 每条都能**展开**看完整值（以前只有一行摘要，想看清只能回去翻请求体），
 * 并且能「定位」—— 跳到「请求 · 网关 · Preview」并展开到那个 key 上，
 * 由 JsonView 高亮 + 滚到视野中央。
 */
function DiffTab({diff, hasPrev, onLocate}: {
  diff: TapDiff | null;
  hasPrev: boolean;
  onLocate: (path: JsonPath) => void;
}) {
  const t = useT();
  if (!hasPrev) {
    return <p className="text-xs text-muted-foreground">{t('trace.diffFirst')}</p>;
  }
  if (!diff) return <SkeletonBar className="h-32 w-full" />;
  if (diffIsEmpty(diff)) {
    return (
      <div className="space-y-1">
        <p className="text-xs text-muted-foreground">{t('trace.diffEmpty')}</p>
        {diff.from_digest && (
          <p className="text-[11px] text-muted-foreground">{t('trace.diffDigestNote')}</p>
        )}
      </div>
    );
  }
  return (
    <div className="space-y-4 text-xs">
      {diff.from_digest && (
        <p className="rounded-md border border-amber-500/30 bg-amber-500/5 px-2 py-1.5 text-[11px] text-amber-700 dark:text-amber-300">
          {t('trace.diffDigestNote')}
        </p>
      )}
      {diff.params.length > 0 && (
        <section>
          <h4 className="mb-1 font-medium">{t('trace.diffParams')}</h4>
          <ul className="space-y-1">
            {diff.params.map((p) => (
              <DiffRow key={p.key} title={p.key} onLocate={() => onLocate([p.key])}
                       locateLabel={t('trace.locate')}>
                <div className="grid gap-1 sm:grid-cols-2">
                  <ValueCell label={t('trace.diffBefore')} value={p.before} tone="bad" />
                  <ValueCell label={t('trace.diffAfter')} value={p.after} tone="good" />
                </div>
              </DiffRow>
            ))}
          </ul>
        </section>
      )}

      {diff.system.changed && (
        <section>
          <h4 className="mb-1 font-medium">{t('trace.diffSystem')}</h4>
          <DiffRow title={`${diff.system.before_chars} → ${diff.system.after_chars}`}
                   onLocate={() => onLocate(['messages', 0])}
                   locateLabel={t('trace.locate')}>
            <div className="grid gap-1 sm:grid-cols-2">
              <ValueCell label={t('trace.diffBefore')} value={diff.system.before} tone="bad" long />
              <ValueCell label={t('trace.diffAfter')} value={diff.system.after} tone="good" long />
            </div>
          </DiffRow>
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
              <DiffRow key={`a${m.index}`} title={`+ ${m.role}`} index={m.index}
                       tone="good" onLocate={() => onLocate(['messages', m.index])}
                       locateLabel={t('trace.locate')}>
                <p className="whitespace-pre-wrap break-words">{m.preview}</p>
              </DiffRow>
            ))}
            {diff.messages.removed.map((m) => (
              <DiffRow key={`r${m.index}`} title={`- ${m.role}`} index={m.index}
                       tone="bad" onLocate={() => onLocate(['messages', m.index])}
                       locateLabel={t('trace.locate')}>
                <p className="whitespace-pre-wrap break-words">{m.preview}</p>
              </DiffRow>
            ))}
            {diff.messages.changed.map((m) => (
              <DiffRow key={`c${m.index}`} title={`~ ${m.role}`} index={m.index}
                       onLocate={() => onLocate(['messages', m.index])}
                       locateLabel={t('trace.locate')}>
                <div className="grid gap-1">
                  <p className="whitespace-pre-wrap break-words text-red-600 line-through decoration-red-400/50">
                    {m.before}
                  </p>
                  <p className="whitespace-pre-wrap break-words text-emerald-700 dark:text-emerald-400">
                    {m.after}
                  </p>
                </div>
              </DiffRow>
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
              <Badge key={`ta${n}`} className="bg-emerald-600/15 text-[10px] text-emerald-700">+ {n}</Badge>
            ))}
            {diff.tools.removed.map((n) => (
              <Badge key={`tr${n}`} className="bg-red-600/15 text-[10px] text-red-600">- {n}</Badge>
            ))}
            {diff.tools.changed.map((x) => (
              <button
                key={`tc${x.name}`}
                type="button"
                onClick={() => onLocate(['tools'])}
                className="rounded-full border px-1.5 text-[10px] hover:bg-muted"
                title={t('trace.locate')}
              >
                ~ {x.name}
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

/** 差异里的一行：默认是摘要，点开看完整值；右侧可「定位」到请求体。 */
function DiffRow({title, index, tone, onLocate, locateLabel, children}: {
  title: string;
  index?: number;
  tone?: 'good' | 'bad';
  onLocate: () => void;
  locateLabel: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <li className={cn('rounded-md border p-1.5',
      tone === 'good' && 'border-emerald-500/30',
      tone === 'bad' && 'border-red-500/30',
      !tone && 'border-border')}>
      <div className="flex items-center gap-2">
        <button type="button" onClick={() => setOpen((v) => !v)}
                className="flex min-w-0 flex-1 items-center gap-1 text-left">
          <ChevronRight className={cn('h-3 w-3 shrink-0 text-muted-foreground transition-transform',
            open && 'rotate-90')} />
          <span className="truncate font-mono text-[11px]">{title}</span>
          {index !== undefined && (
            <span className="shrink-0 text-[10px] text-muted-foreground">#{index}</span>
          )}
        </button>
        <button type="button" onClick={onLocate}
                className="shrink-0 rounded-full border px-1.5 text-[10px] text-muted-foreground hover:bg-muted">
          {locateLabel}
        </button>
      </div>
      {open && <div className="mt-1 pl-4 text-[11px]">{children}</div>}
    </li>
  );
}

/** 差异里的「改前 / 改后」单元格。 */
function ValueCell({label, value, tone, long}: {
  label: string; value: string | null; tone: 'good' | 'bad'; long?: boolean;
}) {
  return (
    <div className="min-w-0 rounded bg-muted/40 p-1">
      <span className="block text-[10px] text-muted-foreground">{label}</span>
      <span className={cn('block break-words font-mono',
        long && 'max-h-32 overflow-auto whitespace-pre-wrap',
        tone === 'bad' ? 'text-red-600' : 'text-emerald-700 dark:text-emerald-400')}>
        {value ?? '—'}
      </span>
    </div>
  );
}


/** 只读文本块（重建结果、原文等）。 */
function Block({title, body}: {title: string; body: string}) {
  return (
    <section>
      <h4 className="mb-1 text-xs font-medium">{title}</h4>
      <pre className={BLOCK_SCROLL + " whitespace-pre-wrap break-words rounded-md bg-muted/40 p-2 text-[11px]"}>
        {body}
      </pre>
    </section>
  );
}

/** 任意值 → 缩进 JSON 文本（Raw 视图与导出都用它）。 */
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
 * 每根条的三段含义与线上一致：起点 = 这一轮开始处理的时刻；宽度 = 总耗时；
 * 条内左段 = 首字等待，右段 = 生成。没有首字延迟（非流式）的轮次整条按生成色画，
 * **不**假设一个首字值。
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
              <span className={cn('h-full', tone === 'error' ? 'bg-red-500/70' : 'bg-amber-500/70')}
                    style={{width: `${(ttft / lat) * 100}%`}} />
              <span className={cn('h-full flex-1', tone === 'error' ? 'bg-red-500/40' : 'bg-primary/60')} />
            </button>
          );
        })}
      </div>
    </div>
  );
}

'use client';

import {useCallback, useEffect, useState} from 'react';
import {
  Activity,
  Copy,
  Globe,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  Users,
} from 'lucide-react';
import {accountApi, errText, proxyApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {RichText} from '@/lib/i18n/rich-text';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {ProxyProbe, ProxyRoute, ProxySettings, ProxyPool, RouteAccount} from '@/lib/types';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {Textarea} from '@/components/ui/textarea';
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
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';

/**
 * 「设置 → 代理」：代理池 + 权重 + 绑定 + 全局兜底。
 *
 * 结构与 Antigravity-Manager 的代理池一致（那边也是这张表 + 一行池配置 + 全局兜底）：
 *   · 池开关 + 策略（优先级 / 轮询 / 随机 / 最少调用）+ 间隔（多久重新挑一条出口）；
 *   · 每条线路：权重、启用开关、地址（脱敏）、绑定账号、操作（测试 / 编辑 / 删除）；
 *   · **账号最多绑一条线路**，不绑 = 跟随池策略（「绑定管理」里逐行改）；
 *   · 「全局上游代理」是池不可用时的兜底出口（开关 + 地址）。
 */
export function ProxyPanel() {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const [routes, setRoutes] = useState<ProxyRoute[]>([]);
  const [pool, setPool] = useState<ProxyPool | null>(null);
  const [settings, setSettings] = useState<ProxySettings | null>(null);
  const [probes, setProbes] = useState<Record<string, ProxyProbe>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState<string | null>(null);
  const [form, setForm] = useState({name: '', url: '', weight: '100', enabled: true});
  const [globalForm, setGlobalForm] = useState({http: '', update: '', enabled: true});
  const [query, setQuery] = useState('');
  const [intervalText, setIntervalText] = useState<string | null>(null);
  const [bindOpen, setBindOpen] = useState(false);
  const [bindLoading, setBindLoading] = useState(false);
  const [accounts, setAccounts] = useState<RouteAccount[]>([]);
  const [autoFor, setAutoFor] = useState<ProxyRoute | null>(null);
  const [autoCount, setAutoCount] = useState('10');

  const load = useCallback(async () => {
    try {
      const [list, cfg] = await Promise.all([proxyApi.routes(), proxyApi.settings()]);
      setRoutes(list.routes);
      setPool(list.pool);
      setSettings(cfg);
      setGlobalForm({
        http: cfg.http_configured ? cfg.http_proxy : '',
        update: cfg.update_configured ? cfg.update_proxy : '',
        enabled: cfg.global_proxy_enabled,
      });
    } catch (e) {
      notify.err(errText(e));
    }
  }, []);

  useEffect(() => {
    // 非管理员不拉列表：接口都是 require_session_admin（脱敏串里带着代理用户名），
    // 拉了只会吃 403 并弹一条没有可行动信息的报错（与其它设置面板一致）。
    if (isAdmin) void load();
  }, [load, isAdmin]);

  async function savePool(patch: Partial<ProxyPool>) {
    if (!pool) return;
    setSaving(true);
    try {
      setPool(await proxyApi.savePool({...pool, ...patch}));
      notify.ok(t('settings.proxySaved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  async function submitRoute() {
    const url = form.url.trim();
    if (!url) {
      notify.warn(t('settings.proxyUrlRequired'));
      return;
    }
    setSaving(true);
    try {
      const body = {url, weight: Number(form.weight || 100), enabled: form.enabled};
      if (editing) await proxyApi.update(editing, body);
      else await proxyApi.create(form.name.trim(), url, body.weight, form.enabled);
      notify.ok(t('settings.proxySaved'));
      setEditing(null);
      setCreating(false);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  async function probe(name: string) {
    setTesting(name);
    try {
      const result = await proxyApi.probe(name);
      setProbes((prev) => ({...prev, [name]: result}));
      if (result.ok) {
        notify.ok(t('settings.proxyTestOk', {ms: String(result.ms)}),
                  result.exit_info || result.exit_ip || '');
      } else {
        notify.err(t('settings.proxyTestFail'), result.error || '');
      }
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setTesting(null);
    }
  }

  async function copyRoute(route: ProxyRoute) {
    // 复制的是脱敏串：密码不进剪贴板（要完整地址得自己回上游 config.json 看）
    try {
      await navigator.clipboard.writeText(route.masked);
      notify.ok(t('settings.proxyCopied'));
    } catch (e) {
      notify.err(errText(e));
    }
  }

  async function remove(name: string, force: boolean) {
    try {
      const result = await proxyApi.remove(name, force);
      notify.ok(t('settings.proxyDeleted'), result.bound_cleared
        ? t('settings.proxyDeletedBound', {count: String(result.bound_cleared)}) : '');
      await load();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  async function runImport() {
    if (!importing?.trim()) return;
    setSaving(true);
    try {
      const result = await proxyApi.import(importing);
      notify.ok(t('settings.proxyImportDone', {count: String(result.imported)}),
                result.problems.length
                  ? t('settings.proxyImportProblems', {count: String(result.problems.length)})
                  : '');
      setImporting(null);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  async function saveGlobal() {
    setSaving(true);
    try {
      const next = await proxyApi.saveSettings({
        http_proxy: globalForm.http.trim(),
        update_proxy: globalForm.update.trim(),
        global_proxy_enabled: globalForm.enabled,
      });
      setSettings(next);
      setGlobalForm({
        http: next.http_configured ? next.http_proxy : '',
        update: next.update_configured ? next.update_proxy : '',
        enabled: next.global_proxy_enabled,
      });
      notify.ok(t('settings.proxySaved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  async function refreshBindings(): Promise<boolean> {
    setBindLoading(true);
    try {
      setAccounts((await proxyApi.bindings()).accounts);
      return true;
    } catch (e) {
      notify.err(errText(e));
      return false;
    } finally {
      setBindLoading(false);
    }
  }

  async function openBindings() {
    setBindOpen(true);
    setAccounts([]);
    // 失败才关窗：列表没拿到时留在原地，用户看得到错误提示，不会以为「没有账号」。
    if (!(await refreshBindings())) setBindOpen(false);
  }

  async function assign(account: RouteAccount, value: string) {
    try {
      await accountApi.setProxy(account.file, value === '__pool__' ? '' : value);
      setAccounts((prev) => prev.map((a) => (a.file === account.file
        ? {...a, proxy: value === '__pool__' ? '' : value} : a)));
      notify.ok(t('settings.proxySaved'));
      await load();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  async function autoAssign() {
    if (!autoFor) return;
    const count = Number(autoCount || 0);
    if (!Number.isInteger(count) || count <= 0) {
      notify.warn(t('settings.proxyAutoAssignBadCount'));
      return;
    }
    setSaving(true);
    try {
      const result = await proxyApi.autoAssign(autoFor.name, count);
      notify.ok(t('settings.proxyAutoAssignDone', {count: String(result.updated)}),
                t('settings.proxyAutoAssignLeft', {count: String(result.unbound_left)}));
      setAutoFor(null);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  if (!isAdmin) {
    return (
      <div className="rounded-[20px] bg-muted p-8 text-center text-sm">
        <div className="font-medium">{t('settings.proxyAdminRequired')}</div>
        <div className="mx-auto mt-2 max-w-[38rem] text-xs text-muted-foreground">
          {t('settings.proxyAdminRequiredDesc')}
        </div>
      </div>
    );
  }

  const shown = routes.filter((r) => {
    const needle = query.trim().toLowerCase();
    return !needle || r.name.toLowerCase().includes(needle)
      || r.masked.toLowerCase().includes(needle)
      || (r.bound_names || []).some((n) => n.toLowerCase().includes(needle));
  });
  // 显式映射（不是拼驼峰）：`round_robin` → Round_robin 会拼出一个不存在的
  // 键名，界面上直接显示 `settings.proxyStrategyRound_robin`（两种策略中招）。
  const STRATEGY_KEY: Record<string, string> = {
    priority: 'settings.proxyStrategyPriority',
    round_robin: 'settings.proxyStrategyRoundRobin',
    random: 'settings.proxyStrategyRandom',
    least_connections: 'settings.proxyStrategyLeastConnections',
  };
  const strategyKey = (s: string) => STRATEGY_KEY[s] ?? s;
  const sourceKey = settings?.source === 'off' ? 'settings.proxyGlobalSourceOff'
    : settings?.source === 'panel' ? 'settings.proxyGlobalSourcePanel'
      : settings?.source === 'env' ? 'settings.proxyGlobalSourceEnv'
        : 'settings.proxyGlobalSourceNone';

  return (
    <div className="space-y-4">
      {/* ── 池配置 + 线路表 ── */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <span className="text-sm font-medium">{t('settings.proxyRoutesTitle')}</span>
          {pool && (
            <>
              <Switch checked={pool.enabled}
                      aria-label={t('settings.proxyRoutesTitle')}
                      onCheckedChange={(v) => void savePool({enabled: v})} />
              <Select value={pool.strategy} onValueChange={(v) => void savePool({strategy: v as ProxyPool['strategy']})}>
                <SelectTrigger className="h-8 w-32 rounded-full bg-background text-xs" aria-label={t('settings.proxyStrategyLabel')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {pool.strategies.map((s) => (
                    <SelectItem key={s} value={s}>{t(strategyKey(s))}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <RefreshCw className="h-3.5 w-3.5" />
                <Input className="h-8 w-20 rounded-full bg-background text-right text-xs tabular-nums"
                       value={intervalText ?? String(pool.interval_seconds)}
                       onChange={(e) => setIntervalText(e.target.value)}
                       onBlur={() => {
                         // 清空或填了非法值就还原成当前值：这里**不能**把空串当 0 ——
                         // 0 的语义是「每次请求都重新挑出口」，误触会让出口一直变。
                         const raw = (intervalText ?? '').trim();
                         const value = Number(raw);
                         setIntervalText(null);
                         if (!raw || !Number.isInteger(value) || value < 0 || value > 86400
                             || value === pool.interval_seconds) {
                           return;
                         }
                         void savePool({interval_seconds: value});
                       }} />
                {t('settings.proxyIntervalUnit')}
              </span>
              <span className="text-[11px] text-muted-foreground">
                {t('settings.proxyPoolEnabledRoutes', {enabled: String(pool.routes_enabled),
                                                       total: String(pool.routes_total)})}
              </span>
            </>
          )}
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input className="h-8 w-40 rounded-full bg-background pl-8 text-xs"
                     value={query} placeholder={t('settings.proxySearch')}
                     onChange={(e) => setQuery(e.target.value)} />
            </div>
            <Button variant="outline" size="sm" className="h-8 rounded-full"
                    onClick={() => void openBindings()}>
              <Users className="mr-1.5 h-3.5 w-3.5" />
              {t('settings.proxyManageBindings')}
            </Button>
            <Button variant="outline" size="sm" className="h-8 rounded-full"
                    onClick={() => setImporting('')}>
              <Upload className="mr-1.5 h-3.5 w-3.5" />
              {t('settings.proxyImport')}
            </Button>
            <Button size="sm" className="h-8 rounded-full"
                    onClick={() => {
                      setForm({name: '', url: '', weight: '100', enabled: true});
                      setCreating(true);
                    }}>
              <Plus className="mr-1.5 h-3.5 w-3.5" />
              {t('settings.proxyAdd')}
            </Button>
          </div>
        </div>
        <div className="mb-3 text-[11px] leading-4 text-muted-foreground">
          <RichText text={t('settings.proxyRoutesDesc')} />
        </div>

        {shown.length === 0 ? (
          routes.length === 0 ? (
            <EmptyState icon={Globe} title={t('settings.proxyEmpty')}
                        description={t('settings.proxyEmptyDesc')} />
          ) : (
            // 有线路但被搜索过滤光了：不能沿用「还没有配置任何线路」，
            // 否则用户会以为线路被删了。
            <EmptyState icon={Search} title={t('settings.proxyNoMatch')}
                        description={t('settings.proxyNoMatchDesc', {query: query.trim()})}>
              <Button variant="outline" size="sm" className="rounded-full"
                      onClick={() => setQuery('')}>
                {t('settings.proxyClearSearch')}
              </Button>
            </EmptyState>
          )
        ) : (
          <div className="overflow-hidden rounded-2xl bg-background/60">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-20 text-xs">{t('settings.proxyWeight')}</TableHead>
                  <TableHead className="w-24 text-xs">{t('settings.proxyColStatus')}</TableHead>
                  <TableHead className="text-xs">{t('settings.proxyUrl')}</TableHead>
                  <TableHead className="w-56 text-xs">{t('settings.proxyBound')}</TableHead>
                  <TableHead className="w-28 pr-4 text-right text-xs">
                    {t('settings.proxyColActions')}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {shown.map((route) => {
                  const result = probes[route.name] || (route.probe
                    ? {ok: route.probe.ok, ms: route.probe.ms, error: route.probe.error} : undefined);
                  return (
                    <TableRow key={route.name}>
                      <TableCell className="text-xs tabular-nums text-muted-foreground">
                        {route.weight}
                      </TableCell>
                      <TableCell className="text-[11px]">
                        {testing === route.name ? (
                          <span className="flex items-center gap-1.5 text-muted-foreground">
                            <Loader2 className="h-3 w-3 animate-spin" />{t('settings.proxyTesting')}
                          </span>
                        ) : result ? (
                          <span className={result.ok
                            ? 'flex items-center gap-1.5 text-emerald-600 dark:text-emerald-400'
                            : 'flex items-center gap-1.5 text-red-600 dark:text-red-400'}
                                title={result.error || ''}>
                            <span className={`h-1.5 w-1.5 rounded-full ${result.ok
                              ? 'bg-emerald-500' : 'bg-red-500'}`} />
                            {result.ok ? `${result.ms} ms` : t('settings.proxyTestFail')}
                          </span>
                        ) : (
                          <span className="flex items-center gap-1.5 text-muted-foreground">
                            <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/40" />
                            {t('settings.proxyProbeNever')}
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="max-w-[24rem]">
                        <span className="flex items-center gap-1.5">
                          <span className={'truncate font-mono text-[11px] ' + (route.enabled
                            ? 'text-muted-foreground' : 'text-muted-foreground/50 line-through')}>
                            {route.name} · {route.masked}
                          </span>
                          {!route.enabled && (
                            <Badge variant="secondary" className="text-[10px]">
                              {t('settings.proxyDisabledBadge')}
                            </Badge>
                          )}
                          {route.local && (
                            <Badge variant="secondary" className="text-[10px]">
                              {t('settings.proxyLocalBadge')}
                            </Badge>
                          )}
                          {route.shared_warning && (
                            <Badge className="bg-amber-500/15 text-[10px] text-amber-700 dark:text-amber-300"
                                   title={t('settings.proxySharedWarning', {count: String(route.bound)})}>
                              {t('settings.proxySharedBadge')}
                            </Badge>
                          )}
                          <Button variant="ghost" size="icon" className="h-6 w-6 rounded-md"
                                  title={t('settings.proxyCopy')}
                                  onClick={() => void copyRoute(route)}>
                            <Copy className="h-3 w-3" />
                          </Button>
                        </span>
                      </TableCell>
                      <TableCell>
                        <span className="flex flex-wrap items-center gap-1">
                          {(route.bound_names || []).map((n) => (
                            <Badge key={n} variant="secondary" className="text-[10px]">{n}</Badge>
                          ))}
                          {route.bound > (route.bound_names || []).length && (
                            <Badge variant="outline" className="text-[10px]">
                              +{route.bound - (route.bound_names || []).length}
                            </Badge>
                          )}
                          {!route.bound && (
                            <span className="text-[11px] text-muted-foreground">
                              {t('settings.proxyNoBindings')}
                            </span>
                          )}
                        </span>
                      </TableCell>
                      <TableCell className="pr-4">
                        <div className="flex justify-end gap-1">
                          <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md"
                                  title={t('settings.proxyAutoAssign')}
                                  onClick={() => {
                                    setAutoFor(route);
                                    setAutoCount('10');
                                    // 「可分配 N 台」来自账号绑定列表：没打开过绑定管理时
                                    // accounts 还是空的，这里补拉一次，否则永远显示 0。
                                    if (!accounts.length) void refreshBindings();
                                  }}>
                            <Users className="h-3.5 w-3.5" />
                          </Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md"
                                  title={t('settings.proxyTest')}
                                  disabled={testing === route.name}
                                  onClick={() => void probe(route.name)}>
                            <Activity className="h-3.5 w-3.5" />
                          </Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md"
                                  title={t('common.edit')}
                                  onClick={() => {
                                    setForm({name: route.name, url: route.masked,
                                             weight: String(route.weight), enabled: route.enabled});
                                    setEditing(route.name);
                                  }}>
                            <Pencil className="h-3.5 w-3.5" />
                          </Button>
                          <ConfirmDialog destructive
                                         title={t('settings.proxyDeleteTitle', {name: route.name})}
                                         description={route.bound
                                           ? t('settings.proxyDeleteBoundWarning',
                                               {count: String(route.bound)})
                                           : t('settings.proxyDeleteDesc')}
                                         confirmText={route.bound
                                           ? t('settings.proxyDeleteForce') : t('common.delete')}
                                         onConfirm={() => remove(route.name, route.bound > 0)}
                                         trigger={
                                           <Button variant="ghost" size="icon"
                                                   className="h-7 w-7 rounded-md"
                                                   title={t('common.delete')}>
                                             <Trash2 className="h-3.5 w-3.5" />
                                           </Button>
                                         } />
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      {/* ── 全局上游代理（池不可用时的兜底）── */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-1 flex items-center gap-2">
          <span className="text-sm font-medium">{t('settings.proxyGlobalTitle')}</span>
          <Switch checked={globalForm.enabled}
                  aria-label={t('settings.proxyGlobalTitle')}
                  onCheckedChange={(v) => setGlobalForm({...globalForm, enabled: v})} />
        </div>
        <div className="mb-3 text-[11px] leading-4 text-muted-foreground">
          <RichText text={t('settings.proxyGlobalDesc')} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Input className="h-9 min-w-[16rem] flex-1 bg-background font-mono text-xs"
                 placeholder={t('settings.proxyGlobalPlaceholder')}
                 value={globalForm.http}
                 onChange={(e) => setGlobalForm({...globalForm, http: e.target.value})} />
          <Button className="rounded-full" disabled={saving} onClick={() => void saveGlobal()}>
            {t('common.save')}
          </Button>
          <Button variant="outline" className="rounded-full"
                  disabled={testing === '__global__'}
                  onClick={async () => {
                    setTesting('__global__');
                    try {
                      const result = await proxyApi.probeGlobal();
                      notify[result.ok ? 'ok' : 'err'](
                        result.ok ? t('settings.proxyTestOk', {ms: String(result.ms)})
                          : t('settings.proxyTestFail'),
                        result.exit_info || result.error || '');
                    } catch (e) {
                      notify.err(errText(e));
                    } finally {
                      setTesting(null);
                    }
                  }}>
            {testing === '__global__'
              ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              : <Activity className="mr-1.5 h-3.5 w-3.5" />}
            {t('settings.proxyTest')}
          </Button>
        </div>
        <div className="mt-2 text-[11px] leading-4 text-muted-foreground">
          {t(sourceKey, {value: settings?.effective || ''})}
        </div>
      </div>

      {/* ── 更新下载代理 ── */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-1 text-sm font-medium">{t('settings.proxyUpdateTitle')}</div>
        <div className="mb-3 text-[11px] leading-4 text-muted-foreground">
          <RichText text={t('settings.proxyUpdateDesc')} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Input className="h-9 min-w-[16rem] flex-1 bg-background font-mono text-xs"
                 placeholder="http://user:pass@host:7890"
                 value={globalForm.update}
                 onChange={(e) => setGlobalForm({...globalForm, update: e.target.value})} />
          <Button className="rounded-full" disabled={saving} onClick={() => void saveGlobal()}>
            {t('common.save')}
          </Button>
        </div>
        {settings?.env_update_proxy ? (
          <div className="mt-2 text-[11px] leading-4 text-muted-foreground">
            {t('settings.proxyUpdateEnv', {value: settings.env_update_proxy})}
          </div>
        ) : null}
      </div>

      {/* ── 说明 ── */}
      <div className="rounded-[20px] bg-muted p-4 text-[11px] leading-5 text-muted-foreground">
        <RichText text={t('settings.proxyScopeNote')} />
      </div>

      {/* 新增 / 编辑线路 */}
      <Dialog open={creating || editing !== null}
              onOpenChange={(open) => {
                if (!open) {
                  setCreating(false);
                  setEditing(null);
                }
              }}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>{editing ? t('settings.proxyEdit') : t('settings.proxyAdd')}</DialogTitle>
            <DialogDescription>{t('settings.proxyDialogDesc')}</DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-2.5">
            <div className="rounded-2xl bg-muted/40 px-3 py-2">
              <Label className="text-xs">{t('settings.proxyName')}</Label>
              <Input className="mt-1 h-8 bg-background" value={form.name}
                     disabled={editing !== null}
                     placeholder={t('settings.proxyNamePlaceholder')}
                     onChange={(e) => setForm({...form, name: e.target.value})} />
            </div>
            <div className="rounded-2xl bg-muted/40 px-3 py-2">
              <Label className="text-xs">{t('settings.proxyUrl')}</Label>
              <Input className="mt-1 h-8 bg-background font-mono text-xs" value={form.url}
                     placeholder="socks5://user:pass@host:10000"
                     onChange={(e) => setForm({...form, url: e.target.value})} />
              <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                {t('settings.proxyUrlHint')}
              </p>
            </div>
            <div className="flex items-center gap-3 rounded-2xl bg-muted/40 px-3 py-2">
              <div className="flex-1">
                <Label className="text-xs">{t('settings.proxyWeight')}</Label>
                <Input className="mt-1 h-8 w-28 bg-background text-right tabular-nums"
                       value={form.weight}
                       onChange={(e) => setForm({...form, weight: e.target.value})} />
              </div>
              <div className="flex items-center gap-2 pt-4">
                <Switch checked={form.enabled}
                        aria-label={t('settings.proxyInPool')}
                        onCheckedChange={(v) => setForm({...form, enabled: v})} />
                <span className="text-xs text-muted-foreground">
                  {t('settings.proxyInPool')}
                </span>
              </div>
            </div>
            <p className="text-[11px] leading-4 text-muted-foreground">
              {t('settings.proxyWeightHint')}
            </p>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" className="rounded-full"
                    onClick={() => {
                      setCreating(false);
                      setEditing(null);
                    }}>
              {t('common.cancel')}
            </Button>
            <Button className="rounded-full" disabled={saving} onClick={() => void submitRoute()}>
              {t('common.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 导入 */}
      <Dialog open={importing !== null} onOpenChange={(open) => !open && setImporting(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{t('settings.proxyImportTitle')}</DialogTitle>
            <DialogDescription>{t('settings.proxyImportDesc')}</DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="rounded-2xl bg-muted/40 px-3 py-2">
              <Textarea className="min-h-40 bg-background font-mono text-xs"
                        value={importing ?? ''}
                        placeholder={t('settings.proxyImportPlaceholder')}
                        onChange={(e) => setImporting(e.target.value)} />
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" className="rounded-full" onClick={() => setImporting(null)}>
              {t('common.cancel')}
            </Button>
            <Button className="rounded-full" disabled={saving} onClick={() => void runImport()}>
              {t('settings.proxyImportRun')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 自动分配 N 台 */}
      <Dialog open={autoFor !== null} onOpenChange={(open) => !open && setAutoFor(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>
              {t('settings.proxyBindingsTitle', {name: autoFor?.name || ''})}
            </DialogTitle>
            <DialogDescription>{t('settings.proxyAutoAssignDesc')}</DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-2.5">
            <div className="flex items-center gap-2 rounded-2xl bg-muted/40 px-3 py-2">
              <span className="text-xs font-medium">{t('settings.proxyAutoAssign')}</span>
              <Input className="h-8 w-20 rounded-full bg-background text-center text-xs tabular-nums"
                     value={autoCount} onChange={(e) => setAutoCount(e.target.value)} />
              <span className="text-xs text-muted-foreground">
                {t('settings.proxyAutoAssignUnit')}
              </span>
            </div>
            <p className="text-[11px] leading-4 text-muted-foreground">
              {bindLoading
                ? t('settings.proxyBindingsLoading')
                : t('settings.proxyAutoAssignHint', {count: String(
                  accounts.filter((a) => !a.proxy).length)})}
            </p>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" className="rounded-full" onClick={() => setAutoFor(null)}>
              {t('common.cancel')}
            </Button>
            <Button className="rounded-full" disabled={saving} onClick={() => void autoAssign()}>
              {t('settings.proxyAutoAssignRun')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 绑定管理：逐行「默认（跟随策略）/ 某条线路」 */}
      <Dialog open={bindOpen} onOpenChange={(open) => !open && setBindOpen(false)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t('settings.proxyBindingsManagerTitle')}</DialogTitle>
            <DialogDescription>{t('settings.proxyBindingsManagerDesc')}</DialogDescription>
          </DialogHeader>
          <DialogBody className="max-h-[min(26rem,calc(100dvh-12rem))] space-y-1.5 overflow-y-auto">
            {bindLoading ? (
              <div className="flex items-center justify-center gap-2 rounded-2xl bg-muted/40 py-8 text-xs text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                {t('settings.proxyBindingsLoading')}
              </div>
            ) : accounts.length === 0 ? (
              <div className="rounded-2xl bg-muted/40 py-8 text-center text-xs text-muted-foreground">
                {t('settings.proxyBindingsEmpty')}
              </div>
            ) : accounts.map((row) => (
              <div key={row.file}
                   className="flex items-center gap-3 rounded-xl bg-muted/40 px-3 py-2">
                <span className="min-w-0 flex-1 truncate text-xs">
                  {row.nickname || row.uid}
                </span>
                <Select value={row.proxy || '__pool__'}
                        onValueChange={(v) => void assign(row, v)}>
                  <SelectTrigger className="h-8 w-56 rounded-full bg-background text-xs" aria-label={t('settings.proxyBindingFor', {name: row.nickname || row.uid})}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__pool__">{t('settings.proxyFollowPool')}</SelectItem>
                    <SelectItem value="__direct__">{t('settings.proxyDirectOnly')}</SelectItem>
                    {routes.map((r) => (
                      <SelectItem key={r.name} value={r.name}>{r.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            ))}
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" className="rounded-full" onClick={() => setBindOpen(false)}>
              {t('common.close')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

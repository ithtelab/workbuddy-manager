'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  DownloadCloud,
  Globe,
  Loader2,
  RefreshCw,
  RotateCcw,
  Server,
  ShieldCheck,
  Sparkles,
  Terminal,
  Upload,
  Trash2,
  X,
  XCircle,
} from 'lucide-react';
import {errText, systemApi} from '@/lib/api';
import type {AutoUpdateConfig, AutoUpdateInfo, GithubMirror, UpdateCheck, UpdatePackageMeta, UpdateStatus} from '@/lib/types';
import {notify} from '@/lib/toast';
import {fmtAgo, fmtDateTime} from '@/lib/format';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
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
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {LoadError} from '@/components/common/states/LoadError';
import {RichText} from '@/lib/i18n/rich-text';
import {useT} from '@/lib/i18n/provider';

/** 更新对象说明（文案走 i18n 键），用于确认弹窗与按钮文案 */
const TARGETS = [
  {
    id: 'both' as const,
    labelKey: 'updatePanel.targetBoth',
    descKey: 'updatePanel.targetBothDesc',
    hintKey: 'updatePanel.targetBothHint',
  },
  {
    id: 'manager' as const,
    labelKey: 'updatePanel.targetManager',
    descKey: 'updatePanel.targetManagerDesc',
    hintKey: 'updatePanel.targetManagerHint',
  },
];

/** 把检测结果拼成一句可读摘要，用于提醒 */
function describeAvailable(c: UpdateCheck, t: (key: string, params?: Record<string, string | number>) => string): string {
  return c.manager.has_update ? t('update.managerVersion', {v: c.manager.latest}) : '';
}

export function UpdatePanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [versions, setVersions] = useState<{manager: string; upstream_connected: boolean; upstream_accounts: number | null} | null>(null);
  const [check, setCheck] = useState<UpdateCheck | null>(null);
  const [checking, setChecking] = useState(false);
  /**
   * 结果横幅是否已被关闭。用「是否正在运行」的边沿来复位：
   * 状态从 running 变为结束、或再次发起更新时会重新出现，
   * 不依赖 finished_at 是否存在，避免时间戳缺失时关不掉。
   */
  const [resultDismissed, setResultDismissed] = useState(false);
  /** 离线升级包（issue #153）：已上传的包 + 上传在途状态 + 用户填的发布页摘要 */
  const [pkg, setPkg] = useState<UpdatePackageMeta | null>(null);
  const [pkgBusy, setPkgBusy] = useState(false);
  const [pkgSha, setPkgSha] = useState('');
  /** 清除上次更新记录（issue #105）在途状态 */
  const [clearBusy, setClearBusy] = useState(false);
  /** 四份数据**全部**没取到：版本与「有没有新版本」都不能下结论（服务可能正在重启） */
  const [loadFailed, setLoadFailed] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      const [st, vs, ck, pk] = await Promise.allSettled([
        systemApi.updateStatus(),
        systemApi.versions(),
        systemApi.checkUpdate(),
        systemApi.packageMeta(),
      ]);
      if (pk.status === 'fulfilled') setPkg(pk.value);
      if (st.status === 'fulfilled') setStatus(st.value);
      if (vs.status === 'fulfilled') setVersions(vs.value);
      if (ck.status === 'fulfilled') setCheck(ck.value);
      // 全失败才报错：更新期间服务重启，单份失败是瞬时的；但四份都没到，
      // 页面上「版本 — / 无更新」就是在编造结论。
      setLoadFailed([st, vs, ck, pk].every((x) => x.status === 'rejected'));
    } catch {
      /* 更新期间服务重启，忽略瞬时失败 */
    }
  }, []);

  /**
   * 上传离线升级包。
   *
   * 前端只做「选文件 + 填发布页摘要」，核对与落盘都在服务端。摘要在服务端默认
   * 必填：安装包会在本机执行包内代码，没有摘要就无法确认包没被替换；确实拿不到
   * 摘要的离线环境由服务端显式设置 WB_ALLOW_UNVERIFIED_PACKAGE=1 放行。
   */
  const uploadPackage = useCallback(async (file: File) => {
    setPkgBusy(true);
    try {
      const meta = await systemApi.uploadPackage(file, pkgSha.trim());
      setPkg(meta);
      notify.ok(t('updatePanel.pkgUploaded', {version: meta.version || ''}),
                t('updatePanel.pkgUploadedDesc', {mb: String(Math.round((meta.size || 0) / 1024 / 1024))}));
      setPkgSha('');
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setPkgBusy(false);
    }
  }, [pkgSha, t]);

  const startFromPackage = useCallback(async (target: 'manager' | 'both') => {
    setBusy(true);
    try {
      const r = await systemApi.startUpdate(target, 'uploaded');
      notify.ok(t('updatePanel.updateStarted'), r.message);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }, [load, t]);

  const dropPackage = useCallback(async () => {
    setPkgBusy(true);
    try {
      await systemApi.deletePackage();
      setPkg({present: false});
      notify.ok(t('updatePanel.pkgCleared'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setPkgBusy(false);
    }
  }, [t]);

  /** GitHub Token（版本检测 / 一键更新的认证，issue #205）：只回掩码 */
  const [gh, setGh] = useState<{configured: boolean; masked: string; source: 'env' | 'panel' | ''} | null>(null);
  const [ghInput, setGhInput] = useState('');
  const [ghBusy, setGhBusy] = useState(false);
  /** GitHub 镜像（issue #216）：国内机器直连超时时用它加速 */
  const [mirror, setMirror] = useState<GithubMirror | null>(null);
  const [mirrorInput, setMirrorInput] = useState('');
  const [mirrorBusy, setMirrorBusy] = useState(false);

  /** 自动更新：定时检测，在时间窗内自动执行一键更新 */
  const [auto, setAuto] = useState<AutoUpdateInfo | null>(null);
  const [autoForm, setAutoForm] = useState<AutoUpdateConfig | null>(null);
  const [autoBusy, setAutoBusy] = useState(false);

  useEffect(() => {
    systemApi.autoUpdate().then((a) => {
      setAuto(a);
      setAutoForm({
        enabled: a.enabled, interval_hours: a.interval_hours,
        window_start: a.window_start, window_end: a.window_end, target: a.target,
      });
    }).catch(() => setAuto(null));
  }, []);

  const saveAuto = useCallback(async (next: AutoUpdateConfig) => {
    setAutoBusy(true);
    try {
      const r = await systemApi.saveAutoUpdate(next);
      setAuto(r);
      setAutoForm({
        enabled: r.enabled, interval_hours: r.interval_hours,
        window_start: r.window_start, window_end: r.window_end, target: r.target,
      });
      notify.ok(r.enabled ? t('updatePanel.autoSavedOn') : t('updatePanel.autoSavedOff'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setAutoBusy(false);
    }
  }, [t]);

  const autoDirty = useMemo(() => !!auto && !!autoForm && (
    auto.interval_hours !== autoForm.interval_hours
    || auto.window_start !== autoForm.window_start
    || auto.window_end !== autoForm.window_end
    || auto.target !== autoForm.target
  ), [auto, autoForm]);

  useEffect(() => {
    systemApi.githubToken().then(setGh).catch(() => setGh(null));
    systemApi.githubMirror().then((m) => {
      setMirror(m);
      setMirrorInput(m.stored);
    }).catch(() => setMirror(null));
  }, []);

  const saveGh = useCallback(async (token: string) => {
    setGhBusy(true);
    try {
      const r = await systemApi.saveGithubToken(token);
      setGh({...r, source: r.configured ? 'panel' : ''});
      setGhInput('');
      notify.ok(token ? t('updatePanel.ghSaved') : t('updatePanel.ghCleared'));
      await recheck();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setGhBusy(false);
    }
  }, [t]);

  const saveMirror = useCallback(async (value: string) => {
    setMirrorBusy(true);
    try {
      const r = await systemApi.saveGithubMirror(value);
      setMirror(r);
      setMirrorInput(r.stored);
      notify.ok(value.trim() ? t('updatePanel.mirrorSaved') : t('updatePanel.mirrorCleared'));
      // 换了取版本的路子，缓存里的结论就不作数了
      await recheck();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setMirrorBusy(false);
    }
  }, [t]);

  /** 强制重新检测（绕过服务端 6 小时缓存） */
  const recheck = useCallback(async () => {
    setChecking(true);
    try {
      const r = await systemApi.checkUpdate(true);
      setCheck(r);
      if (r.has_any) {
        notify.warn(t('updatePanel.newVersion'), describeAvailable(r, t));
      } else if (r.manager.upstream_changed) {
        notify.warn(t('updatePanel.upstreamNoticeTitle'),
          t('updatePanel.upstreamOnlyChanged'));
      } else if (r.manager.error || r.upstream.error) {
        // **失败不能说成结论**（issue #204）：403/网络失败时旧缓存会回退，
        // 以前这里照样弹「已是最新」，把一次失败说成了"没有新版本"。
        notify.warn(t('updatePanel.checkFailed'),
          (r.manager.error || r.upstream.error).slice(0, 160));
      } else {
        notify.ok(t('updatePanel.upToDate'), t('updatePanel.upToDateDesc'));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 运行中高频轮询，空闲时低频（服务可能在更新中短暂不可用）
  const running = !!status?.running;
  useEffect(() => {
    const interval = running ? 1500 : 15000;
    const timer = window.setInterval(() => {
      void load();
    }, interval);
    return () => window.clearInterval(timer);
  }, [load, running]);

  // running 一旦变为 true，就复位「已关闭」，使本轮结果在结束后重新可见
  useEffect(() => {
    if (running) setResultDismissed(false);
  }, [running]);

  // 日志自动滚到底
  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [status?.logs]);

  const logs = status?.logs ?? [];
  const logText = useMemo(
    () => (logs.length ? logs.map((l) => l.text).join('\n') : status?.log_tail || ''),
    [logs, status?.log_tail],
  );
  /** 上一次更新已经结束（成功或失败）——有结果可清除 */
  const hasResult = !!status && !status.running && status.ok !== null;

  /**
   * 清除上次更新的结果与日志（issue #105）。
   *
   * 此前只能靠「再发起一次更新」覆盖：状态文件与 update.log 都留着，那条
   * 「更新未完成」和日志永远擦不掉。更新进行中后端会拒绝（409），这里也把入口收起来。
   */
  async function clearResult() {
    setClearBusy(true);
    try {
      await systemApi.clearUpdateStatus();
      notify.ok(t('updatePanel.cleared'));
      setResultDismissed(false);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setClearBusy(false);
    }
  }

  async function start(target: 'manager' | 'upstream' | 'both') {
    setBusy(true);
    try {
      const r = await systemApi.startUpdate(target);
      notify.ok(t('updatePanel.updateStarted'), r.message);
      // 立即拉一次，进入高频轮询
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  const done = !!status && !status.running && status.ok !== null && !resultDismissed;

  return (
    <div className="space-y-4">
      {/* 四份数据全没取到：下面的版本号 / 「已是最新」都不可信，先阻断结论。 */}
      {loadFailed && !running && (
        <LoadError message={t('updatePanel.loadFailed')} onRetry={load} />
      )}
      {/* 当前版本 */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Server className="h-4 w-4" />
            {t('updatePanel.currentVersion')}
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              onClick={recheck}
              disabled={checking || running}
            >
              <RefreshCw className={checking ? 'animate-spin' : ''} />
              {t('updatePanel.checkUpdate')}
            </Button>
            <Button variant="outline" size="sm" className="rounded-full" onClick={load} disabled={running}>
              <RefreshCw className={running ? 'animate-spin' : ''} />
              {t('common.refresh')}
            </Button>
          </div>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div className="rounded-2xl bg-background/60 px-3.5 py-3">
            <div className="text-[11px] text-muted-foreground">{t('updatePanel.manager')}</div>
            <div className="mt-1 flex items-center gap-1.5 text-sm font-semibold tabular-nums">
              {versions?.manager || status?.version || '—'}
              {/* 本次装的东西有没有被核对过：让用户看得见，不只留在日志里 */}
              {status?.integrity?.status === 'digest' && (
                <span
                  className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-600 dark:text-emerald-400"
                  title={status.integrity.digest
                    ? t('updatePanel.verifiedTitleDetail', {digest: status.integrity.digest})
                    : t('updatePanel.verifiedTitle')}
                >
                  <ShieldCheck className="h-3 w-3" />
                  {t('updatePanel.verified')}
                </span>
              )}
            </div>
          </div>
          <div className="rounded-2xl bg-background/60 px-3.5 py-3">
            <div className="text-[11px] text-muted-foreground">{t('updatePanel.upstreamConn')}</div>
            <div className="mt-1 flex items-center gap-1.5 text-sm font-semibold">
              {versions?.upstream_connected ? (
                <>
                  <span className="text-emerald-600 dark:text-emerald-400">{t('updatePanel.connOk')}</span>
                  {typeof versions.upstream_accounts === 'number' && (
                    <span className="text-xs font-normal text-muted-foreground">
                      {t('updatePanel.accountCount', {
                        count: versions.upstream_accounts,
                        n: versions.upstream_accounts,
                      })}
                    </span>
                  )}
                </>
              ) : (
                <span className="text-red-600 dark:text-red-400">{t('updatePanel.connDown')}</span>
              )}
            </div>
          </div>
          <div className="rounded-2xl bg-background/60 px-3.5 py-3">
            <div className="text-[11px] text-muted-foreground">{t('updatePanel.lastUpdate')}</div>
            <div className="mt-1 text-sm font-semibold">
              {status?.finished_at ? fmtAgo(status.finished_at) : t('format.never')}
            </div>
          </div>
        </div>
        {status?.upstream_dir && (
          <div className="mt-2 truncate font-mono text-[11px] text-muted-foreground" title={status.upstream_dir}>
            {t('updatePanel.upstreamDir', {dir: status.upstream_dir})}
          </div>
        )}
      </div>

      {/* 新版本提醒 */}
      {check?.has_any && !status?.running && (
        <div className="rounded-[20px] border border-blue-500/40 bg-blue-500/10 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex items-start gap-2.5">
              <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-blue-500" />
              <div className="space-y-1.5">
                <div className="text-xs font-medium">{t('updatePanel.newVersionAvailable')}</div>
                {check.manager.has_update && (
                  <div className="text-[11px] text-muted-foreground">
                    {t('updatePanel.managerUpgrade', {from: check.manager.current})}{' '}
                    <span className="font-medium text-foreground">{check.manager.latest}</span>
                  </div>
                )}
                {/* 更新前就能看到 Release 正文：像「需要配套上游」这种要求，
                    等装完再翻本地 CHANGELOG 已经晚了（issue #217）。 */}
                {check.manager.has_update && !!check.manager.release_notes && (
                  <details className="group text-[11px]">
                    <summary className="cursor-pointer list-none text-foreground/80">
                      <span className="group-open:hidden">{t('updatePanel.releaseNotes')}</span>
                      <span className="hidden group-open:inline">{t('updatePanel.hideReleaseNotes')}</span>
                    </summary>
                    <pre className="mt-1 max-h-[220px] overflow-auto whitespace-pre-wrap break-all rounded-xl bg-background/60 p-2 font-sans leading-relaxed text-muted-foreground">
                      {check.manager.release_notes}
                    </pre>
                  </details>
                )}
                {check.manager.has_update && check.manager.upstream_changed && (
                  <div className="flex items-start gap-1.5 text-[11px] text-amber-600 dark:text-amber-400">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    <span>{t('updatePanel.upstreamChanged')}</span>
                  </div>
                )}
                {/* 产物未就位：版本可见但没有 .tar.gz 时点更新只会拿到 404。先说清楚、
                    并把按钮收起来（issue #127/#129 是同一类窗口期问题）。产物上传后
                    最迟五分钟、或点一次刷新就会恢复。 */}
                {check.manager.has_update && check.manager.assets_ready === false && (
                  <div className="flex items-start gap-1.5 text-[11px] text-amber-600 dark:text-amber-400">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    <span>{t('updatePanel.assetsPending')}</span>
                  </div>
                )}
                <div className="text-[11px] text-muted-foreground">
                  {t('updatePanel.updateHint')}
                </div>
              </div>
            </div>
            <div className="flex gap-2">
              {check.manager.has_update && (
                <Button
                  size="sm"
                  className="rounded-full"
                  // 产物未就位时不放行：这条路必然以 404 收场，让用户点了再失败只是把
                  // 同一条信息换个地方说（见上方提示）。
                  disabled={!isAdmin || busy || running || check.manager.assets_ready === false
                    || (!!check.manager.upstream_changed && status?.can_update_upstream === false)}
                  title={check.manager.assets_ready === false
                    ? t('updatePanel.assetsPending')
                    : check.manager.upstream_changed && status?.can_update_upstream === false
                      ? t('updatePanel.dockerNote')
                      : undefined}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      // 本次发布带了不同的上游：一起更新，避免「面板升了、上游没升」
                      // 的版本错配（issue #217）。否则维持原来的只换面板。
                      const r = await systemApi.startUpdate(
                        check.manager.upstream_changed ? 'both' : 'manager');
                      notify.ok(t('updatePanel.updateStarted'), r.message);
                      await load();
                    } catch (e) {
                      notify.err(errText(e));
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  <DownloadCloud />
                  {check.manager.upstream_changed
                    ? t('updatePanel.updateBoth') : t('updatePanel.updateManager')}
                </Button>
              )}
            </div>
          </div>
        </div>
      )}

      {check && !check.manager.has_update && check.manager.upstream_changed
        && !status?.running && (
        <div className="flex items-start gap-2 rounded-[20px] border border-amber-500/30 bg-amber-500/10 px-4 py-3">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          <span className="text-[11px] leading-4 text-muted-foreground">
            {t('updatePanel.upstreamOnlyChanged')}
          </span>
        </div>
      )}

      {/* GitHub Token（issue #205）：未认证 60 次/小时/IP，共享出口很容易被限流 */}
      {isAdmin && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-1 text-xs font-medium">{t('updatePanel.ghTitle')}</div>
          <div className="mb-2 text-[11px] leading-4 text-muted-foreground">
            <RichText text={t('updatePanel.ghDesc')} />
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-[16rem] flex-1 space-y-1">
              <Input
                type="password"
                value={ghInput}
                onChange={(e) => setGhInput(e.target.value)}
                placeholder={gh?.configured ? gh.masked : t('updatePanel.ghPlaceholder')}
                className="h-8 bg-background font-mono text-xs"
                autoComplete="off"
              />
            </div>
            <Button size="sm" className="rounded-full" disabled={ghBusy || !ghInput.trim()}
                    onClick={() => saveGh(ghInput.trim())}>
              <ShieldCheck className="h-3.5 w-3.5" />
              {t('common.save')}
            </Button>
            {gh?.configured && (
              <Button size="sm" variant="outline" className="rounded-full" disabled={ghBusy}
                      onClick={() => saveGh('')}>
                <Trash2 className="h-3.5 w-3.5" />
                {t('updatePanel.ghClear')}
              </Button>
            )}
          </div>
          <div className="mt-2 text-[11px] text-muted-foreground">
            {gh?.source === 'env'
              ? t('updatePanel.ghFromEnv')
              : gh?.configured
                ? t('updatePanel.ghConfigured', {masked: gh.masked})
                : t('updatePanel.ghNotSet')}
          </div>
        </div>
      )}

      {/* GitHub 镜像（issue #216）：国内机器直连 GitHub 超时时用它加速 */}
      {isAdmin && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-1 text-xs font-medium">{t('updatePanel.mirrorTitle')}</div>
          <div className="mb-2 text-[11px] leading-4 text-muted-foreground">
            <RichText text={t('updatePanel.mirrorDesc')} />
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-[16rem] flex-1 space-y-1">
              <Input
                value={mirrorInput}
                onChange={(e) => setMirrorInput(e.target.value)}
                placeholder="https://gh-proxy.com/"
                aria-label={t('updatePanel.mirrorTitle')}
                className="h-8 bg-background font-mono text-xs"
                autoComplete="off"
              />
            </div>
            <Button size="sm" className="rounded-full" disabled={mirrorBusy}
                    onClick={() => void saveMirror(mirrorInput.trim())}>
              <Globe className="h-3.5 w-3.5" />
              {t('common.save')}
            </Button>
            {mirror?.stored && (
              <Button size="sm" variant="outline" className="rounded-full" disabled={mirrorBusy}
                      onClick={() => void saveMirror('')}>
                <Trash2 className="h-3.5 w-3.5" />
                {t('updatePanel.mirrorClear')}
              </Button>
            )}
          </div>
          {!!mirror?.presets?.length && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] text-muted-foreground">{t('updatePanel.mirrorPresets')}</span>
              {mirror.presets.map((p) => (
                <button
                  key={p}
                  type="button"
                  className="rounded-full bg-background px-2 py-0.5 font-mono text-[11px] text-muted-foreground hover:text-foreground"
                  onClick={() => setMirrorInput(p)}
                >
                  {p}
                </button>
              ))}
            </div>
          )}
          <div className="mt-2 text-[11px] leading-4 text-muted-foreground">
            {mirror?.source === 'env'
              ? t('updatePanel.mirrorFromEnv', {value: mirror.env_mirror})
              : mirror?.mirror
                ? t('updatePanel.mirrorActive', {value: mirror.mirror})
                : t('updatePanel.mirrorNone')}
          </div>
        </div>
      )}

      {/* 自动更新：定时检测，只在时间窗内自动执行一键更新（默认关闭） */}
      {isAdmin && autoForm && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="flex items-center gap-1.5 text-xs font-medium">
                <Clock className="h-3.5 w-3.5" />
                {t('updatePanel.autoTitle')}
              </div>
              <div className="mt-1 text-[11px] leading-4 text-muted-foreground">
                <RichText text={t('updatePanel.autoDesc')} />
              </div>
            </div>
            <Switch
              aria-label={t('updatePanel.autoTitle')}
              checked={autoForm.enabled}
              disabled={autoBusy || (!!auto!.unsupported_reason && !autoForm.enabled)}
              onCheckedChange={(v) => void saveAuto({...autoForm, enabled: v})}
            />
          </div>
          {auto!.unsupported_reason && (
            <div className="mt-2 flex items-start gap-1.5 text-[11px] leading-4 text-amber-600 dark:text-amber-400">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {t('updatePanel.autoUnsupported')}
            </div>
          )}
          <div className="mt-3 grid gap-3 sm:grid-cols-3">
            <div className="space-y-1">
              <Label className="text-[11px] text-muted-foreground">{t('updatePanel.autoInterval')}</Label>
              <Input
                type="number"
                min={1}
                max={168}
                value={autoForm.interval_hours}
                onChange={(e) => setAutoForm({...autoForm, interval_hours: Number(e.target.value) || 0})}
                className="h-8 bg-background text-xs"
                aria-label={t('updatePanel.autoInterval')}
              />
            </div>
            <div className="space-y-1">
              <Label className="text-[11px] text-muted-foreground">{t('updatePanel.autoWindow')}</Label>
              <div className="flex items-center gap-1.5">
                <Input
                  type="number"
                  min={0}
                  max={23}
                  value={autoForm.window_start}
                  onChange={(e) => setAutoForm({...autoForm, window_start: Number(e.target.value)})}
                  className="h-8 bg-background text-xs"
                  aria-label={t('updatePanel.autoWindowStart')}
                />
                <span className="text-xs text-muted-foreground">–</span>
                <Input
                  type="number"
                  min={0}
                  max={23}
                  value={autoForm.window_end}
                  onChange={(e) => setAutoForm({...autoForm, window_end: Number(e.target.value)})}
                  className="h-8 bg-background text-xs"
                  aria-label={t('updatePanel.autoWindowEnd')}
                />
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-[11px] text-muted-foreground">{t('updatePanel.autoTarget')}</Label>
              <Select value={autoForm.target}
                      onValueChange={(v) => setAutoForm({...autoForm, target: v as AutoUpdateConfig['target']})}>
                <SelectTrigger className="h-8 w-full bg-background text-xs" aria-label={t('updatePanel.autoTarget')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">{t('updatePanel.autoTargetAuto')}</SelectItem>
                  <SelectItem value="manager">{t('updatePanel.autoTargetManager')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            <div className="text-[11px] leading-4 text-muted-foreground">
              {t('updatePanel.autoWindowHint', {
                tz: `UTC${auto!.server_tz_offset_minutes >= 0 ? '+' : '-'}${
                  String(Math.floor(Math.abs(auto!.server_tz_offset_minutes) / 60)).padStart(2, '0')}:${
                  String(Math.abs(auto!.server_tz_offset_minutes) % 60).padStart(2, '0')}`,
              })}
              {autoForm.target === 'auto' && !auto!.docker_ok && (
                <> {t('updatePanel.autoNoDocker')}</>
              )}
            </div>
            {autoDirty && (
              <Button size="sm" className="rounded-full" disabled={autoBusy}
                      onClick={() => void saveAuto(autoForm)}>
                {t('common.save')}
              </Button>
            )}
          </div>
          {auto!.state.last_check_at > 0 && (
            <div className="mt-2 text-[11px] leading-4 text-muted-foreground">
              {t('updatePanel.autoLastRun', {
                ago: fmtAgo(auto!.state.last_check_at),
                msg: auto!.state.last_message || '-',
              })}
            </div>
          )}
        </div>
      )}

      {/* 本次检测失败、值来自上次成功结果：必须说明，不能让人以为「已经是最新」 */}
      {check && check.manager.error && !status?.running && (
        <div className="flex items-start gap-2 rounded-[20px] border border-amber-500/30 bg-amber-500/10 px-4 py-3">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          <div className="min-w-0 space-y-1 text-[11px] leading-4">
            <div className="font-medium">{t('updatePanel.checkFailed')}</div>
            <div className="break-all text-muted-foreground">
              {check.manager.error.slice(0, 200)}
            </div>
            <div className="text-muted-foreground">
              {check.manager.stale
                ? t('updatePanel.staleNote', {
                    at: fmtDateTime(check.manager.stale_at || 0),
                  })
                : t('updatePanel.checkFailedHint')}
            </div>
          </div>
        </div>
      )}

      {check && !check.has_any && !check.manager.upstream_changed
        && !check.manager.error && !status?.running && (
        <div className="flex items-center gap-2 rounded-[20px] border border-emerald-500/30 bg-emerald-500/10 px-4 py-3">
          <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
          <span className="text-xs">{t('updatePanel.allUpToDate')}</span>
          {check.checked_at > 0 && (
            <span className="text-[11px] text-muted-foreground">
              {t('updatePanel.checkedAgo', {ago: fmtAgo(check.checked_at)})}
            </span>
          )}
        </div>
      )}

      {/* 更新状态 */}
      {status?.running && (
        <div className="flex items-start gap-2.5 rounded-[20px] border border-blue-500/30 bg-blue-500/10 p-4">
          <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-blue-500" />
          <div className="min-w-0 flex-1 space-y-1">
            <div className="text-xs font-medium">
              {t('updatePanel.updatingNow', {step: status.step || t('updatePanel.stepRunning')})}
            </div>
            <div className="text-[11px] text-muted-foreground">
              {t('updatePanel.updatingHint')}
            </div>
          </div>
        </div>
      )}

      {done && (
        <div
          className={
            'flex items-start gap-2.5 rounded-[20px] border p-4 ' +
            (status?.ok
              ? 'border-emerald-500/30 bg-emerald-500/10'
              : 'border-red-500/30 bg-red-500/10')
          }
        >
          {status?.ok ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
          ) : (
            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-500" />
          )}
          <div className="min-w-0 flex-1 space-y-1">
            <div className="text-xs font-medium">
              {status?.ok ? t('updatePanel.updateDone') : t('updatePanel.updateNotDone')}
            </div>
            <div className="text-[11px] text-muted-foreground">
              {status?.ok
                ? (status?.duration ? t('updatePanel.doneDuration', {n: status.duration}) : '') +
                  t('updatePanel.doneRestart') +
                  (status?.integrity?.status === 'digest' ? t('updatePanel.doneVerified') : '')
                : t('updatePanel.failedDetail')}
            </div>

          </div>
          <button
            type="button"
            aria-label={t('updatePanel.closeTip')}
            title={t('updatePanel.closeTip')}
            className="-m-1 shrink-0 rounded-full p-1 text-muted-foreground transition-colors hover:bg-foreground/10 hover:text-foreground"
            onClick={() => setResultDismissed(true)}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {status && status.updater_found === false && (
        <div className="flex items-start gap-2.5 rounded-[20px] border border-amber-500/30 bg-amber-500/10 p-4">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          <div className="space-y-1">
            <div className="text-xs font-medium">{t('updatePanel.updaterMissing')}</div>
            <div className="text-[11px] text-muted-foreground">
              <RichText text={t('updatePanel.updaterMissingHint')} />
            </div>
          </div>
        </div>
      )}

      {/* 更新操作 */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-1 text-sm font-medium">{t('updatePanel.oneClickUpdate')}</div>
        <div className="mb-3 text-[11px] leading-4 text-muted-foreground">
          {t('updatePanel.oneClickUpdateDesc')}
        </div>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {TARGETS.map((target, i) => {
            // 能否更新上游取决于**能否操作 docker**（后端按实际能力判定），
            // 而不是"是否在容器里"：容器挂了 docker.sock 就能做这些事。
            // 不可用时直接禁用并说明替代做法，而不是让用户点了才失败。
            const blocked = status?.can_update_upstream === false && target.id !== 'manager';
            const text = blocked
              ? t('updatePanel.dockerBlocked')
              : t('updatePanel.targetDescText', {
                  desc: t(target.descKey),
                  hint: t(target.hintKey),
                });
            return (
            <ConfirmDialog
              key={target.id}
              title={t('updatePanel.targetConfirmTitle', {label: t(target.labelKey)})}
              description={text}
              confirmText={t('updatePanel.startUpdate')}
              onConfirm={() => start(target.id)}
              trigger={
                <button
                  type="button"
                  disabled={!isAdmin || busy || running || blocked}
                  title={blocked ? t('updatePanel.targetBlockedTitle') : undefined}
                  className={
                    'flex flex-col items-start gap-1 rounded-2xl px-3.5 py-3 text-left transition-colors ' +
                    'bg-background/60 hover:bg-background disabled:cursor-not-allowed disabled:opacity-50'
                  }
                >
                  <div className="flex items-center gap-1.5 text-xs font-medium">
                    {i === 0 ? <DownloadCloud className="h-3.5 w-3.5" /> : <RotateCcw className="h-3.5 w-3.5" />}
                    {t(target.labelKey)}
                  </div>
                  <div className="text-[11px] leading-4 text-muted-foreground">{t(target.descKey)}</div>
                </button>
              }
            />
            );
          })}
        </div>
        {status?.can_update_upstream === false && (
          <p className="mt-2 text-[11px] leading-4 text-muted-foreground">
            <RichText text={t('updatePanel.dockerNote')} />
          </p>
        )}
        {!isAdmin && (
          <p className="mt-2 text-[11px] text-muted-foreground">{t('updatePanel.readonlyNote')}</p>
        )}
      </div>

      {/* 离线升级包（issue #153）：内网 / 拿不到 GitHub 的机器，从别处下载发布包上传 */}
      {isAdmin && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-1 flex flex-wrap items-center gap-2 text-sm font-medium">
            <Upload className="h-4 w-4" />
            {t('updatePanel.pkgTitle')}
            {pkg?.present && pkg.version && (
              <Badge variant="secondary" className="rounded-full font-mono text-[10px]">
                {pkg.version}
              </Badge>
            )}
          </div>
          <div className="mb-3 text-[11px] leading-4 text-muted-foreground">
            <RichText text={t('updatePanel.pkgDesc')} />
          </div>

          {pkg?.present ? (
            <div className="space-y-2">
              <div className="rounded-2xl bg-background/60 px-3.5 py-2.5 text-[11px] leading-5">
                <div className="font-mono">{pkg.name || t('updatePanel.pkgUnnamed')}</div>
                <div className="text-muted-foreground break-all">
                  sha256 {pkg.sha256 || '—'}
                  {pkg.size ? ` · ${(pkg.size / 1024 / 1024).toFixed(1)} MB` : ''}
                  {pkg.expected_sha256 ? ` · ${t('updatePanel.pkgCompared')}` : ` · ${t('updatePanel.pkgNotCompared')}`}
                </div>
                {!pkg.expected_sha256 && (
                  <div className="mt-1 text-amber-600 dark:text-amber-500">
                    {t('updatePanel.pkgCompareHint')}
                  </div>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" className="rounded-full"
                        disabled={busy || running}
                        onClick={() => void startFromPackage('both')}>
                  <DownloadCloud className="mr-1.5 h-3.5 w-3.5" />
                  {t('updatePanel.pkgUseBoth')}
                </Button>
                <Button size="sm" variant="outline" className="rounded-full"
                        disabled={busy || running}
                        onClick={() => void startFromPackage('manager')}>
                  {t('updatePanel.pkgUseManager')}
                </Button>
                <Button size="sm" variant="ghost" className="rounded-full"
                        disabled={pkgBusy || running}
                        onClick={() => void dropPackage()}>
                  {t('updatePanel.pkgClear')}
                </Button>
              </div>
            </div>
          ) : (
            <div className="space-y-2">
              <div className="flex flex-wrap items-end gap-2">
                <label className="flex flex-col gap-1">
                  <Label className="text-[11px] text-muted-foreground">{t('updatePanel.pkgFile')}</Label>
                  <input
                    type="file"
                    accept=".tar.gz,application/gzip"
                    disabled={pkgBusy || running}
                    className="text-[11px] file:mr-2 file:rounded-full file:border-0 file:bg-background file:px-3 file:py-1.5 file:text-[11px] file:text-foreground"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file) void uploadPackage(file);
                      e.target.value = '';
                    }}
                  />
                </label>
                <label className="flex min-w-[16rem] flex-1 flex-col gap-1">
                  <Label className="text-[11px] text-muted-foreground">{t('updatePanel.pkgShaLabel')}</Label>
                  <Input className="h-8 bg-background font-mono text-[11px]"
                         value={pkgSha} placeholder={t('updatePanel.pkgShaPlaceholder')}
                         onChange={(e) => setPkgSha(e.target.value)} />
                </label>
              </div>
              {pkgBusy && (
                <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  {t('updatePanel.pkgUploading')}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* 日志。**没有可看的内容时不渲染这块**（issue #105：一次失败会长期驻留，
          清掉记录后这里还应整块消失，而不是永远挂着一个空框）。 */}
      {(logText || running || hasResult) && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Terminal className="h-4 w-4" />
              {t('updatePanel.logTitle')}
            </div>
            <div className="flex items-center gap-2">
              {logs.length > 0 && (
                <Badge variant="secondary" className="rounded-full text-[10px]">
                  {t('updatePanel.logLines', {count: logs.length, n: logs.length})}
                </Badge>
              )}
              {/* 清除上次更新的结果与日志。更新进行中不给入口（后端也会拒绝）。 */}
              {!running && (logText || hasResult) && (
                <ConfirmDialog
                  title={t('updatePanel.clearResultTitle')}
                  description={t('updatePanel.clearResultDesc')}
                  confirmText={t('updatePanel.clearResult')}
                  destructive
                  onConfirm={clearResult}
                  trigger={
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 rounded-full px-2 text-[11px] text-muted-foreground"
                      disabled={clearBusy}
                    >
                      <Trash2 className="h-3 w-3" />
                      {t('updatePanel.clearResult')}
                    </Button>
                  }
                />
              )}
            </div>
          </div>
          {logText ? (
            <div
              ref={logRef}
              className="scroll-slim max-h-[320px] overflow-auto rounded-2xl bg-background/60 p-3"
            >
              <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-5 text-muted-foreground">
                {logText}
              </pre>
            </div>
          ) : (
            <div className="rounded-2xl bg-background/60 px-3 py-8 text-center text-xs text-muted-foreground">
              {t('updatePanel.logEmpty')}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

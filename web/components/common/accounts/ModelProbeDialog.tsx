'use client';

import {useMemo, useRef, useState} from 'react';
import {CheckCheck, Loader2, ScanSearch, TriangleAlert, X} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {useAsyncAll} from '@/lib/use-async-data';
import {accountApi, errText, modelApi} from '@/lib/api';
import {useI18n} from '@/lib/i18n/provider';
import {planProbeBatches} from '@/lib/probe-batches';
import type {Realm} from '@/lib/realm-context';
import type {
  Account,
  ProbeModelResult,
  ProbeModelsResponse,
  ProbeStatus,
} from '@/lib/types';

const STATUS_KEY: Record<ProbeStatus, string> = {
  ok: 'accounts.probeStatusOk',
  rate: 'accounts.probeStatusRate',
  credit: 'accounts.probeStatusCredit',
  auth: 'accounts.probeStatusAuth',
  no_model: 'accounts.probeStatusNoModel',
  error: 'accounts.probeStatusError',
};

const STATUS_STYLE: Record<ProbeStatus, string> = {
  ok: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  rate: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  credit: 'bg-orange-500/10 text-orange-600 dark:text-orange-400',
  auth: 'bg-red-500/10 text-red-600 dark:text-red-400',
  no_model: 'bg-muted text-muted-foreground',
  error: 'bg-red-500/10 text-red-600 dark:text-red-400',
};

const STATUS_ORDER: ProbeStatus[] = ['ok', 'rate', 'credit', 'auth', 'no_model', 'error'];

/**
 * 账号 × 模型主动探测（issue #270）。
 *
 * 解决的问题：面板此前只能**被动**知道「某号某模型被限流」——要等上游真的拒绝
 * 过一次。新号接入、上游新增模型、遇到 `503 no_healthy_account` 时，都无法回答
 * 「这个号到底能不能用这个模型」。
 *
 * 探测会发**真实请求**（max_tokens=1 的最小对话），所以：
 *   · 账号内串行、每条间隔 600ms，账号间最多 4 路并发（后端实现）；
 *   · 每批最多 60 条组合，界面先显示总请求数、批次数与进度；
 *   · `11102 无此模型` 不算错误——账号本身没问题。
 */
export function ModelProbeDialog({
  open,
  onOpenChange,
  accounts,
  upstreamId,
  realm,
  onFinished,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 当前版本的账号（父组件已按 realm 过滤） */
  accounts: Account[];
  upstreamId: number | null;
  realm: Realm;
  onFinished?: () => void;
}) {
  const {t} = useI18n();
  const [pickedAccounts, setPickedAccounts] = useState<string[]>(
    () => accounts.map((a) => a.file),
  );
  const [pickedModels, setPickedModels] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<ProbeModelsResponse | null>(null);
  const [resultSelection, setResultSelection] = useState<{accounts: Account[]; models: string[]}>({accounts: [], models: []});
  const [completed, setCompleted] = useState(0);
  const [stopping, setStopping] = useState(false);
  const stopRequested = useRef(false);

  const catalog = useAsyncAll({catalog: () => modelApi.catalog(realm)}, [realm]);
  const models = catalog.values.catalog?.models ?? [];
  const modelName = (id: string) => models.find((m) => m.id === id)?.name || id;

  const combos = pickedAccounts.length * pickedModels.length;
  const batches = planProbeBatches(pickedAccounts, pickedModels);

  const matrix = useMemo(() => {
    const map = new Map<string, ProbeModelResult>();
    for (const r of result?.results ?? []) {
      map.set(`${r.file}\u0000${r.model}`, r);
    }
    return map;
  }, [result]);

  function toggle(list: string[], value: string): string[] {
    if (list.includes(value)) return list.filter((v) => v !== value);
    return [...list, value];
  }

  async function run() {
    setBusy(true);
    setError('');
    setResult(null);
    // 结果属于本次探测；为下一次勾选账号/模型时，不重画旧结果的行列。
    setResultSelection({
      accounts: accounts.filter((a) => pickedAccounts.includes(a.file)),
      models: [...pickedModels],
    });
    setCompleted(0);
    setStopping(false);
    stopRequested.current = false;
    const merged: ProbeModelsResponse = {
      results: [], summary: {ok: 0, rate: 0, credit: 0, auth: 0, no_model: 0, error: 0}, requests: 0,
    };
    try {
      for (const batch of batches) {
        if (stopRequested.current) break;
        const r = await accountApi.probeModels(batch.accounts, batch.models, upstreamId);
        merged.results.push(...r.results);
        merged.requests += r.requests;
        for (const status of STATUS_ORDER) merged.summary[status] += r.summary[status] ?? 0;
        setResult({...merged, results: [...merged.results], summary: {...merged.summary}});
        setCompleted(merged.requests);
        onFinished?.();
      }
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
      setStopping(false);
    }
  }

  const canRun = !busy && pickedAccounts.length > 0 && pickedModels.length > 0;

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!busy) onOpenChange(v); }}>
      <DialogContent className="flex max-h-[88vh] min-w-0 max-w-[760px] flex-col">
        <DialogHeader className="shrink-0">
          <DialogTitle className="flex items-center gap-2">
            <ScanSearch className="size-4" />
            {t('accounts.probeTitle')}
          </DialogTitle>
          <DialogDescription className="pt-1 text-[12px] leading-relaxed">
            {t('accounts.probeDesc')}
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 min-w-0 space-y-3 overflow-y-auto px-6 pb-2">
          <div className="grid min-w-0 gap-4 sm:grid-cols-2">
            {/* 账号 */}
            <div className="min-w-0">
              <div className="mb-1 flex items-center justify-between text-xs font-medium">
                <span>{t('accounts.probeAccounts')}</span>
                <Button
                  type="button"
                  variant="outline" size="sm"
                  disabled={busy || accounts.length === 0}
                  className="h-7 cursor-pointer gap-1 rounded-full text-[11px]"
                  onClick={() => {
                    setPickedAccounts(pickedAccounts.length === accounts.length ? [] : accounts.map((a) => a.file));
                    setCompleted(0);
                  }}
                >
                  {pickedAccounts.length === accounts.length ? <X className="size-3" /> : <CheckCheck className="size-3" />}
                  {pickedAccounts.length === accounts.length ? t('accounts.probeClear') : t('accounts.probeSelectAll')}
                </Button>
              </div>
              <div className="max-h-[150px] overflow-y-auto rounded-xl border p-2">
                {accounts.length === 0 && (
                  <div className="p-2 text-[11px] text-muted-foreground">
                    {t('dashboard.noAccounts')}
                  </div>
                )}
                {accounts.map((a) => (
                  <label key={a.file}
                         className="flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-[12px] hover:bg-muted">
                    <input
                      type="checkbox"
                      disabled={busy}
                      checked={pickedAccounts.includes(a.file)}
                      onChange={() => { setPickedAccounts((v) => toggle(v, a.file)); setCompleted(0); }}
                    />
                    <span className="truncate">{a.nickname || a.uid || a.file}</span>
                  </label>
                ))}
              </div>
            </div>

            {/* 模型 */}
            <div className="min-w-0">
              <div className="mb-1 flex items-center justify-between text-xs font-medium">
                <span>{t('accounts.probeModels')}</span>
                <Button
                  type="button"
                  variant="outline" size="sm"
                  disabled={busy || models.length === 0}
                  className="h-7 cursor-pointer gap-1 rounded-full text-[11px]"
                  onClick={() => {
                    setPickedModels(pickedModels.length === models.length ? [] : models.map((m) => m.id));
                    setCompleted(0);
                  }}
                >
                  {pickedModels.length === models.length ? <X className="size-3" /> : <CheckCheck className="size-3" />}
                  {pickedModels.length === models.length ? t('accounts.probeClear') : t('accounts.probeSelectAll')}
                </Button>
              </div>
              <div className="max-h-[150px] overflow-y-auto rounded-xl border p-2">
                {models.length === 0 && (
                  <div className="p-2 text-[11px] text-muted-foreground">
                    {t('accounts.probeNoModels')}
                  </div>
                )}
                {models.map((m) => (
                  <label key={m.id}
                         className="flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-[12px] hover:bg-muted">
                    <input
                      type="checkbox"
                      disabled={busy}
                      checked={pickedModels.includes(m.id)}
                      onChange={() => { setPickedModels((v) => toggle(v, m.id)); setCompleted(0); }}
                    />
                    <span className="truncate" title={m.id}>{m.name || m.id}</span>
                  </label>
                ))}
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
            <span>{t('accounts.probeRequests', {n: combos})}</span>
            <span>{t('accounts.probeBatchProgress', {done: completed, total: combos, batches: batches.length})}</span>
            <button type="button" className="text-primary hover:underline" disabled={busy}
                    onClick={() => {
                      const restricted = accounts.filter((a) => (a.rate_limited_models ?? []).some((m) => m.reason?.startsWith('11102')));
                      setPickedAccounts(restricted.map((a) => a.file));
                      setPickedModels([...new Set(restricted.flatMap((a) => (a.rate_limited_models ?? [])
                        .filter((m) => m.reason?.startsWith('11102')).map((m) => m.model)))]);
                      setCompleted(0);
                    }}>
              {t('accounts.probeSelectRestricted')}
            </button>
          </div>
          <p className="text-[11px] leading-relaxed text-muted-foreground">{t('accounts.probeAutoRecoveryHint')}</p>
          {busy && <progress className="h-1.5 w-full accent-primary" max={Math.max(1, combos)} value={completed} />}

          {error && (
            <div className="flex items-start gap-2 rounded-xl bg-red-500/10 px-3 py-2 text-[11px] text-red-600">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {result && (
            <div className="min-w-0 space-y-2">
              <div className="flex flex-wrap gap-1.5">
                <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] text-primary">
                  {t('accounts.probeSynced', {n: result.results.filter((r) => r.gateway_synced).length})}
                </span>
                {STATUS_ORDER.filter((s) => (result.summary[s] ?? 0) > 0).map((s) => (
                  <span key={s}
                        className={`whitespace-nowrap rounded-full px-2 py-0.5 text-[10px] ${STATUS_STYLE[s]}`}>
                    {t(STATUS_KEY[s])} {result.summary[s]}
                  </span>
                ))}
              </div>
              {result.results.some((r) => r.gateway_synced === false && !r.gateway_sync_skipped) && (
                <p className="text-[11px] text-amber-600">{t('accounts.probeSyncFailed')}</p>
              )}
              <div className="max-h-[32vh] min-w-0 max-w-full overflow-auto rounded-xl border">
                <table className="w-full border-collapse text-[11px]">
                  <thead className="sticky top-0 z-10 bg-background">
                    <tr className="border-b bg-muted/50">
                      <th className="whitespace-nowrap px-2 py-1.5 text-left font-medium">
                        {t('accounts.probeAccounts')}
                      </th>
                      {resultSelection.models.map((m) => (
                        <th key={m} className="whitespace-nowrap px-2 py-1.5 text-center font-medium"
                            title={m}>
                          {modelName(m)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {resultSelection.accounts.map((a) => (
                      <tr key={a.file} className="border-b last:border-0">
                        <td className="max-w-[160px] truncate px-2 py-1.5" title={a.file}>
                          {a.nickname || a.uid || a.file}
                        </td>
                        {resultSelection.models.map((m) => {
                          const r = matrix.get(`${a.file}\u0000${m}`);
                          return (
                            <td key={m} className="px-1.5 py-1 text-center">
                              {r ? (
                                <span
                                  className={`inline-block whitespace-nowrap rounded-full px-2 py-0.5 ${STATUS_STYLE[r.status]}`}
                                  title={`${t(STATUS_KEY[r.status])}\n${r.message}${r.gateway_sync_message ? `\n${r.gateway_sync_message}` : ''}`}
                                >
                                  {t(STATUS_KEY[r.status])}
                                </span>
                              ) : (
                                <span className="text-muted-foreground">—</span>
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>

        <DialogFooter className="shrink-0 pt-2">
          <Button variant="ghost" size="sm" className="rounded-full"
                  disabled={stopping} onClick={() => {
                    if (busy) { stopRequested.current = true; setStopping(true); }
                    else onOpenChange(false);
                  }}>
            {busy ? t(stopping ? 'accounts.probeStopping' : 'accounts.probeStop') : t('common.cancel')}
          </Button>
          <Button size="sm" className="rounded-full" disabled={!canRun} onClick={run}>
            {busy ? <Loader2 className="mr-1.5 size-3.5 animate-spin" /> : null}
            {busy ? t('accounts.probeRunning') : t('accounts.probeRun')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

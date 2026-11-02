'use client';

import {useEffect, useState} from 'react';
import Link from 'next/link';
import {ChevronDown, ChevronUp, ShieldAlert} from 'lucide-react';
import {accountRiskApi, errText} from '@/lib/api';
import {useAsyncAll} from '@/lib/use-async-data';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useRealm} from '@/lib/realm-context';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {fmtDateTime} from '@/lib/format';
import {notify} from '@/lib/toast';
import {Button} from '@/components/ui/button';

export function AccountRiskWarnings({upstreamId = null, allGroups = false}: {
  upstreamId?: number | null; allGroups?: boolean;
}) {
  const {realm} = useRealm();
  const {isAdmin} = useAuth();
  const t = useT();
  const [history, setHistory] = useState(false);
  /** 折叠偏好：'1' 收起 / '0' 展开 / null 还没选过。默认在「没有账号被暂停」时收起。 */
  const [foldPref, setFoldPref] = useState<string | null>(null);
  useEffect(() => {
    try { setFoldPref(window.localStorage.getItem('wb.account-risk.collapsed')); } catch { /* 读不到按未选处理 */ }
  }, []);
  const [busy, setBusy] = useState(false);
  const {values, errors, reload} = useAsyncAll({warnings: () => accountRiskApi.list(realm, upstreamId, allGroups, history)},
    [realm, upstreamId, allGroups, history]);
  useHeartbeat(reload, 30000);
  const data = values.warnings;
  const failed = !!errors.warnings || !!data?.failed_groups?.length;
  const items = data?.items ?? [];
  const pausedCount = new Set(items.filter(x => x.paused === true).map(x => `${x.group}:${x.uid}`)).size;
  if (!items.length && !data?.total && !failed) return null;
  const fold = foldPref === '1';
  function toggleFold() {
    const next = !fold;
    setFoldPref(next ? '1' : '0');
    try { window.localStorage.setItem('wb.account-risk.collapsed', next ? '1' : '0'); } catch { /* 写不进去只影响下次默认 */ }
  }

  async function resume(id: string) {
    setBusy(true);
    try { await accountRiskApi.resume(id); await reload(); notify.ok(t('risk.resumed')); }
    catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  async function acknowledge() {
    setBusy(true);
    try {
      for (const item of items.filter(item => !item.acknowledged)) await accountRiskApi.acknowledge(item.id);
      await reload();
      notify.ok(t('risk.acknowledged'));
    } catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  return (
    <section aria-label={t('risk.title')} className="rounded-2xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <ShieldAlert className="h-4 w-4 shrink-0 text-amber-600" />
        <h2 className="font-semibold">{t('risk.title')}</h2>
        <span className="text-xs text-muted-foreground">{t('risk.count', {n: new Set(items.map(x => `${x.group}:${x.uid}`)).size})}</span>
        {!failed && <span className="text-xs font-medium">{t('risk.pausedCount', {n: pausedCount})}</span>}
        {isAdmin && items.some(x => !x.acknowledged) && (
          <Button variant="outline" size="sm" className="ml-auto" disabled={busy} onClick={acknowledge}>{t('risk.acknowledge')}</Button>
        )}
        {/* 预警有历史记录时会一直占着页面顶部（issue #374）。允许收起/展开，
            偏好按浏览器记住；没有账号被暂停时默认收起。 */}
        <Button variant="ghost" size="sm" className={isAdmin && items.some(x => !x.acknowledged) ? '' : 'ml-auto'}
                aria-expanded={!fold} onClick={toggleFold}>
          {fold ? <ChevronDown className="mr-1 h-3.5 w-3.5" /> : <ChevronUp className="mr-1 h-3.5 w-3.5" />}
          {t(fold ? 'risk.expand' : 'risk.collapse')}
        </Button>
      </div>
      {!fold && (<>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">{t('risk.explanation')}</p>
      {!failed && pausedCount === 0 && items.every(x => x.paused === false) && <p className="mt-2 text-xs font-medium">{t('risk.noNewPauses')}</p>}
      {failed && <p role="alert" className="mt-2 text-xs text-red-600">{t('risk.loadFailed')} <Button variant="link" size="sm" onClick={reload}>{t('state.retry')}</Button></p>}
      <label className="mt-2 flex items-center gap-2 text-xs"><input type="checkbox" checked={history} onChange={e => setHistory(e.target.checked)} />{t('risk.history')}</label>
      <details className="mt-2" open={items.some(x => x.paused === true)}>
        <summary className="cursor-pointer text-xs font-medium">{t('risk.details')}</summary>
        <ul className="mt-2 max-h-72 space-y-2 overflow-y-auto">
          {items.map(item => (
            <li key={item.id} className="rounded-lg bg-background/60 p-2 text-xs leading-5">
              <div className="flex flex-wrap items-center gap-x-2">
                <span className="break-all font-medium">{item.nickname || item.uid}</span>
                <span className="text-muted-foreground">{item.group}</span>
                <strong>{t(item.paused === true ? (item.needs_review ? 'risk.review' : 'risk.paused') : item.paused === null ? 'risk.unknown' : item.historical ? 'risk.historical' : 'risk.warning')}</strong>
              </div>
              <div>{t(item.operation === 'token_refresh' ? 'risk.refresh' : 'risk.background')} · HTTP {item.http_status} · {fmtDateTime(item.ts)}</div>
              {item.paused === true && !item.needs_review && <div>{t('risk.retryAt')} {fmtDateTime(item.pause_until)}</div>}
              {item.needs_review && <div>{t('risk.repeatCount', {n: item.failures})}</div>}
              {isAdmin && item.paused === true && item.needs_review && <Button variant="outline" size="sm" disabled={busy} onClick={() => resume(item.id)}>{t('risk.resume')}</Button>}
              {item.action_error && <p className="text-red-600">{t('risk.pauseFailed')} ({item.action_error})</p>}
            </li>
          ))}
        </ul>
      </details>
      <p className="mt-2 text-xs text-muted-foreground">{t('risk.recovery')} <Link href="/accounts" className="underline">{t('risk.accounts')}</Link></p>
      </>)}
    </section>
  );
}

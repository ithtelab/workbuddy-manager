'use client';

import {useEffect, useState} from 'react';
import Link from 'next/link';
import {ChevronDown, ChevronUp, ShieldAlert} from 'lucide-react';
import {accountRiskApi, errText} from '@/lib/api';
import {taskLogResultText} from '@/lib/tasklog-text';
import {useAsyncAll} from '@/lib/use-async-data';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useRealm} from '@/lib/realm-context';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {fmtDateTime} from '@/lib/format';
import {notify} from '@/lib/toast';
import {Button} from '@/components/ui/button';

function operationLabel(op: string, t: (key: string) => string): string {
  switch (op) {
    case 'token_refresh':
      return t('risk.refresh');
    case 'travel':
      return t('tasks.kindTravel');
    case 'checkin':
      return t('tasks.kindCheckin');
    case 'activity':
      return t('tasks.kindActivity');
    case 'keepalive':
      return t('tasks.kindKeepalive');
    case 'user-resource':
      return t('tasks.kindUserResource');
    case 'school':
      return t('tasks.kindSchool');
    case 'cat':
      return t('tasks.kindCat');
    case 'taskrun':
      return t('tasks.kindTaskrun');
    default:
      return op || t('risk.background');
  }
}

export function AccountRiskWarnings({
  upstreamId = null,
  allGroups = false,
  activeOnly = false,
  hideLowPriority = false,
}: {
  upstreamId?: number | null;
  allGroups?: boolean;
  /**
   * 仅在存在「当前保护性暂停」或「未确认的新预警（非安装前历史回填）」时渲染。
   * 用于仪表盘 / 账号页：没有待处理异常时不占页头，一旦触发新预警或
   * 发生限流暂停就能第一时间看到；确认后且无活跃暂停即自动收走。
   * 过滤在服务端 SQL 完成（`active_only`），不依赖客户端截断 200 行。
   */
  activeOnly?: boolean;
  /** 仪表盘隐藏 `taskrun` 5xx 等低级记录；账号页保持可见。 */
  hideLowPriority?: boolean;
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
  const {values, errors, reload} = useAsyncAll(
    {warnings: () => accountRiskApi.list(
      realm, upstreamId, allGroups, activeOnly ? false : history, activeOnly,
    )},
    [realm, upstreamId, allGroups, activeOnly, history],
  );
  useHeartbeat(reload, 30000);
  const data = values.warnings;
  const failed = !!errors.warnings || !!data?.failed_groups?.length;
  const rawItems = data?.items ?? [];
  const items = hideLowPriority ? rawItems.filter((x) => x.severity !== 'info') : rawItems;
  const pausedCount = new Set(items.filter((x) => x.paused === true).map((x) => `${x.group}:${x.uid}`)).size;
  if (activeOnly ? !items.length : (!items.length && !data?.total && !failed)) return null;
  /** 没被暂停时默认收起：列表里多为非暂停类异常记录（旅行领奖 500、签到 429…），
   *  默认一行顶栏即可提醒运维处理或确认；真有账号被暂停才默认展开。 */
  const fold = foldPref === null ? pausedCount === 0 : foldPref === '1';
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
      for (const item of items.filter((item) => !item.acknowledged)) await accountRiskApi.acknowledge(item.id);
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
        <span className="text-xs text-muted-foreground">{t('risk.count', {n: new Set(items.map((x) => `${x.group}:${x.uid}`)).size})}</span>
        {!failed && <span className="text-xs font-medium">{t('risk.pausedCount', {n: pausedCount})}</span>}
        {isAdmin && items.some((x) => !x.acknowledged) && (
          <Button variant="outline" size="sm" className="ml-auto" disabled={busy} onClick={acknowledge}>{t('risk.acknowledge')}</Button>
        )}
        <Button variant="ghost" size="sm" className={isAdmin && items.some((x) => !x.acknowledged) ? '' : 'ml-auto'}
                aria-expanded={!fold} onClick={toggleFold}>
          {fold ? <ChevronDown className="mr-1 h-3.5 w-3.5" /> : <ChevronUp className="mr-1 h-3.5 w-3.5" />}
          {t(fold ? 'risk.expand' : 'risk.collapse')}
        </Button>
      </div>
      {!fold && (<>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">{t('risk.explanation')}</p>
        {!failed && pausedCount === 0 && items.every((x) => x.paused === false) && <p className="mt-2 text-xs font-medium">{t('risk.noNewPauses')}</p>}
        {failed && <p role="alert" className="mt-2 text-xs text-red-600">{t('risk.loadFailed')} <Button variant="link" size="sm" onClick={reload}>{t('state.retry')}</Button></p>}
        {!activeOnly && (
          <label className="mt-2 flex items-center gap-2 text-xs"><input type="checkbox" checked={history} onChange={(e) => setHistory(e.target.checked)} />{t('risk.history')}</label>
        )}
        {items.length > 0 && (
          <ul className="mt-2 max-h-72 space-y-2 overflow-y-auto">
            {items.map((item) => {
              const translated = (item.detail_cn || item.detail)
                ? taskLogResultText({message_cn: item.detail_cn, message: item.detail})
                : '';
              const rawTail = !item.detail_cn?.includes('：') && item.detail?.includes(': ')
                ? item.detail.slice(item.detail.indexOf(': ') + 2).trim()
                : '';
              const detailText = translated && rawTail && !translated.includes(rawTail)
                ? `${translated} · ${rawTail}`
                : translated;
              return (
                <li key={item.id} className="rounded-lg bg-background/60 p-2.5 text-xs leading-5">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="break-all font-medium">{item.nickname || item.uid}</span>
                    {item.note && (
                      <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[11px] font-medium text-amber-800 dark:text-amber-200">
                        {item.note}
                      </span>
                    )}
                    {item.nickname && item.uid && item.nickname !== item.uid && (
                      <span className="font-mono text-[11px] text-muted-foreground">{item.uid.slice(0, 8)}</span>
                    )}
                    <span className="text-muted-foreground">{item.group}</span>
                    <strong>{t(item.paused === true ? (item.needs_review ? 'risk.review' : 'risk.paused') : item.paused === null ? 'risk.unknown' : item.historical ? 'risk.historical' : 'risk.warning')}</strong>
                  </div>
                  <div className="mt-0.5 font-medium">
                    {operationLabel(item.operation, t)} · HTTP {item.http_status} · {fmtDateTime(item.ts)}
                  </div>
                  {detailText && (
                    <div className="mt-0.5 break-all text-[11px] text-muted-foreground" title={item.detail || undefined}>
                      {detailText}
                    </div>
                  )}
                  {item.paused === true && !item.needs_review && <div>{t('risk.retryAt')} {fmtDateTime(item.pause_until)}</div>}
                  {item.needs_review && <div>{t('risk.repeatCount', {n: item.failures})}</div>}
                  {isAdmin && item.paused === true && item.needs_review && <Button variant="outline" size="sm" className="mt-1" disabled={busy} onClick={() => resume(item.id)}>{t('risk.resume')}</Button>}
                  {item.action_error && <p className="text-red-600">{t('risk.pauseFailed')} ({item.action_error})</p>}
                </li>
              );
            })}
          </ul>
        )}
        <p className="mt-2 text-xs text-muted-foreground">{t('risk.recovery')} <Link href="/accounts" className="underline">{t('risk.accounts')}</Link></p>
      </>)}
    </section>
  );
}

'use client';

import {useCallback, useEffect, useState} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  HardDrive,
  Loader2,
  RefreshCw,
} from 'lucide-react';

import {errText, maintenanceApi} from '@/lib/api';
import type {
  DatabaseCompactResult,
  DatabaseMaintenanceLast,
  DatabaseStatus,
  DatabaseTableCounts,
} from '@/lib/types';
import {fmtDateTime} from '@/lib/format';
import {notify} from '@/lib/toast';
import {useAuth} from '@/lib/auth-context';
import {useI18n, useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';

/** 字节数 → 人类可读。数据库可能到 GB 级，`fmtBytes` 只到 MB 不够用。 */
function fmtSize(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  if (!Number.isFinite(v) || v <= 0) return '0 B';
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(2)} GB`;
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(2)} MB`;
  if (v >= 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${Math.round(v)} B`;
}

/** 五张主要表：key 与后端 `database_status()` 返回的字段一一对应。 */
const TABLE_ROWS: {key: keyof DatabaseTableCounts; label: string}[] = [
  {key: 'request_logs', label: 'dbMaintenance.tableRequestLogs'},
  {key: 'tap_turns', label: 'dbMaintenance.tableTapTurns'},
  {key: 'checkin_logs', label: 'dbMaintenance.tableCheckinLogs'},
  {key: 'account_risk_events', label: 'dbMaintenance.tableRiskEvents'},
  {key: 'task_logs', label: 'dbMaintenance.tableTaskLogs'},
];

function httpStatus(e: unknown): number | undefined {
  return (e as {response?: {status?: number}}).response?.status;
}

/**
 * 设置 → 数据备份里的「数据库维护」卡片（issue #492 第二步）。
 *
 * SQLite 删行不缩文件，`manager.db` 可能长期停在几 GB。压缩是一次显式的
 * 管理员动作：后端会暂停写入、在旁写一份完整副本、校验通过后原子替换。
 * 卡片本身只负责展示与二次确认，安全逻辑全在服务端。
 */
export function DatabaseMaintenancePanel() {
  const t = useT();
  const {locale} = useI18n();
  const {isAdmin} = useAuth();
  const [status, setStatus] = useState<DatabaseStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<DatabaseCompactResult | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await maintenanceApi.database());
      setLoadError(false);
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAdmin) void load();
  }, [isAdmin, load]);

  const runCompact = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await maintenanceApi.compact();
      setResult(r);
      notify.ok(t('dbMaintenance.compactDone', {
        before: fmtSize(r.before_bytes),
        after: fmtSize(r.after_bytes),
      }));
      await load();
    } catch (e) {
      // 已知失败原因按状态码给本地化文案；未知异常在非中文界面兜底成通用提示，
      // 避免把后端的中文原文直接漏到其它语言里。
      const code = httpStatus(e);
      if (code === 507) notify.err(t('dbMaintenance.insufficientSpace'));
      else if (code === 409) notify.err(t('dbMaintenance.busy'));
      else notify.err(locale.startsWith('zh') ? errText(e) : t('dbMaintenance.compactFailed'));
    } finally {
      setBusy(false);
    }
  }, [busy, load, locale, t]);

  if (!isAdmin) return null;

  if (loading) {
    return (
      <div className="rounded-[20px] bg-muted p-5 text-sm text-muted-foreground">
        {t('dbMaintenance.loading')}
      </div>
    );
  }

  if (loadError || !status) {
    return (
      <div className="rounded-[20px] bg-muted p-5">
        <div className="text-sm text-muted-foreground">{t('dbMaintenance.loadFailed')}</div>
        <Button variant="outline" size="sm" className="mt-3" onClick={() => void load()}>
          {t('state.retry')}
        </Button>
      </div>
    );
  }

  const last = status.last;
  const lastText = last ? describeLast(last, t) : t('dbMaintenance.never');

  return (
    <div className="space-y-4">
      {/* ── 说明 ── */}
      <div className="rounded-[20px] bg-muted p-5">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium text-foreground">
          <Database className="h-4 w-4" />
          {t('dbMaintenance.title')}
        </div>
        <p className="text-xs leading-6 text-muted-foreground">
          {t('dbMaintenance.desc')}
        </p>
      </div>

      {/* ── 当前状态 ── */}
      <div className="rounded-[20px] border border-border p-5">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex items-center gap-3 rounded-2xl bg-muted p-3">
            <HardDrive className="h-4 w-4 text-muted-foreground" />
            <div>
              <div className="text-[11px] text-muted-foreground">{t('dbMaintenance.dbSize')}</div>
              <div className="font-mono text-sm">{fmtSize(status.db_bytes)}</div>
            </div>
          </div>
          <div className="flex items-center gap-3 rounded-2xl bg-muted p-3">
            <HardDrive className="h-4 w-4 text-muted-foreground" />
            <div>
              <div className="text-[11px] text-muted-foreground">{t('dbMaintenance.walSize')}</div>
              <div className="font-mono text-sm">{fmtSize(status.wal_bytes)}</div>
            </div>
          </div>
        </div>

        <div className="mt-4 text-sm font-medium">{t('dbMaintenance.tableRows')}</div>
        <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {TABLE_ROWS.map((row) => (
            <div
              key={row.key}
              className="flex items-center justify-between rounded-xl border border-border px-3 py-2 text-xs"
            >
              <span className="text-muted-foreground">{t(row.label)}</span>
              <span className="font-mono">{fmtCount(status.tables[row.key])}</span>
            </div>
          ))}
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span className="font-medium text-foreground">{t('dbMaintenance.lastTitle')}</span>
          {last && (
            <Badge variant={last.ok ? 'secondary' : 'destructive'}>
              {last.ok ? t('dbMaintenance.lastOk') : t('dbMaintenance.lastFailed')}
            </Badge>
          )}
          <span>{lastText}</span>
          {last?.at ? <span>· {fmtDateTime(last.at)}</span> : null}
        </div>

        {result && (
          <div className="mt-4 flex items-start gap-2 rounded-2xl bg-emerald-50 p-3 text-xs leading-6 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-400">
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
            <span>
              {t('dbMaintenance.compactDone', {
                before: fmtSize(result.before_bytes),
                after: fmtSize(result.after_bytes),
              })}
              {' · '}
              {result.saved_bytes > 0
                ? t('dbMaintenance.saved', {saved: fmtSize(result.saved_bytes)})
                : t('dbMaintenance.noSpaceFreed')}
            </span>
          </div>
        )}

        <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-border pt-4">
          <ConfirmDialog
            title={t('dbMaintenance.confirmTitle')}
            description={t('dbMaintenance.confirmDesc')}
            confirmText={t('dbMaintenance.compact')}
            trigger={
              <Button disabled={busy}>
                {busy ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="h-4 w-4" />
                )}
                {busy ? t('dbMaintenance.compacting') : t('dbMaintenance.compact')}
              </Button>
            }
            onConfirm={runCompact}
          />
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => void load()}>
            <RefreshCw className="h-3.5 w-3.5" />
            {t('dbMaintenance.refresh')}
          </Button>
        </div>

        <div className="mt-3 flex items-start gap-2 text-xs leading-6 text-muted-foreground">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{t('dbMaintenance.warn')}</span>
        </div>
      </div>
    </div>
  );
}

/** 行数加千分位；语言无关，避免再引一层 i18n 依赖。 */
function fmtCount(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  return Number.isFinite(v) ? v.toLocaleString() : '0';
}

function describeLast(
  last: DatabaseMaintenanceLast,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  if (last.kind === 'compact') {
    if (last.ok && last.before_bytes != null && last.after_bytes != null) {
      return t('dbMaintenance.compactDone', {
        before: fmtSize(last.before_bytes),
        after: fmtSize(last.after_bytes),
      });
    }
    return t('dbMaintenance.compactFailed');
  }
  if (last.ok) {
    return t('dbMaintenance.pruneRemoved', {n: Number(last.removed ?? 0)});
  }
  return t('dbMaintenance.lastFailed');
}

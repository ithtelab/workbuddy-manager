'use client';

/**
 * 「设置 → 计费价格」：DeepSeek 官方价目表（issue #380）。
 *
 * 这一页回答三个问题：
 *   1. **现在用的是哪一版价目**、上次核对官方价是什么时候（超过 90 天标黄）；
 *   2. **每个模型现在多少钱**（高峰 / 空闲两档，元 / 百万 tokens）；
 *   3. **要不要自己改**——官方调价后不必等新版本，把新价目贴进来即可。
 *
 * 为什么是「版本 + 生效日期」而不是一张当前价目：费用按**调用时刻**的价目算，
 * 改价必须只影响之后。所以界面上永远是「追加一条新版本」，旧版本的账不重算。
 *
 * 为什么允许覆盖：价目随版本分发，但调价往往比发版快；面板里改完立刻生效，
 * 不用等下一个 Release。覆盖内容存在设置里（`pricing_override`），可随时清除。
 */
import {useCallback, useEffect, useState} from 'react';
import {AlertTriangle, ExternalLink, Loader2, RefreshCw, Save, Trash2} from 'lucide-react';

import {settingsApi} from '@/lib/api';
import {fmtMoney} from '@/lib/format';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {PricingInfo, PricingVersion} from '@/lib/types';
import {Button} from '@/components/ui/button';
import {Textarea} from '@/components/ui/textarea';

/** 一条版本的价格表（元 / 百万 tokens）。 */
function VersionTable({version}: {version: PricingVersion}) {
  const t = useT();
  const rows = Object.entries(version.models ?? {});
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[520px] text-xs">
        <thead>
          <tr className="text-left text-[11px] text-muted-foreground">
            <th className="py-1 pr-3 font-normal">{t('pricing.colModel')}</th>
            <th className="py-1 pr-3 font-normal">{t('pricing.colCacheHit')}</th>
            <th className="py-1 pr-3 font-normal">{t('pricing.colCacheMiss')}</th>
            <th className="py-1 pr-3 font-normal">{t('pricing.colOutput')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, rate]) => (
            <tr key={name} className="border-t border-border/40">
              <td className="py-1.5 pr-3 font-mono text-[11px]">
                {name}
                {rate.label && <span className="ml-1 text-muted-foreground">{rate.label}</span>}
              </td>
              {(['cache_hit', 'cache_miss', 'output'] as const).map((field) => (
                <td key={field} className="whitespace-nowrap py-1.5 pr-3 tabular-nums">
                  {rate[field]?.peak != null ? fmtMoney(rate[field]?.peak) : '—'}
                  <span className="text-muted-foreground">
                    {' / '}
                    {rate[field]?.off_peak != null ? fmtMoney(rate[field]?.off_peak) : '—'}
                  </span>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-[10px] text-muted-foreground">{t('pricing.rateHint')}</p>
    </div>
  );
}

export function PricingPanel() {
  const t = useT();
  const [info, setInfo] = useState<PricingInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await settingsApi.pricing();
      setInfo(data);
      setDraft(data.has_override ? JSON.stringify(data.override, null, 2) : '');
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    setSaving(true);
    try {
      let body: Record<string, unknown> | null = null;
      const text = draft.trim();
      if (text) {
        try {
          body = JSON.parse(text) as Record<string, unknown>;
        } catch {
          notify.err(t('pricing.invalidJson'));
          return;
        }
      }
      const next = await settingsApi.savePricing(body);
      setInfo(next);
      setDraft(next.has_override ? JSON.stringify(next.override, null, 2) : '');
      notify.ok(t('pricing.saved'));
    } catch (exc) {
      notify.err(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSaving(false);
    }
  };

  const clear = async () => {
    setSaving(true);
    try {
      const next = await settingsApi.savePricing(null);
      setInfo(next);
      setDraft('');
      notify.ok(t('pricing.cleared'));
    } catch (exc) {
      notify.err(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSaving(false);
    }
  };

  if (loading && !info) {
    return (
      <div className="flex items-center gap-2 rounded-[20px] bg-muted p-5 text-xs text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t('common.loading')}
      </div>
    );
  }

  if (error && !info) {
    return (
      <div className="rounded-[20px] bg-muted p-5 text-xs text-muted-foreground">
        {t('state.partialFailed')}
        <Button variant="ghost" size="sm" className="ml-2 h-6 rounded-full" onClick={() => void load()}>
          <RefreshCw className="h-3.5 w-3.5" />
          {t('state.retry')}
        </Button>
      </div>
    );
  }

  if (!info) return null;

  const active = info.versions[info.versions.length - 1];
  const stale = active?.age_days != null && active.age_days > info.stale_after_days;

  return (
    <div className="space-y-4">
      <div className="rounded-[20px] bg-muted p-5 text-xs leading-6">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <span className="text-sm font-medium">{t('pricing.title')}</span>
          <span className={info.is_peak_now ? 'text-amber-600 dark:text-amber-400' : 'text-emerald-600 dark:text-emerald-400'}>
            {info.is_peak_now ? t('pricing.peakNow') : t('pricing.offPeakNow')}
          </span>
        </div>
        <p className="text-muted-foreground">{t('pricing.desc')}</p>
        <div className="mt-3 space-y-1">
          <div>
            {t('pricing.activeVersion')}：<span className="font-mono">{active?.effective_from || '—'}</span>
            {active?.overridden && (
              <span className="ml-2 rounded-full bg-amber-500/12 px-2 py-0.5 text-[10px] text-amber-600 dark:text-amber-400">
                {t('pricing.overridden')}
              </span>
            )}
          </div>
          <div className="text-muted-foreground">
            {t('pricing.verifiedAt')}：{active?.verified_at || '—'}
            {active?.age_days != null && ` (${t('pricing.ageDays', {n: active.age_days})})`}
          </div>
          {active?.source && (
            <a
              href={active.source}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-muted-foreground underline-offset-2 hover:underline"
            >
              <ExternalLink className="h-3 w-3" />
              {t('pricing.source')}
            </a>
          )}
        </div>

        {stale && (
          <div className="mt-3 flex items-start gap-2 rounded-xl bg-amber-500/10 p-2 text-[11px] text-amber-700 dark:text-amber-400">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{t('pricing.stale', {n: info.stale_after_days})}</span>
          </div>
        )}
        {info.missing_holiday_years.length > 0 && (
          <div className="mt-2 flex items-start gap-2 rounded-xl bg-amber-500/10 p-2 text-[11px] text-amber-700 dark:text-amber-400">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{t('pricing.missingHolidays', {years: info.missing_holiday_years.join('、')})}</span>
          </div>
        )}
      </div>

      <div className="rounded-[20px] bg-muted p-5">
        <div className="mb-3 text-sm font-medium">{t('pricing.tableTitle')}</div>
        {active && <VersionTable version={active} />}
      </div>

      <div className="rounded-[20px] bg-muted p-5">
        <div className="mb-2 text-sm font-medium">{t('pricing.overrideTitle')}</div>
        <p className="mb-3 text-[11px] leading-5 text-muted-foreground">{t('pricing.overrideDesc')}</p>
        <Textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          spellCheck={false}
          placeholder={'{\n  "versions": [\n    {"effective_from": "2026-12-01", "models": {...}}\n  ]\n}'}
          className="min-h-[160px] font-mono text-[11px]"
        />
        <div className="mt-3 flex flex-wrap gap-2">
          <Button size="sm" className="rounded-full" disabled={saving} onClick={() => void save()}>
            {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
            {t('common.save')}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="rounded-full"
            disabled={saving || !info.has_override}
            onClick={() => void clear()}
          >
            <Trash2 className="h-3.5 w-3.5" />
            {t('pricing.clearOverride')}
          </Button>
        </div>
      </div>
    </div>
  );
}

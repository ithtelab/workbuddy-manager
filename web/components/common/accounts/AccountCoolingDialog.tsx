'use client';

import {useEffect, useMemo, useState} from 'react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {accountApi, errText} from '@/lib/api';
import type {Account} from '@/lib/types';
import {fmtDateTime, fmtRemain} from '@/lib/format';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';

type PresetId = '1h' | '6h' | '12h' | '24h' | '3d' | '7d' | 'next_04' | 'permanent' | 'custom';
type CustomUnit = 'hours' | 'days' | 'minutes';

export function AccountCoolingDialog({
  account,
  open,
  onOpenChange,
  onSuccess,
  upstreamId,
}: {
  account: Account | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: () => void;
  upstreamId?: number | null;
}) {
  const t = useT();
  const [preset, setPreset] = useState<PresetId>('24h');
  const [customVal, setCustomVal] = useState('2');
  const [customUnit, setCustomUnit] = useState<CustomUnit>('hours');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setPreset('24h');
      setCustomVal('2');
      setCustomUnit('hours');
      setReason('');
      setBusy(false);
    }
  }, [open, account?.file]);

  const targetSeconds = useMemo(() => {
    if (preset === 'permanent') return 73 * 365 * 86400;
    if (preset === '1h') return 3600;
    if (preset === '6h') return 21600;
    if (preset === '12h') return 43200;
    if (preset === '24h') return 86400;
    if (preset === '3d') return 3 * 86400;
    if (preset === '7d') return 7 * 86400;
    if (preset === 'next_04') {
      const now = new Date();
      const next4 = new Date(now);
      if (now.getHours() >= 4) {
        next4.setDate(next4.getDate() + 1);
      }
      next4.setHours(4, 0, 0, 0);
      return Math.max(60, Math.floor((next4.getTime() - now.getTime()) / 1000));
    }
    if (preset === 'custom') {
      const v = Math.max(1, parseInt(customVal, 10) || 1);
      if (customUnit === 'minutes') return v * 60;
      if (customUnit === 'days') return v * 86400;
      return v * 3600;
    }
    return 86400;
  }, [preset, customVal, customUnit]);

  const etaText = useMemo(() => {
    if (preset === 'permanent') return t('accounts.forceCoolingPermanentETA');
    const at = Date.now() + targetSeconds * 1000;
    return fmtDateTime(at);
  }, [preset, targetSeconds, t]);

  const currentCoolingNotice = useMemo(() => {
    if (!account?.cooling) return null;
    const secs = account.cool_remaining_sec;
    if (typeof secs === 'number' && secs >= 365 * 86400 * 5) {
      return t('accounts.forceCoolingPermanentCurrent');
    }
    const left = typeof secs === 'number' && secs > 0 ? fmtRemain(secs) : '';
    const until = typeof secs === 'number' && secs > 0 ? fmtDateTime(Date.now() + secs * 1000) : '';
    return t('accounts.forceCoolingCurrent', {left: left || '—', until: until || '—'});
  }, [account?.cooling, account?.cool_remaining_sec, t]);

  const PRESETS: {id: PresetId; label: string}[] = [
    {id: '1h', label: `1 ${t('accounts.forceCoolingUnitHours')}`},
    {id: '6h', label: `6 ${t('accounts.forceCoolingUnitHours')}`},
    {id: '12h', label: `12 ${t('accounts.forceCoolingUnitHours')}`},
    {id: '24h', label: `24 ${t('accounts.forceCoolingUnitHours')}`},
    {id: '3d', label: `3 ${t('accounts.forceCoolingUnitDays')}`},
    {id: '7d', label: `7 ${t('accounts.forceCoolingUnitDays')}`},
    {id: 'next_04', label: t('accounts.forceCoolingNext4AM')},
    {id: 'permanent', label: t('accounts.forceCoolingPermanent')},
    {id: 'custom', label: t('accounts.forceCoolingCustom')},
  ];

  async function submit() {
    if (!account || busy) return;
    setBusy(true);
    try {
      const isPerm = preset === 'permanent';
      const res = await accountApi.forceCooling(
        account.file,
        {
          duration_seconds: isPerm ? undefined : targetSeconds,
          until: isPerm ? '2099-12-31T23:59:59Z' : undefined,
          permanent: isPerm,
          reason: reason.trim() || (isPerm ? t('accounts.forceCoolingPermanentReason') : t('accounts.forceCoolingReasonPlaceholder')),
        },
        upstreamId,
      );
      if (res.ok === false) {
        notify.err(res.message || t('accounts.forceCoolingFail'));
        return;
      }
      notify.ok(t('accounts.forceCoolingDone'));
      onOpenChange(false);
      onSuccess();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[440px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>{t('accounts.forceCoolingTitle')}</DialogTitle>
          <DialogDescription>
            {t('accounts.forceCoolingDesc')}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
          <div className="space-y-4 px-6 pb-2">
            {currentCoolingNotice && (
              <div className="rounded-lg border border-sky-500/30 bg-sky-500/10 p-2.5 text-xs text-sky-700 dark:text-sky-300">
                {currentCoolingNotice}
              </div>
            )}

            <div>
              <div className="mb-2 text-xs font-medium text-muted-foreground">
                {t('accounts.forceCoolingDuration')}
              </div>
              <div className="grid grid-cols-3 sm:grid-cols-4 gap-1.5">
                {PRESETS.map((p) => (
                  <Button
                    key={p.id}
                    type="button"
                    size="sm"
                    variant={preset === p.id ? 'default' : 'outline'}
                    className="h-8 text-xs rounded-lg"
                    onClick={() => setPreset(p.id)}
                  >
                    {p.label}
                  </Button>
                ))}
              </div>

              {preset === 'custom' && (
                <div className="mt-2.5 flex items-center gap-2">
                  <Input
                    type="number"
                    min="1"
                    max="365"
                    value={customVal}
                    onChange={(e) => setCustomVal(e.target.value)}
                    className="h-8 text-sm"
                    placeholder={t('accounts.forceCoolingCustomPlaceholder')}
                    autoFocus
                  />
                  <select
                    value={customUnit}
                    onChange={(e) => setCustomUnit(e.target.value as CustomUnit)}
                    className="h-8 rounded-md border border-input bg-background px-2 text-xs"
                  >
                    <option value="hours">{t('accounts.forceCoolingUnitHours')}</option>
                    <option value="days">{t('accounts.forceCoolingUnitDays')}</option>
                    <option value="minutes">{t('accounts.forceCoolingUnitMinutes')}</option>
                  </select>
                </div>
              )}

              <div className="mt-2 text-xs text-muted-foreground">
                {t('accounts.forceCoolingETA', {time: etaText})}
              </div>
            </div>

            <div className="space-y-1">
              <label className="text-xs font-medium text-muted-foreground">
                {t('accounts.forceCoolingReason')}
              </label>
              <Input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder={t('accounts.forceCoolingReasonPlaceholder')}
                maxLength={50}
                className="h-8 text-sm"
              />
            </div>
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" className="rounded-full" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button className="rounded-full" onClick={submit} disabled={busy}>
            {account?.cooling ? t('accounts.forceCoolingConfirmUpdate') : t('accounts.forceCoolingConfirm')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

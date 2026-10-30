'use client';

import {rateLimitedModels} from '@/lib/account-status';
import {fmtDateTime} from '@/lib/format';
import {useT} from '@/lib/i18n/provider';
import type {Account} from '@/lib/types';
import {Button} from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';

export function AccountModelLimitsDialog({account, open, onOpenChange}: {
  account: Account | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const limited = account ? rateLimitedModels(account) : [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-w-[560px] flex-col">
        <DialogHeader className="shrink-0">
          <DialogTitle>{t('accounts.modelLimitDetails', {n: limited.length})}</DialogTitle>
          <DialogDescription className="break-all">
            {account?.nickname || account?.uid || account?.file}
          </DialogDescription>
        </DialogHeader>
        <ul className="min-h-0 space-y-3 overflow-y-auto px-6 pb-2 text-xs">
          {limited.map((m) => {
            const until = account?.rate_limited_models?.find((x) => x.model === m.model)?.until;
            const missing = m.reason.startsWith('11102');
            return (
              <li key={m.model} className="space-y-1 rounded-xl border p-3">
                <div className="break-all font-mono font-medium">{m.model}</div>
                <p className="text-amber-600 dark:text-amber-400">
                  {t(missing ? 'accounts.modelMissing' : 'accounts.modelLimited')}
                </p>
                {m.reason && <p className="break-words text-muted-foreground">{m.reason}</p>}
                {!missing && until && (
                  <p className="text-muted-foreground">
                    {t('accounts.modelLimitUntil', {at: fmtDateTime(Date.parse(until) / 1000)})}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
        <DialogFooter className="shrink-0">
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            {t('common.close')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

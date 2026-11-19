'use client';

import {useEffect, useRef, useState} from 'react';
import {Download, Loader2, Upload, UsersRound} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {useT} from '@/lib/i18n/provider';
import {accountApi, errText} from '@/lib/api';
import {notify} from '@/lib/toast';
import type {UpstreamEndpoint} from '@/lib/types';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

export function UploadAccountsButton({
  upstreamId,
  groups,
  onSuccess,
}: {
  upstreamId?: number | null;
  groups: UpstreamEndpoint[];
  onSuccess?: () => void;
}) {
  const t = useT();
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [overwrite, setOverwrite] = useState(false);
  const [exportGroup, setExportGroup] = useState('default');

  useEffect(() => {
    setExportGroup(upstreamId == null ? 'default' : String(upstreamId));
  }, [upstreamId]);

  const groupOptions = groups.filter((group) => !group.is_default && group.id != null);
  const selectedExportId = exportGroup === 'default' ? null : Number(exportGroup);
  const selectedExportGroup = exportGroup === 'default'
    ? undefined
    : groupOptions.find((group) => group.id === selectedExportId);

  async function upload(files: File[]) {
    if (!files.length || busy) return;
    setBusy(true);
    try {
      const result = await accountApi.upload(files, upstreamId, overwrite);
      if (result.added.length || result.overwritten.length) {
        notify.ok(
          t('accounts.uploadDone'),
          t('accounts.uploadDoneDetail', {
            added: result.added.length,
            overwritten: result.overwritten.length,
          }),
        );
        onSuccess?.();
      }
      if (result.rejected.length || result.failed.length) {
        notify.err(
          t('accounts.uploadPartial'),
          [...result.rejected, ...result.failed]
            .map((item) => `${item.file}: ${item.message}`)
            .join('\n'),
        );
      }
    } catch (error) {
      notify.err(errText(error));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  }

  async function exportAccounts() {
    if (busy || (selectedExportGroup && !selectedExportGroup.auth_dir)) return;
    setBusy(true);
    try {
      const blob = await accountApi.exportZip(selectedExportId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = 'accounts.zip';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      notify.ok(t('accounts.exportDone'), t('accounts.exportDoneDetail'));
    } catch (error) {
      notify.err(errText(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept=".json,application/json"
        multiple
        className="hidden"
        onChange={(event) => void upload(Array.from(event.target.files ?? []))}
      />
      <Button
        size="sm"
        variant="outline"
        className="rounded-full"
        disabled={busy}
        onClick={() => setOpen(true)}
        title={t('accounts.importExport')}
      >
        {busy ? <Loader2 className="animate-spin" /> : <UsersRound />}
        <span>{t('accounts.importExport')}</span>
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-[520px]" showCloseButton>
          <DialogHeader>
            <DialogTitle>{t('accounts.importExport')}</DialogTitle>
            <DialogDescription>{t('accounts.importExportDesc')}</DialogDescription>
          </DialogHeader>
          <div className="flex w-full flex-col gap-4 px-6 pb-6">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-2xl border border-border/70 p-4">
                <div className="mb-3 flex items-center gap-2 text-sm font-medium">
                  <Upload className="h-4 w-4" />
                  {t('accounts.importAccount')}
                </div>
                <p className="mb-4 text-xs leading-5 text-muted-foreground">
                  {t('accounts.importAccountDesc')}
                </p>
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={overwrite}
                    disabled={busy}
                    onChange={(event) => setOverwrite(event.target.checked)}
                  />
                  {t('accounts.uploadAllowOverwrite')}
                </label>
                <Button
                  className="mt-4 w-full rounded-full"
                  disabled={busy}
                  onClick={() => inputRef.current?.click()}
                >
                  <Upload className="h-4 w-4" />
                  {t('accounts.importAccount')}
                </Button>
              </div>
              <div className="rounded-2xl border border-border/70 p-4">
                <div className="mb-3 flex items-center gap-2 text-sm font-medium">
                  <Download className="h-4 w-4" />
                  {t('accounts.exportAccount')}
                </div>
                <p className="mb-4 text-xs leading-5 text-muted-foreground">
                  {t('accounts.exportAccountDesc')}
                </p>
                <Select value={exportGroup} onValueChange={setExportGroup} disabled={busy}>
                  <SelectTrigger className="h-9 w-full rounded-full text-xs" aria-label={t('accounts.exportGroup')}>
                    <SelectValue placeholder={t('accounts.exportGroup')} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="default">{t('accounts.groupDefault')}</SelectItem>
                    {groupOptions.map((group) => (
                      <SelectItem key={group.id} value={String(group.id)}>
                        {group.name}{!group.auth_dir ? ` ${t('accounts.groupNoDirShort')}` : ''}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {/* 导出要把账号凭据打包带走，按仓库约定走二次确认 */}
                <ConfirmDialog
                  title={t('accounts.exportConfirmTitle')}
                  description={t('accounts.exportConfirmDesc')}
                  confirmText={t('accounts.exportAccount')}
                  destructive
                  trigger={
                    <Button
                      variant="outline"
                      className="mt-4 w-full rounded-full"
                      disabled={busy || Boolean(selectedExportGroup && !selectedExportGroup.auth_dir)}
                    >
                      <Download className="h-4 w-4" />
                      {t('accounts.exportAccount')}
                    </Button>
                  }
                  onConfirm={() => void exportAccounts()}
                />
              </div>
            </div>
            <Button variant="outline" className="w-full rounded-full" onClick={() => setOpen(false)}>
              {t('common.cancel')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

'use client';

import {useRef, useState} from 'react';
import {Upload, Loader2} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {useT} from '@/lib/i18n/provider';
import {accountApi, errText} from '@/lib/api';
import {notify} from '@/lib/toast';

const DEFAULT_UPLOAD_TTL = 30 * 24 * 60 * 60 * 1000;

function addDefaultExpiry(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(addDefaultExpiry);
  if (!value || typeof value !== 'object') return value;

  const source = value as Record<string, unknown>;
  if (Array.isArray(source.accounts)) {
    return {...source, accounts: source.accounts.map(addDefaultExpiry)};
  }
  if (source.auth && typeof source.auth === 'object' && !Array.isArray(source.auth)) {
    const auth = source.auth as Record<string, unknown>;
    if (auth.expiresAt == null && auth.expires_at == null) {
      return {...source, auth: {...auth, expiresAt: Math.floor((Date.now() + DEFAULT_UPLOAD_TTL) / 1000)}};
    }
    return source;
  }
  if (source.expiresAt == null && source.expires_at == null && source.expiresIn == null && source.expires_in == null) {
    return {...source, expires_at: Math.floor((Date.now() + DEFAULT_UPLOAD_TTL) / 1000)};
  }
  return source;
}

async function prepareUploadFile(file: File): Promise<File> {
  try {
    const parsed = JSON.parse(await file.text());
    const normalized = addDefaultExpiry(parsed);
    return new File([JSON.stringify(normalized)], file.name, {type: 'application/json'});
  } catch {
    return file;
  }
}

export function UploadAccountsButton({
  upstreamId,
  onSuccess,
}: {
  upstreamId?: number | null;
  onSuccess?: () => void;
}) {
  const t = useT();
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);

  async function upload(files: File[]) {
    if (!files.length || busy) return;
    setBusy(true);
    try {
      const result = await accountApi.upload(await Promise.all(files.map(prepareUploadFile)), upstreamId);
      if (result.uploaded.length) {
        notify.ok(
          t('accounts.uploadDone'),
          t('accounts.uploadDoneDetail', {n: result.uploaded.length}),
        );
        onSuccess?.();
      }
      if (result.failed.length) {
        notify.err(
          t('accounts.uploadPartial'),
          result.failed.map((item) => `${item.file}: ${item.message}`).join('\n'),
        );
      }
    } catch (error) {
      notify.err(errText(error));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = '';
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
        onClick={() => inputRef.current?.click()}
        title={t('accounts.uploadJson')}
      >
        {busy ? <Loader2 className="animate-spin" /> : <Upload />}
        <span>{t('accounts.uploadJson')}</span>
      </Button>
    </>
  );
}

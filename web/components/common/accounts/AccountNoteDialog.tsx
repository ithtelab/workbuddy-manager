'use client';

import {useEffect, useState} from 'react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {accountApi, errText} from '@/lib/api';
import type {Account} from '@/lib/types';
import {Button} from '@/components/ui/button';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {Input} from '@/components/ui/input';

/**
 * 账号备注的编辑弹窗（issue #67）。
 *
 * 为什么要这个东西：用手机号邀请注册的账号，昵称往往认不出是谁（`昵称-1`、
 * 空昵称之类），删号时不知道该删哪个。备注就是给账号起一个**自己记得住**的名字。
 *
 * 存法：备注存在本端库里、按 **uid** 关联（`db.account_notes`），不写进上游的
 * 账号文件——那是上游按自己 schema 读写的文件，塞自定义字段会被它覆盖。按 uid
 * 存的直接好处：临时停用（改文件名）之后备注还在。
 */
export function AccountNoteDialog({
  account,
  open,
  onOpenChange,
  onSaved,
  upstreamId,
}: {
  /** null = 未选中任何账号（弹窗关闭态） */
  account: Account | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
  /** 账号所在分组（多账号池）：备注端点按分组解析账号文件；null = 默认分组 */
  upstreamId?: number | null;
}) {
  const t = useT();
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  // 每次打开都以该账号当前的备注为初值：否则会拿着上一个账号的内容去编辑，
  // 一点保存就把备注写串（这类串号在「备注」上尤其难发现）。
  useEffect(() => {
    if (open) {
      setNote(account?.note ?? '');
      setBusy(false);
    }
  }, [open, account?.file, account?.note]);

  async function save() {
    if (!account || busy) return;
    setBusy(true);
    try {
      const r = await accountApi.setNote(account.file, note.trim(), upstreamId);
      notify.ok(t('accounts.noteSaved'), r.note ? r.note : t('accounts.noteCleared'));
      onOpenChange(false);
      onSaved();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[380px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>{t('accounts.noteTitle')}</DialogTitle>
          <DialogDescription>
            {t('accounts.noteDesc', {name: account?.nickname || account?.uid || ''})}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
          <div className="px-6 pb-2">
            <Input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={t('accounts.notePlaceholder')}
              maxLength={100}
              autoFocus
              onKeyDown={(e) => {
                if (e.key === 'Enter') void save();
              }}
            />
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" className="rounded-full" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button className="rounded-full" onClick={save} disabled={busy}>
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

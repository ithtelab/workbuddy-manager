'use client';

/**
 * 「移动到分组」弹窗（账号页）。
 *
 * 移动的是**账号文件本身**：不加参数、不改凭证，落点由目标分组的账号目录决定
 * （见 server/routers/accounts.py 的 move 端点）。所以这里只要一个目标选择，
 * 以及「哪些组能收」——没有本地账号目录的组收不了（选了会 409），直接禁用并
 * 在选项里说明，而不是让用户点了才知道。
 */
import {useEffect, useState} from 'react';

import {accountApi, errText} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {Account, UpstreamEndpoint} from '@/lib/types';
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
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

/** 默认分组在 Select 里的值（后端约定：0 = 默认分组，见 move 端点） */
const DEFAULT_VALUE = '__default__';

export function MoveAccountDialog({
  open,
  onOpenChange,
  account,
  groups,
  fromGroupId,
  onMoved,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** 要移动的账号；null = 弹窗未打开 */
  account: Account | null;
  /** 全部分组（含默认行）；无本地目录的组不可作为目标 */
  groups: UpstreamEndpoint[];
  /** 账号当前所在分组；null = 默认分组 */
  fromGroupId: number | null;
  onMoved?: () => void;
}) {
  const t = useT();
  const [target, setTarget] = useState('');
  const [busy, setBusy] = useState(false);
  const accountName = account ? account.nickname || account.uid : '';

  useEffect(() => {
    if (open) setTarget('');
  }, [open]);

  async function submit() {
    if (!account || busy || !target) return;
    const toId = target === DEFAULT_VALUE ? 0 : Number(target);
    setBusy(true);
    try {
      const r = await accountApi.move(account.file, toId, fromGroupId);
      notify.ok(t('accounts.moved', {name: r.to.name}));
      onOpenChange(false);
      onMoved?.();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  // 候选：排除当前所在分组；默认行只在「当前不是默认分组」时可选
  const candidates = groups.filter((g) => {
    if (g.is_default) return fromGroupId != null;
    return g.id !== fromGroupId;
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[420px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>{t('accounts.moveTitle', {name: accountName})}</DialogTitle>
          <DialogDescription>{t('accounts.moveDesc')}</DialogDescription>
        </DialogHeader>
        {/* 布局类写在 Body **里面**这层：DialogBody 的 className 落在 ScrollArea
            的 Root 上，写在那儿内层字段拿不到（见 DialogBody 的注释）。 */}
        <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
          <div className="px-6 pb-2">
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">
                {t('accounts.moveTargetLabel')}
              </Label>
              <Select value={target} onValueChange={setTarget}>
                <SelectTrigger aria-label={t('accounts.moveTargetLabel')}>
                  <SelectValue placeholder={t('accounts.movePick')} />
                </SelectTrigger>
                <SelectContent>
                  {candidates.map((g) => {
                    const canMove = !!g.auth_dir;
                    const value = g.is_default ? DEFAULT_VALUE : String(g.id);
                    return (
                      <SelectItem key={value} value={value} disabled={!canMove}>
                        {(g.is_default ? t('accounts.groupDefault') : g.name)
                          + (canMove ? '' : t('accounts.moveNoDirSuffix'))}
                      </SelectItem>
                    );
                  })}
                </SelectContent>
              </Select>
            </div>
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" className="rounded-full" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button className="rounded-full" disabled={busy || !target} onClick={submit}>
            {t('accounts.moveConfirm')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

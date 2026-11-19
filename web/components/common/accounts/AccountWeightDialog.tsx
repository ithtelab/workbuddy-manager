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
 * 账号手写权重（issue #187）。
 *
 * 语义是**乘数**：上游选号本来就按「余额 + 快过期占比 + 闲置时长」加权随机，
 * 权重把结果再乘一遍。所以：
 *   · 2 = 同等条件下被抽中的机会约为默认的两倍；0.5 = 减半；
 *   · **不是**固定优先 —— 高权重号不会把低权重号饿死（那会让闲置号的积分白过期，
 *     而同一条 issue 的另一半诉求正是「快过期的先用」）；
 *   · 不改变可用性：冷却 / 熔断 / 禁用 / 在途占满 / 成本分层照旧拦截。
 *
 * 存法：写进上游 auth 文件顶层 `weight`（不是本端库）—— 选号发生在上游，
 * 它必须能读到。改完由后端请求重载上游，否则要等下一次重启才生效。
 */
export function AccountWeightDialog({
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
  /** 账号所在分组（多账号池）：端点按分组解析账号文件；null = 默认分组 */
  upstreamId?: number | null;
}) {
  const t = useT();
  const [value, setValue] = useState('1');
  const [busy, setBusy] = useState(false);

  // 每次打开都以该账号的**当前值**为初值：否则会拿着上一个账号的权重去编辑，
  // 一保存就把设置写串（与备注同一类串号隐患）。
  useEffect(() => {
    if (open) {
      const w = Number(account?.weight || 0);
      setValue(w > 0 ? String(w) : '1');
      setBusy(false);
    }
  }, [open, account?.file, account?.weight]);

  async function save() {
    if (!account || busy) return;
    const raw = value.trim();
    // 空 / 1 = 恢复默认：写 0 让后端把这个键删掉，而不是往文件里塞 weight: 1。
    const weight = raw === '' ? 0 : Number(raw);
    if (!Number.isFinite(weight)) {
      notify.err(t('accounts.weightInvalid'));
      return;
    }
    if (weight !== 0 && weight !== 1 && (weight < 0.1 || weight > 100)) {
      notify.err(t('accounts.weightInvalid'));
      return;
    }
    setBusy(true);
    try {
      const r = await accountApi.setWeight(
        account.file, weight === 1 ? 0 : weight, upstreamId);
      (r.weight > 0 ? notify.ok : notify.info)(
        r.weight > 0
          ? t('accounts.weightSaved', {weight: String(r.weight)})
          : t('accounts.weightCleared'),
      );
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
      <DialogContent className="max-w-[420px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>{t('accounts.weightTitle')}</DialogTitle>
          <DialogDescription>
            {t('accounts.weightDesc', {name: account?.nickname || account?.uid || ''})}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
          <div className="space-y-2 px-6 pb-2">
            <Input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              inputMode="decimal"
              placeholder="1"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === 'Enter') void save();
              }}
            />
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              {t('accounts.weightHint')}
            </p>
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

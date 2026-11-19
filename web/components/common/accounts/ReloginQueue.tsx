'use client';

import {useEffect, useMemo, useState} from 'react';
import {RefreshCw, TriangleAlert, Users, Zap, CheckCircle2} from 'lucide-react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {accountApi, errText} from '@/lib/api';
import type {Account} from '@/lib/types';
import {availabilityLabelKey, availabilityOf, needsRelogin} from '@/lib/account-status';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {AddAccountDialog} from './AddAccountDialog';

/**
 * 重登队列只收可尝试修复凭证的系统禁用账号，排除上游账号封禁。
 */
export function ReloginQueueDialog({
  open,
  onOpenChange,
  accounts,
  upstreamId,
  onSuccess,
  adminApiEnabled,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 已合并池状态、并按当前版本过滤后的账号列表 */
  accounts: Account[];
  upstreamId?: number | null;
  onSuccess?: () => void;
  /** 上游「账号管理接口」（admin.enabled）开关；false = 未开启，解封不可用 */
  adminApiEnabled?: boolean | null;
}) {
  const t = useT();
  const [reloginOpen, setReloginOpen] = useState(false);
  const [busyFile, setBusyFile] = useState<string | null>(null);
  const [selectedIndex, setSelectedIndex] = useState(0);

  // 用户主动停用及上游封禁都不进入重登队列。
  const queue = useMemo(
    () => accounts.filter(needsRelogin),
    [accounts],
  );

  // 账号解禁离开队列或总数缩减时，防止下标越界
  useEffect(() => {
    if (selectedIndex >= queue.length) {
      setSelectedIndex(Math.max(0, queue.length - 1));
    }
  }, [queue.length, selectedIndex]);

  const current = queue[selectedIndex] ?? queue[0];

  /** ping 测试 = 连通性测试（与账号行内那个 Zap 按钮同一接口） */
  async function ping(a: Account) {
    setBusyFile(a.file);
    try {
      const res = await accountApi.test(a.file, upstreamId);
      (res.ok ? notify.ok : notify.warn)(res.message || t('accounts.testDone'));
      // ping 通过说明该账号已可用：刷新一次让状态与队列同步（后端可能在后台
      // 刚完成「登录后自动解除系统禁用」，列表还停在旧快照上）。
      if (res.ok) onSuccess?.();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusyFile(null);
    }
  }

  /**
   * 重登成功后的收尾：直接交给调用方。
   *
   * 「立刻刷一次 + 几次延迟刷新」的节奏由账号页统一持有（`handleLoginSuccess`）：
   * 「添加账户」与「重登」两条路以前各写一份，刷新时机不同 —— 同一件事在两条路上
   * 表现不一致，正是这次要消灭的东西。后端在重登响应里已经带回 `revive` 的执行
   * 结果（见 `_revive_inline`），不再需要靠多刷几次去「等它好」。
   */
  function handleReloginSuccess() {
    onSuccess?.();
  }

  function statusBadge(a: Account) {
    const tier = availabilityOf(a);
    const reason = String(a.disabled_reason || '');
    return (
      <Badge
        variant="destructive"
        className="rounded-full shrink-0"
        title={reason ? t('accounts.disabledReason', {reason}) : undefined}
      >
        {t(availabilityLabelKey(tier, a))}
      </Badge>
    );
  }

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          className="max-w-[640px] max-h-[85vh] flex flex-col p-0 overflow-hidden"
          showCloseButton
        >
          <DialogHeader className="px-6 pt-6 pb-3 border-b border-border/40">
            <div className="flex items-center gap-2">
              <Users className="h-5 w-5 text-muted-foreground" />
              <DialogTitle className="text-base font-semibold">
                {t('accounts.reloginQueueTitle')}
              </DialogTitle>
              <Badge variant="secondary" className="rounded-full text-xs ml-1">
                {t('accounts.reloginQueueCount', {count: queue.length})}
              </Badge>
            </div>
            <DialogDescription className="text-xs text-muted-foreground mt-1">
              {t('accounts.reloginQueueDesc')}
            </DialogDescription>
          </DialogHeader>

          {/* 上游管理接口未开启时，重登只换令牌、清不掉禁用位——弹窗顶部先讲清，
              避免用户白重登一遍才发现在白忙（与徽章悬停提示同一口径）。 */}
          {adminApiEnabled === false && (
            <div className="flex items-start gap-2.5 border-b border-border/40 bg-amber-500/10 px-6 py-3">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
              <p className="text-xs leading-4 text-amber-700 dark:text-amber-300">
                {t('accounts.disabledQueueHintAdminOff')}
              </p>
            </div>
          )}

          {queue.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-14 px-6 text-center space-y-3">
              <div className="grid h-12 w-12 place-items-center rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
                <CheckCircle2 className="h-6 w-6" />
              </div>
              <div className="text-sm font-medium">{t('accounts.reloginQueueEmptyTitle')}</div>
              <p className="text-xs text-muted-foreground max-w-sm">
                {t('accounts.reloginQueueEmptyDesc')}
              </p>
            </div>
          ) : (
            <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
              {/* 当前操作目标卡片 */}
              {current && (
                <div className="rounded-xl border border-border/70 bg-muted/40 p-4 space-y-3">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 text-xs font-semibold text-primary">
                      <span className="grid h-5 w-5 place-items-center rounded-full bg-primary text-[10px] font-bold text-primary-foreground">
                        {selectedIndex + 1}
                      </span>
                      <span>{t('accounts.reloginQueueCurrent')}</span>
                    </div>
                    {statusBadge(current)}
                  </div>

                  <div className="space-y-1">
                    <div className="text-sm font-semibold truncate">
                      {current.nickname || t('accounts.unnamed')}
                    </div>
                    <div className="font-mono text-xs text-muted-foreground truncate">
                      UID: {current.uid}
                    </div>
                    {current.disabled_reason ? (
                      <div className="text-xs text-muted-foreground truncate">
                        {t('accounts.reloginQueueReason')}{String(current.disabled_reason)}
                      </div>
                    ) : null}
                  </div>

                  <div className="flex flex-wrap items-center justify-between gap-2 pt-2 border-t border-border/40">
                    <span className="text-[11px] text-muted-foreground">
                      {t('accounts.reloginQueueCurrentHint')}
                    </span>
                    <div className="flex gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        className="rounded-full h-8 text-xs gap-1.5"
                        disabled={busyFile === current.file}
                        onClick={() => ping(current)}
                      >
                        <Zap className="h-3.5 w-3.5" />
                        {t('accounts.reloginQueuePing')}
                      </Button>
                      <Button
                        size="sm"
                        className="rounded-full h-8 text-xs gap-1.5"
                        onClick={() => setReloginOpen(true)}
                      >
                        <RefreshCw className="h-3.5 w-3.5" />
                        {t('accounts.reloginQueueReloginCurrent')}
                      </Button>
                    </div>
                  </div>
                </div>
              )}

              {/* 队列清单（点击任意行直接跳转并选中该账号） */}
              <div className="space-y-2 mt-4">
                <div className="flex items-center justify-between px-1 text-xs text-muted-foreground font-medium">
                  <span>{t('accounts.reloginQueueWaitingCount', {count: queue.length})}</span>
                  <span className="text-[11px] opacity-75">{t('accounts.reloginQueueListHint')}</span>
                </div>

                <div className="rounded-xl border border-border/50 divide-y divide-border/40 overflow-hidden bg-background/50">
                  {queue.map((a, i) => {
                    const isSelected = i === selectedIndex;
                    return (
                      <div
                        key={a.file}
                        onClick={() => setSelectedIndex(i)}
                        className={
                          'flex items-center justify-between gap-3 px-3.5 py-2.5 transition-colors cursor-pointer ' +
                          (isSelected
                            ? 'bg-primary/10 border-l-2 border-primary'
                            : 'hover:bg-muted/60')
                        }
                      >
                        <div className="flex items-center gap-3 min-w-0">
                          <span
                            className={
                              'grid h-5 w-5 shrink-0 place-items-center rounded-full text-[10px] font-bold ' +
                              (isSelected
                                ? 'bg-primary text-primary-foreground'
                                : 'bg-muted text-muted-foreground')
                            }
                          >
                            {i + 1}
                          </span>
                          <div className="min-w-0">
                            <div className="text-xs font-medium truncate">
                              {a.nickname || t('accounts.unnamed')}
                            </div>
                            <div className="font-mono text-[10px] text-muted-foreground truncate">
                              {a.uid}
                            </div>
                          </div>
                        </div>

                        <div className="flex items-center gap-2 shrink-0">
                          {statusBadge(a)}
                          <Button
                            size="sm"
                            variant={isSelected ? 'default' : 'outline'}
                            className="h-7 px-2.5 text-xs rounded-full gap-1"
                            onClick={(e) => {
                              e.stopPropagation();
                              setSelectedIndex(i);
                              setReloginOpen(true);
                            }}
                          >
                            <RefreshCw className="h-3 w-3" />
                            <span>{t('accounts.reloginQueueRelogin')}</span>
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              <div className="flex items-center gap-2 px-1 pt-1 text-[11px] text-muted-foreground">
                <TriangleAlert className="h-3.5 w-3.5 shrink-0 text-amber-500" />
                <span>{t('accounts.reloginQueueAdvanceHint')}</span>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* 重登必须**带着目标账号**进弹窗：版本跟它走、扫到别的号不落盘、
          落盘就地写它已有的文件。以前这里只打开一个通用的「添加账户」，
          于是「重登」与「添加账户」在服务端是同一件事 —— 扫了别的号就等于又加了
          一个账号，队列里那一行自然一直清不掉。 */}
      <AddAccountDialog
        open={reloginOpen}
        onOpenChange={setReloginOpen}
        onSuccess={handleReloginSuccess}
        upstreamId={upstreamId}
        target={current
          ? {
              uid: current.uid,
              nickname: current.nickname,
              realm: current.realm ?? 'cn',
              file: current.file,
              panel_disabled: current.disabled_by_panel === true,
            }
          : null}
      />
    </>
  );
}

export const ReloginQueue = ReloginQueueDialog;

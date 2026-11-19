'use client';

import {RefreshCw, TriangleAlert} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {useT} from '@/lib/i18n/provider';

/**
 * 取数失败的提示。两种形态共用一个组件，因为说的是同一件事
 * ——「这份数据没拿到」——只是范围不同：
 *
 *  · variant="page"：首屏一个字段都没成功。此时整页没有任何可信数据，
 *    所以占满内容区，并给一个「重试」让用户能自己再试一次。
 *  · variant="inline"：已有数据，只是这次刷新有字段失败。用一条窄提示挂在
 *    内容上方，**不能**把已经显示出来的内容顶掉——那些数据仍然是对的，
 *    只是可能不是最新的。
 *
 * 为什么不再用 toast：toast 几秒后自己消失，用户切回来时看不到「有东西没刷新
 * 成功」，而页面上的数字看起来一切正常（这正是原来仪表盘的问题）。
 * 改成常驻提示，直到下一次刷新成功。
 */
export function LoadError({
  variant = 'inline',
  message,
  hint,
  onRetry,
}: {
  variant?: 'page' | 'inline';
  /** 失败说明。不给则用通用文案 */
  message?: string;
  /** 补充说明，仅 page 形态显示 */
  hint?: string;
  onRetry?: () => void;
}) {
  const t = useT();
  const text = message ?? t('state.loadFailed');

  if (variant === 'inline') {
    return (
      <div
        role="status"
        className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-[16px] border border-amber-500/30 bg-amber-500/[0.07] px-3.5 py-2 text-[11px]"
      >
        <TriangleAlert className="h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
        <span className="flex-1 leading-5">{text}</span>
        {onRetry && (
          <Button
            variant="ghost"
            size="sm"
            className="h-6 rounded-full px-2 text-[11px] text-amber-700 hover:text-amber-800 dark:text-amber-300 dark:hover:text-amber-200"
            onClick={onRetry}
          >
            <RefreshCw className="h-3 w-3" />
            {t('state.retry')}
          </Button>
        )}
      </div>
    );
  }

  return (
    <div
      role="alert"
      className="flex flex-col items-center justify-center gap-3 rounded-[20px] bg-muted px-6 py-16 text-center"
    >
      <div className="grid h-12 w-12 place-items-center rounded-full bg-amber-500/15">
        <TriangleAlert className="h-5 w-5 text-amber-600 dark:text-amber-400" />
      </div>
      <div className="text-sm font-medium">{text}</div>
      <p className="max-w-md text-xs leading-5 text-muted-foreground">
        {hint ?? t('state.loadFailedHint')}
      </p>
      {onRetry && (
        <Button variant="outline" size="sm" className="rounded-full" onClick={onRetry}>
          <RefreshCw />
          {t('state.retry')}
        </Button>
      )}
    </div>
  );
}

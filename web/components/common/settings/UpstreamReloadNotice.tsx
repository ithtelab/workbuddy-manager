'use client';

import {useEffect, useRef, useState} from 'react';
import {CircleCheck, Loader2, TriangleAlert, X} from 'lucide-react';

import {Button} from '@/components/ui/button';
import {settingsApi} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {
  reloadPhase,
  reloadPollDelay,
  type ReloadPhase,
  type ReloadStateLike,
} from '@/lib/reload-state';

/**
 * 上游重载状态（批次 5）。
 *
 * **它解决什么**：保存上游配置后界面只说一句「正在自动应用到上游…」，之后就
 * 没有下文——重载成没成、失败原因是什么，用户一概不知道。后端一直在记账，
 * `GET /api/upstream/reload-state` 也一直在那儿，只是没人调用。这里把它接上，
 * 让「正在应用配置」从一句承诺变成可核对的状态。
 *
 * **为什么是常驻提示而不是 toast**：与 `LoadError` 同一个理由——toast 几秒后
 * 自己消失。而「上游重载失败」恰恰是**需要人去宿主机处理**的事，它必须在用户
 * 切回这个页面时还在。反过来，「配置已生效」只是一次确认，所以它不跨会话：
 * 刷新页面后就不该再显示（判定见 `@/lib/reload-state`）。
 *
 * **为什么挂在设置页外壳上**：重载失败影响的是**整个面板**（账号列表、签到、
 * 密钥都会跟着不正常），不只是「上游配置」这一个 Tab。挂在不受子路由重挂载
 * 影响的外壳上，切 Tab 不会把提示弄丢，也不会每切一次就重新问一遍。
 */
export function UpstreamReloadNotice() {
  const t = useT();
  const [state, setState] = useState<ReloadStateLike | null>(null);
  const [dismissed, setDismissed] = useState(false);
  /** 进入页面后第一次读到的 `last_at`。用来判断「期间有没有真的重载过」 */
  const baseline = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      // 标签页在后台时不问：重载是分钟级的事，回到前台再对齐就够，
      // 没必要让一个看不见的页面持续发请求（与 use-heartbeat 同一取舍）。
      if (document.hidden) {
        timer = setTimeout(tick, reloadPollDelay('idle'));
        return;
      }
      let next: ReloadStateLike | null = null;
      try {
        next = await settingsApi.reloadState();
      } catch {
        // 读不到就什么都不说。这里**不能**回落到「一切正常」——那等于替上游
        // 打包票；也不能报错，因为这只是个状态提示，不是用户发起的操作。
        next = null;
      }
      if (!alive) return;
      if (next) {
        if (baseline.current === null) baseline.current = next.last_at;
        setState(next);
      }
      const phase = next ? reloadPhase(next, baseline.current ?? next.last_at) : 'idle';
      timer = setTimeout(tick, reloadPollDelay(phase));
    };

    tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, []);

  const phase: ReloadPhase = state
    ? reloadPhase(state, baseline.current ?? state.last_at)
    : 'idle';
  if (phase === 'idle') return null;
  if (phase === 'failed' && dismissed) return null;

  if (phase === 'applying') {
    return (
      <div
        data-slot="reload-notice"
        data-phase="applying"
        role="status"
        className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-[16px] border border-sky-500/30 bg-sky-500/[0.07] px-3.5 py-2 text-[11px]"
      >
        <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-sky-600 dark:text-sky-400" />
        <span className="font-medium">{t('settings.applyRunning')}</span>
        <span className="text-muted-foreground">{t('settings.applyRunningHint')}</span>
      </div>
    );
  }

  if (phase === 'ok') {
    return (
      <div
        data-slot="reload-notice"
        data-phase="ok"
        role="status"
        className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-[16px] border border-emerald-500/30 bg-emerald-500/[0.07] px-3.5 py-2 text-[11px]"
      >
        <CircleCheck className="h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
        <span className="font-medium">{t('settings.applyOk')}</span>
      </div>
    );
  }

  // 失败：把上游（docker / 脚本）的原样输出带出来 —— 那是唯一能定位问题的东西。
  // 措辞上不说「保存失败」：配置**已经写进去了**，失败的是让它生效的那一步，
  // 两者的处理方式完全不同。
  return (
    <div
      data-slot="reload-notice"
      data-phase="failed"
      role="alert"
      className="flex items-start gap-2.5 rounded-[16px] border border-amber-500/30 bg-amber-500/[0.07] px-3.5 py-2 text-[11px]"
    >
      <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
      <div className="flex-1 space-y-1">
        <div className="font-medium">{t('settings.applyFailed')}</div>
        {state?.last_message ? (
          <div className="break-all text-muted-foreground">{state.last_message}</div>
        ) : null}
        <div className="text-muted-foreground">{t('settings.applyFailedHint')}</div>
      </div>
      <Button
        variant="ghost"
        size="sm"
        className="h-6 shrink-0 rounded-full px-2 text-[11px] text-amber-700 hover:text-amber-800 dark:text-amber-300 dark:hover:text-amber-200"
        aria-label={t('settings.applyDismiss')}
        onClick={() => setDismissed(true)}
      >
        <X className="h-3 w-3" />
      </Button>
    </div>
  );
}

'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {accountApi, errText} from '@/lib/api';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {TaskRunStatus} from '@/lib/types';

export type TaskRunMode = 'preview' | 'claim' | 'full';

/**
 * 活动任务的状态轮询与动作（任务页的全局面板、账号页的单号弹窗共用）。
 *
 * 两处以前各写一份同样的 load / start / stop / 定时器，注释里还互相
 * 「与 XX 同一策略」—— 改一处漏一处时，两个入口的行为就会分叉。抽出这一
 * 份之后：运行中 2 秒轮询、空闲 15 秒、`onFinished` 只在「运行 → 停止」
 * 那一刻触发一次。
 *
 * @param target  `ALL`（全部账号）或单个账号 uid
 * @param upstreamId 分组 id（issue #274）；null = 默认分组
 * @param enabled 弹窗关闭时传 false，停止轮询；重新打开会从当前状态继续
 * @param onFinished 一次执行结束时通知调用方（例如刷新账号列表）
 */
export function useTaskRun({
  target,
  upstreamId = null,
  enabled = true,
  onFinished,
}: {
  target: string;
  upstreamId?: number | null;
  enabled?: boolean;
  onFinished?: () => void;
}) {
  const {t, tp} = useI18n();
  const [status, setStatus] = useState<TaskRunStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wasRunning = useRef(false);
  // 回调放进 ref：调用方每次渲染都可能传一个新函数，若放进 effect 依赖，
  // 轮询会被反复推倒重来（多发请求、丢失「刚刚跑完」的边沿）。
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  const load = useCallback(async () => {
    try {
      const s = await accountApi.taskRunStatus();
      setStatus(s);
      return s;
    } catch {
      return null; // 拉状态失败不打扰用户（按钮自然处于不可点状态）
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    wasRunning.current = false;
    const tick = async () => {
      const s = await load();
      if (!alive) return;
      const nowRunning = s?.running === true;
      if (wasRunning.current && !nowRunning) finishedRef.current?.();
      wasRunning.current = nowRunning;
      // 跑的时候高频看输出；空闲时低频，避免无意义的轮询。
      timer.current = setTimeout(tick, nowRunning ? 2000 : 15000);
    };
    tick();
    return () => {
      alive = false;
      if (timer.current) clearTimeout(timer.current);
      timer.current = null;
    };
  }, [enabled, load]);

  const run = useCallback(async (mode: TaskRunMode, confirm = false) => {
    setBusy(true);
    try {
      await accountApi.taskRunStart(mode, target, confirm, upstreamId);
      // 描述用界面的模式名（后端回的是「已开始执行（preview）」这类中文原文）
      notify.ok(t('tasks.runStarted'), t(`tasks.runMode_${mode}`));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }, [load, t, target, upstreamId]);

  const stop = useCallback(async () => {
    setBusy(true);
    try {
      const r = await accountApi.taskRunStop();
      // 后端只回两种固定文案（已停止 / 当前没有正在执行的任务），走短语表
      (r.ok ? notify.ok : notify.info)(tp(r.message));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }, [load, tp]);

  return {status, busy, run, stop};
}

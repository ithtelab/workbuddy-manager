'use client';

import {useCallback, useEffect, useRef, useState} from 'react';

/**
 * 防抖：`value` 停止变化 `delay` 毫秒之后，才把新值对外给出去。
 *
 * 为什么需要它：日志页的筛选从「点查询才生效」改成「改完即生效」之后，文本类
 * 输入框（模型 / 来源 IP）会在**每次按键**上触发一次请求——输入 `kimi` 会连发
 * 4 次（`k` / `ki` / `kim` / `kimi`），其中前三次的结果纯属浪费。防抖把这一串
 * 压成 1 次。
 *
 * 返回值是 `[落定值, 立即落定]`：
 *  · **落定值**给取数与依赖数组用（不要用输入值）；
 *  · `applyNow` 给「一键重置筛选」这类**明确要求立刻生效**的入口用。不这样做的话，
 *    重置只改了输入框，还要等防抖计时器走完才真正生效，中间会先按「旧文本筛选 +
 *    新下拉筛选」查一次——列表闪两下，而且那一份结果谁都不想看。
 *
 * `onSettle` 在落定的**同一次更新里**执行（React 会把 `setTimeout` 回调里的多个
 * setState 合批）。调用方需要「落定时顺带把页码复位」时**必须**用它，不要自己写
 * `useEffect(() => setPage(1), [落定值])`：后者会先提交一帧「新筛选 + 旧页码」，
 * 于是先按旧页码查一次、再回第 1 页查第二次，白打一个请求。
 */
export function useDebounced<T>(
  value: T,
  delay = 400,
  onSettle?: () => void,
): [T, (v: T) => void] {
  const [settled, setSettled] = useState(value);

  /**
   * 回调每次渲染都是新函数，不能进 effect 依赖（那会让计时器每帧重排，
   * 于是**永远不会落定**）。用 ref 取最新的一份，同 lib/use-heartbeat.ts。
   */
  const settle = useRef(onSettle);
  settle.current = onSettle;

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    timer.current = setTimeout(() => {
      timer.current = null;
      // 顺序有意如此：先做「落定时的附带动作」（如复位页码），再落定值。
      // 两者在同一次回调里，React 合批成一次渲染，取数只会跑一次。
      settle.current?.();
      setSettled(value);
    }, delay);
    return () => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = null;
    };
  }, [value, delay]);

  const applyNow = useCallback((v: T) => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    settle.current?.();
    setSettled(v);
  }, []);

  return [settled, applyNow];
}

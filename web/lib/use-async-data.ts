'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {asyncFlags, depMode, splitSettled, type DepPrints, type FetchMode} from '@/lib/async-state';

/** 字段名 → 取数函数 */
export type AsyncFetchers<T> = {[K in keyof T]: () => Promise<T[K]>};

export interface AsyncAllState<T> {
  /**
   * 已成功取到的字段。
   *
   * 刷新时某个字段失败，它**保留上一次的值**——那份数据仍然是对的，只是可能
   * 不是最新的。清成空会让整块内容凭空消失，比显示旧值更糟。
   */
  values: Partial<T>;
  /** 失败的字段 → 原因。成功的字段不会出现在这里，可用来数「有几项没刷上」 */
  errors: Partial<Record<keyof T, unknown>>;
  /** 首屏未就绪（还没拿到任何数据）→ 该显示骨架 */
  isInitialLoading: boolean;
  /** 首屏彻底失败（一个字段都没成功）→ 该显示错误态与「重试」 */
  isInitialFailed: boolean;
  /** 已有数据、后台正在刷新 → 内容保持不变，仅用于 aria-busy 这类轻提示 */
  isRefreshing: boolean;
  /**
   * 重新拉取一次（保留现有数据）。
   *
   * **返回本次刷新是否全部成功**，并在这次请求落地后才 resolve。需要它的场景是
   * 「先做一个写操作、再刷新列表」——密钥页建完密钥后要能区分「创建失败」与
   * 「建好了但列表没刷上」，后者绝不能报成前者（用户会以为没建成功、再点一次，
   * 于是建出重复密钥）。
   *
   * 不需要这个信息的调用方照旧忽略返回值即可（`onRetry={reload}`、
   * `useHeartbeat(reload, …)` 都不受影响）。
   */
  reload: () => Promise<boolean>;
}

/**
 * 并发取多个字段，并把「首屏」与「后台刷新」两种状态区分开。
 *
 * 为什么需要它：这几页都是「进页面就并发拉几份数据 + 心跳定时重拉」，而原先
 * 每页各写一遍 `Promise.allSettled` + 若干个 useState，于是各自漏掉同一批东西
 * ——没有加载态（首屏把「还没取到」显示成「暂无数据」）、没有错误态（失败只弹
 * 一个几秒后消失的 toast）、切换版本时旧数据仍留在屏幕上（标题已经改成国际版，
 * 数字还是国内版的）。把这段逻辑收成一个 hook，各页只需声明「我要哪几份数据」。
 *
 * 状态怎么推导、为什么这么推导，都在 lib/async-state.ts 里（纯函数，可单独测）。
 *
 * @param fetchers 字段名 → 取数函数。可以内联写箭头函数，不必用 useMemo 包。
 * @param deps     数据上下文（例如 [realm]）。**变了就重取，且先清空旧数据**：
 *                 旧数据属于旧上下文，留着会显示成新上下文的数字。
 * @param refreshDeps 同一上下文里的查询范围（例如翻页用的 [page]）。**变了只重取，
 *                 不清空**——现有内容留在屏幕上直到新数据到达。翻页是高频操作，
 *                 清空会让每翻一页闪一次骨架。
 */
export function useAsyncAll<T extends Record<string, unknown>>(
  fetchers: AsyncFetchers<T>,
  deps: readonly unknown[] = [],
  refreshDeps: readonly unknown[] = [],
): AsyncAllState<T> {
  const [values, setValues] = useState<Partial<T>>({});
  const [errors, setErrors] = useState<Partial<Record<keyof T, unknown>>>({});
  const [pending, setPending] = useState(true);

  /**
   * 请求序号：只让**最新一次**发起的请求写状态。
   *
   * 两种会撞车的情形：
   *  · 切换版本（deps 变化）——旧版本的请求可能后到，把新版本的数据覆盖掉，
   *    界面上就出现「标题写着国际版、数字还是国内版」；
   *  · 心跳与手动重试交叠——先发的慢、后发的快，先发的那份回来时反而成了最新。
   * 每次发起自增，回来时对不上就整包丢弃。
   */
  const seq = useRef(0);

  /**
   * fetchers 每次渲染都是新对象（调用方写的是内联箭头函数），不能进依赖数组；
   * 用 ref 取最新的一份，`run` 才能保持稳定。同 lib/use-heartbeat.ts 的做法。
   */
  const ref = useRef(fetchers);
  ref.current = fetchers;

  const run = useCallback((mode: FetchMode): Promise<boolean> => {
    const mine = ++seq.current;
    if (mode === 'initial') {
      // deps 变了 = 换了一个数据上下文。旧数据**必须**清掉：留着比空着更糟
      // ——界面各处（标题、卡片提示）已经按新上下文说话了，数字却还是旧的。
      setValues({});
      setErrors({});
    }
    setPending(true);

    const keys = Object.keys(ref.current) as (keyof T)[];
    // 逐项独立成败：一份数据挂了不该拖垮其余几份（原写法用的就是 allSettled）。
    return Promise.allSettled(keys.map((k) => ref.current[k]())).then((settled) => {
      // 过期响应：整包丢弃。这里**返回 true 而不是 false**——这一次的成败已经
      // 不作数了（更新的那次请求才是权威），而返回 false 会让调用方凭空报一次
      // 失败（「建好了但刷新失败」这种提示会误弹）。宁可不说，也不要误报。
      if (mine !== seq.current) return true;

      const fresh = splitSettled(keys as string[], settled);
      // 刷新时**合并**而不是替换：这次失败的字段保留上一次的值。整体替换的话，
      // 心跳里任何一个请求超时都会把已经显示出来的内容清空。
      setValues((prev) => (
        mode === 'initial' ? fresh.values : {...prev, ...fresh.values}
      ) as Partial<T>);
      // errors 整体替换：这次成功了的字段，上一次的失败记录要一并清掉，
      // 否则「部分数据加载失败」的提示会永远挂着。
      setErrors(fresh.errors as Partial<Record<keyof T, unknown>>);
      setPending(false);
      // 只要还有字段没取到就算「没全部成功」。调用方据此决定要不要提示用户。
      return Object.keys(fresh.errors).length === 0;
    });
  }, []);

  // deps 是调用方写的数组字面量，每帧都是新引用，直接当依赖会让首屏请求每帧重发；
  // 序列化成字符串才能比出「真的变了」。（依赖写成 depsKey 而不是 deps：后者每帧
  // 都是新数组，effect 会每帧重跑，首屏请求也就每帧重发一次。）
  const depsKey = JSON.stringify(deps);
  const refreshKey = JSON.stringify(refreshDeps);

  /**
   * 上一次的依赖指纹。`context` 初值取 null，于是首屏一定算出 'initial'——
   * 不需要在挂载时额外补一次请求；`query` 初值取当前值，挂载时不该因为
   * 「查询范围从无到有」而多打一次。
   *
   * 两种依赖的区别与「同时变化时听谁的」，都在 lib/async-state.ts 的 depMode 里
   * （纯函数，可单独测）。
   */
  const prints = useRef<DepPrints>({context: null, query: refreshKey});

  useEffect(() => {
    const next: DepPrints = {context: depsKey, query: refreshKey};
    const mode = depMode(prints.current, next);
    prints.current = next;
    // 首屏这次不需要知道成败（成败已由 isInitialLoading / isInitialFailed 表达），
    // 显式 void 掉，表明不是漏写了 await。
    if (mode) void run(mode);
  }, [depsKey, refreshKey, run]);

  const reload = useCallback(() => run('refresh'), [run]);

  return {
    values,
    errors,
    ...asyncFlags(values, errors, pending),
    reload,
  };
}

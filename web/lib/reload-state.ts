/**
 * 上游重载状态的判定（批次 5）—— 纯函数，零运行时依赖。
 *
 * ## 它解决什么
 *
 * 保存上游配置后，界面一直说「正在自动应用到上游…」，然后就没有下文了。
 * 后端其实**一直**在记账（`server/services/reload.py` 的 `_state`：是否在跑、
 * 上一次成没成、什么时候、原样报错），也有现成的只读端点
 * （`GET /api/upstream/reload-state`），`web/lib/api.ts` 里连封装都写好了
 * ——**只是从来没有人调用它**。
 *
 * 于是「正在应用配置」是一句**没有兑现的承诺**：重载失败（没装 docker、容器
 * 名写错、脚本挂住）时界面照样一片祥和，用户以为改完就生效了。这一批把那个
 * 端点接上，让这句话变成可核对的状态。
 *
 * ## 为什么判据要带一个 `baseline`
 *
 * `last_ok` 记的是**上一次**重载的结果，它会一直留在那里。只看 `last_ok`
 * 的话，场景是这样的：
 *
 *   1. 上一次重载失败 → `last_ok === false`；
 *   2. 用户修好了配置、保存 → 这一次的重载**还没开始跑**；
 *   3. 界面立刻报「上游重载失败」——而这一次根本还没跑完。
 *
 * 反过来也一样：上一次成功，这次正在跑，界面就先说「已生效」。
 * 两种都是**把上一次的结论安到这一次头上**。
 *
 * 所以判定要区分「这一次有没有落地」：`last_at` 只在一次重载真正结束时更新，
 * 拿它跟**进入页面时观察到的值**比，就能知道期间有没有发生过一次重载。
 * 这也顺便避开了客户端与服务端**时钟不一致**的问题（不比较绝对时间）。
 *
 * ## 为什么 `failed` 不看 baseline，`ok` 看
 *
 * 不对称是**有意**的：
 *
 *   · 失败是**要人处理**的状态（得去宿主机重启容器）。它不该因为「发生在我
 *     打开页面之前」就消失——那正好会让人错过它。
 *   · 成功只是**一次确认**（「你刚才那次保存生效了」）。如果也一直挂着，
 *     页面就会永远显示「配置已生效」，变成新的噪音。
 */

/** `GET /api/upstream/reload-state` 的响应形状（与 `types.ts` 的 `ReloadState` 一致）。 */
export type ReloadStateLike = {
  /** 正在执行重启 */
  running: boolean;
  /** 有重启在排队（把紧挨着的多次改动合并成一次） */
  pending: boolean;
  /** 上次重启**完成**时间（秒）。只有一次重载真的跑完才会前进 */
  last_at: number;
  /** 上次重启的结果。**从没重载过**时是 `null` */
  last_ok: boolean | null;
  /** 上游（docker / 脚本）的原样输出，失败时就是失败原因 */
  last_message: string;
  restart_count: number;
};

export const RELOAD_PHASES = ['applying', 'ok', 'failed', 'idle'] as const;

export type ReloadPhase = (typeof RELOAD_PHASES)[number];

/** 应用中的轮询间隔：这是一件**正在发生**的事，要跟紧（容器重启通常几秒到几十秒） */
export const RELOAD_POLL_APPLYING_MS = 1500;

/** 闲时的轮询间隔：只为「上一次失败」这种常驻状态，慢一点省请求 */
export const RELOAD_POLL_IDLE_MS = 15000;

/**
 * 当前该显示哪一档。
 *
 * @param state    最新一次读到的状态
 * @param baseline 进入页面后**第一次**读到的 `last_at`（见文件顶部的说明）
 */
export function reloadPhase(state: ReloadStateLike, baseline: number): ReloadPhase {
  // 正在跑 / 已排队 → 不管上一次成没成都先如实说「正在应用」：
  // 上一次的结论属于上一次，不能拿来盖住这一次。
  if (state.running || state.pending) return 'applying';
  // 失败常驻（要人去处理，见文件顶部的不对称说明）
  if (state.last_ok === false) return 'failed';
  // 成功只报「期间观察到过」的那一次
  if (state.last_ok === true && state.last_at > baseline) return 'ok';
  return 'idle';
}

/** 下一轮轮询等多久。只有 `applying` 需要跟紧。 */
export function reloadPollDelay(phase: ReloadPhase): number {
  return phase === 'applying' ? RELOAD_POLL_APPLYING_MS : RELOAD_POLL_IDLE_MS;
}

/** 这个相位要不要在界面上占位（`idle` 什么都不渲染）。 */
export function reloadPhaseVisible(phase: ReloadPhase): boolean {
  return phase !== 'idle';
}

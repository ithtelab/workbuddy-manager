'use client';

import {useT} from '@/lib/i18n/provider';
import {fmtDateTime, fmtNumber} from '@/lib/format';
import {useNow} from '@/lib/use-now';
import type {CreditExpiry} from '@/lib/types';
import {groupExpiriesByDay} from '@/lib/display-prefs';

/**
 * 积分到期倒计时。
 *
 * 为什么要做：积分按套餐分批过期（CycleEndTime），过期即作废。面板此前只显示
 * 余额合计，用户看不到「这笔钱什么时候没」，于是常有积分白白过期。上游选号时
 * 会把快过期的号排前面去优先消耗，但那解决不了「总量用不完」的情况——最终还是
 * 得让用户看得见到期时间。
 *
 * 只显示**最近一个**到期套餐的倒计时（列表由后端按到期时间升序给出），全部套餐
 * 明细放在悬停提示里：单元格里塞不下多行，而用户最需要知道的正是「最近的那笔
 * 还有多久」。
 *
 * 角标里**必须带上那笔的额度**，不能只写时间。积分是按套餐分批过期的，总额旁边
 * 只挂一个「8 天后到期」会被读成「这些积分全都 8 天后到期」——而更常见的情况是
 * 只有其中一小笔到期、大头还在后面。带上额度才读得出「总额里有多少马上要没」，
 * 悬停里也有逐笔明细与合计可对账。
 *
 * 颜色按紧迫度分档，让快到期的自己跳出来，而不是等用户逐个去算。
 */
export function CreditCountdown({expiries}: {expiries?: CreditExpiry[] | null}) {
  // 没有到期信息（套餐永不过期 / 尚未查到）时不渲染任何东西 —— 包括不订阅时钟。
  // 拆成两层就是为了这个：useNow 是 hook，不能在提前 return 之后才调用。
  // 角标按**本地自然日**合并后再取第一天（issue #434）：同一天到期的多笔会一起
  // 作废，只显示最早那一笔会把「当天要没多少」低估（实测 90 + 100 + 100 只显示 90）。
  // 悬停明细仍逐包列出——那是对账用的，不能合并。
  const next = expiries ? groupExpiriesByDay(expiries)[0] : undefined;
  if (!next) return null;
  return <Countdown next={next} all={expiries ?? []} />;
}

function Countdown({next, all}: {next: CreditExpiry; all: CreditExpiry[]}) {
  const t = useT();
  // 用渲染时刻粗算一次剩余时间，只用来决定刷新频率（精度要求 = 文案变化频率）：
  // 最后一分钟必须秒级，否则「即将到期」出现得不及时；再往后分钟级足够，
  // 一秒一次纯属让几十上百行账号跟着空转。
  //
  // 阈值取 120 秒而非 60：这个判断只在每次刷新时重做，分钟级档最坏会晚 60 秒
  // 才发现自己该升级到秒级，留一倍余量才能保证进最后一分钟时已经是秒级在跑。
  const roughLeft = next.at - Math.floor(Date.now() / 1000);
  const now = useNow(roughLeft < 120 ? 1000 : 60_000);

  const left = next.at - Math.floor(now / 1000);
  // 三档紧迫度：1 天内红 → 7 天内琥珀 → 更远常规色。
  // 分档的意义在于「一眼看出哪笔该先花掉」；全列都染成暖色就等于没有优先级。
  const cls =
    left < 86400
      ? 'bg-red-500/15 text-red-600 dark:text-red-400'
      : left < 7 * 86400
        ? 'bg-amber-500/15 text-amber-600 dark:text-amber-400'
        : 'bg-muted text-muted-foreground';

  // 明细：每条「额度 · 到期时刻（本地时区）」。时刻用绝对时间，
  // 用户要拿它跟腾讯官网/客服对账，倒计时只解决紧迫感。
  const lines = all.map((e) => `${fmtNumber(e.amount)} · ${fmtDateTime(e.at)}`);
  const total = all.reduce((sum, e) => sum + e.amount, 0);

  return (
    <span
      className={`rounded-full px-1.5 py-0.5 text-[10px] leading-3 tabular-nums ${cls}`}
      title={[
        t('credit.expiryTipTitle', {total: fmtNumber(total)}),
        ...lines,
        '',
        t('credit.expiryTipNote'),
      ].join('\n')}
    >
      {/* 额度在前、时间在后：读作「这么多，还有这么久到期」。反过来只写时间的话
          会被当成整列总额的到期时间（见文件头注释）。 */}
      {fmtNumber(next.amount)} · {countdownText(t, left)}
    </span>
  );
}

/**
 * 倒计时文案。**向下取整**：倒计时说「还有 5.8 小时」不如「还有 5 小时」干脆，
 * 精确时刻由悬停提示里的日期承担。取整方向也是刻意的——宁可让用户以为时间更少，
 * 也不能多报，否则用户按「还有 5 小时」安排任务，实际只剩 4 小时多就作废了。
 *
 * 文案自带「后过期」：单元格里只有一个数字的话看不出在数什么。
 */
function countdownText(t: ReturnType<typeof useT>, left: number): string {
  if (left <= 0) return t('credit.expired');
  if (left < 60) return t('credit.expiring');
  if (left < 3600) {
    const n = Math.floor(left / 60);
    return t('credit.expiresInMinutes', {count: n, n});
  }
  if (left < 86400) {
    const n = Math.floor(left / 3600);
    return t('credit.expiresInHours', {count: n, n});
  }
  const n = Math.floor(left / 86400);
  return t('credit.expiresInDays', {count: n, n});
}

/**
 * 仪表盘「即将到期积分」用的到期明细。
 *
 * 积分元数据按 uid 存，一次查询会带上所有分组、所有版本的账号。余额已经按
 * 当前版本的账号名单算过了，到期必须走**同一份名单**：否则切到国际版仍会
 * 显示国内版更早的那一笔，金额、时刻和紧急色一起串。刷新换的是同一份 map，
 * 不按名单收的话刷新也改不掉。
 *
 * 只收调用方已经按版本筛过的账号。realm 为空视为国内版的判定留在调用方，
 * 与账号计数共用，不在这里再写一遍。
 *
 * 同一 uid 出现在多个分组时只计一次——元数据本身就是按 uid 一份。
 * 不修改传入的账号列表、元数据，以及其中的到期数组。
 */
import type {CreditExpiry, CreditsMeta} from './types';

export function collectScopedExpiries(
  accounts: readonly {uid: string}[],
  meta: Record<string, CreditsMeta>,
): CreditExpiry[] {
  const seen = new Set<string>();
  const out: CreditExpiry[] = [];
  for (const account of accounts) {
    if (seen.has(account.uid)) continue;
    seen.add(account.uid);
    const expiries = meta[account.uid]?.expiries;
    if (!expiries) continue;
    for (const expiry of expiries) out.push(expiry);
  }
  return out.sort((a, b) => a.at - b.at);
}

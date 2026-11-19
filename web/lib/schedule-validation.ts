/** Validate the complete form, including unchanged bounds, before submitting. */
export function scheduleDelayError(values: Record<string, unknown>): string | undefined {
  for (const [key, maximum] of [
    ['random_delay_minutes', 60],
    ['account_delay_min_seconds', 600],
    ['account_delay_max_seconds', 600],
  ] as const) {
    const value = values[key] ?? 0;
    if (typeof value !== 'number' || !Number.isInteger(value) || value < 0 || value > maximum) {
      return '任务随机延迟须为 0～60 的整数，账号间隔须为 0～600 的整数';
    }
  }
  if (Number(values.account_delay_min_seconds ?? 0) > Number(values.account_delay_max_seconds ?? 0)) {
    return '账号最短间隔不能大于最长间隔';
  }
  return undefined;
}

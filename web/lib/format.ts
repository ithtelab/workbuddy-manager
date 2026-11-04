/**
 * 本地化格式化工具。
 *
 * 这些函数是纯函数（非组件），语言取自 lib/i18n 的模块级当前语言，
 * 由 I18nProvider 在渲染期同步——调用它们的组件会随语言切换重渲染。
 * 因此本文件里不要写死任何语言：日期、数字、时长、相对时间都跟随界面语言。
 */
import {intlLocale, t} from '@/lib/i18n';

/** 秒级时长格式化：已过期 / 分钟 / 小时 / 天 */
export function fmtRemain(seconds: number): string {
  if (seconds <= 0) return t('format.expired');
  if (seconds >= 365 * 86400 * 5) return t('format.permanent');
  if (seconds < 60) {
    // 不足一分钟别再向下取整成「0 分钟」：那会被读成「已到期」。
    return t('format.seconds', {count: seconds, n: seconds});
  }
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    return t('format.minutes', {count: minutes, n: minutes});
  }
  // count 传数值（决定英文单复数），n 传展示串（与原实现一致，保留一位小数）
  if (seconds < 86400) {
    const hrs = Math.round((seconds / 3600) * 10) / 10;
    return t('format.hours', {count: hrs, n: hrs.toFixed(1)});
  }
  const ds = Math.round((seconds / 86400) * 10) / 10;
  return t('format.days', {count: ds, n: ds.toFixed(1)});
}

export function fmtDateTime(ts: number | null | undefined): string {
  if (!ts) return '—';
  const ms = ts > 1e12 ? ts : ts * 1000;
  const d = new Date(ms);
  return d.toLocaleString(intlLocale(), {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function fmtDate(ts: number | null | undefined): string {
  if (!ts) return '—';
  const ms = ts > 1e12 ? ts : ts * 1000;
  return new Date(ms).toLocaleDateString(intlLocale());
}

/**
 * 日期时间 + 相对「今天」的标记：`2026/09/20 14:03:11`。
 *
 * 为什么需要（issue #44）：任务页与日志页都有「近 24 小时」这个范围，它**必然跨天**
 * ——这时候列表里今天的 14:03 与昨天的 14:03 长得一模一样，扫一眼分不出哪条是
 * 今天的，只能挨个去数字段里的日期。用户的原话是「翻看 24 小时的时候会翻到前一天
 * 的记录，不方便观察各个账号运行状态」。
 *
 * 所以对**非今天**的行把日期顶到最前面并标注「昨天 / 更早」，今天的行保持简短。
 * 不直接写死「今天」的原因：列表里绝大多数行都是今天的，逐行标一遍只是噪音；
 * 真正需要区分的是那几条**不是今天**的。
 *
 * 跨天（今天/昨天）按**本地日历日**判，不是「距今 24 小时内」——用户看的是日历，
 * 凌晨 1 点看 23 小时前的记录会认为那是「昨天」，按日历判才与直觉一致。
 */
export function fmtDateTimeMarked(ts: number | null | undefined): string {
  if (!ts) return '—';
  const ms = ts > 1e12 ? ts : ts * 1000;
  const d = new Date(ms);
  const full = fmtDateTime(ts);
  const dayDiff = calendarDaysAgo(d);
  if (dayDiff === 0) return full;
  if (dayDiff === 1) return `${t('format.yesterday')} ${full}`;
  return full;
}

/** 目标时间距「今天」的本地日历天数（今天=0，昨天=1，未来=负数）。 */
function calendarDaysAgo(d: Date): number {
  const now = new Date();
  // 用 Date.UTC 把两个本地日期归一到 UTC 零点再相减：这样得到的是**日历天**之差，
  // 不受夏令时（某些时区一天是 23/25 小时）与具体时刻影响。
  const a = Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
  const b = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((b - a) / 86400000);
}

/** 千分位数字 */
export function fmtNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return '0';
  return n.toLocaleString(intlLocale());
}

/** 大数紧凑显示：1.2k / 3.4M */
export function fmtCompact(n: number | null | undefined): string {
  if (!n) return '0';
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

export function fmtLatency(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/** Token 有效期的语义分档，账号列表与仪表盘共用同一套规则 */
export type ExpiryTier = 'expired' | 'urgent' | 'soon' | 'healthy';

export interface ExpiryVisual {
  tier: ExpiryTier;
  /** 文字颜色 class */
  textClass: string;
  /** 进度条颜色（CSS 色值） */
  barColor: string;
  /** 状态短标签 */
  label: string;
}

/**
 * 有效期进度条的兜底窗口（60 天）。
 *
 * 正常会用后端从 JWT 解出的真实总时长（ttlSeconds）；只有在解不出时
 * 才退回这个默认值（腾讯签发约 60 天）。
 *
 * 历史问题：这里曾写死 72 小时（3 天）作为满格，而 token 实际有效期约 60 天，
 * 于是「60 天」和「5 天」的进度条都是一整条，等于没有信息。
 */
export const EXPIRY_BAR_FALLBACK_SECONDS = 60 * 86400;

/**
 * 进度条宽度百分比（0–100）。
 * ttlSeconds 为该令牌签发的总时长；未知时用兜底窗口。
 */
export function expiryBarPercent(remainSeconds: number, ttlSeconds?: number | null): number {
  if (!Number.isFinite(remainSeconds) || remainSeconds <= 0) return 0;
  const total = ttlSeconds && ttlSeconds > 0 ? ttlSeconds : EXPIRY_BAR_FALLBACK_SECONDS;
  return Math.min(100, (remainSeconds / total) * 100);
}

/**
 * 按剩余有效期分档：
 * - expired 已过期      → 红（destructive）
 * - urgent  < 1 小时     → 琥珀
 * - soon    < 6 小时     → 蓝
 * - healthy 其余         → 绿
 */
export function expiryVisual(remainSeconds: number): ExpiryVisual {
  if (remainSeconds <= 0) {
    return {
      tier: 'expired',
      textClass: 'text-red-600 dark:text-red-400',
      barColor: 'var(--destructive)',
      label: t('expiry.expired'),
    };
  }
  if (remainSeconds < 3600) {
    return {
      tier: 'urgent',
      textClass: 'text-amber-600 dark:text-amber-400',
      barColor: '#f59e0b',
      label: t('expiry.urgent'),
    };
  }
  if (remainSeconds < 6 * 3600) {
    return {
      tier: 'soon',
      textClass: 'text-blue-600 dark:text-blue-400',
      barColor: '#3b82f6',
      label: t('expiry.soon'),
    };
  }
  return {
    tier: 'healthy',
    textClass: 'text-emerald-600 dark:text-emerald-400',
    barColor: '#10b981',
    // 文案是「有效」而不是「在线」：这一档只说明**令牌在有效期内**，不说明账号
    // 可用（它可能没进上游池、被禁用或一直在失败）。此前这里写「在线」，被首页
    // 当成账号状态直接渲染，于是出现「首页说在线、账号页说未加载」的矛盾。
    // 账号可用性一律走 `lib/account-status` 的 `availabilityOf()`。
    label: t('expiry.valid'),
  };
}

/** 相对时间：3 分钟前 */
export function fmtAgo(ts: number | null | undefined): string {
  if (!ts) return t('format.never');
  const ms = ts > 1e12 ? ts : ts * 1000;
  const diff = Date.now() - ms;
  if (diff < 0) return t('format.justNow');
  const s = Math.floor(diff / 1000);
  if (s < 60) return t('format.secondsAgo', {count: s, n: s});
  if (s < 3600) {
    const minutes = Math.floor(s / 60);
    return t('format.minutesAgo', {count: minutes, n: minutes});
  }
  if (s < 86400) {
    const hrs = Math.floor(s / 3600);
    return t('format.hoursAgo', {count: hrs, n: hrs});
  }
  const ds = Math.floor(s / 86400);
  return t('format.daysAgo', {count: ds, n: ds});
}

/**
 * 复制文本到剪贴板，返回**是否真的复制成功**。
 *
 * 为什么必须检查回退路径的返回值（issue #57）：`document.execCommand('copy')`
 * 是**有布尔返回值**的 —— 被浏览器拒绝时返回 `false`（非安全上下文、权限被拒、
 * 不在用户手势之内都会这样）。原先忽略它、直接 `return true`，于是「根本没复制
 * 成功」也会提示「已复制到剪贴板」，用户对着空剪贴板反复点，只能来报「提示成功
 * 但复制不上」。
 *
 * 两条路径的判据都落在「真的写进去了」：
 *   1. 优先 Clipboard API —— 它只在**安全上下文**（HTTPS 或 localhost）可用，
 *      普通 HTTP 访问时 `navigator.clipboard` 是 undefined；
 *   2. 回退到 `execCommand('copy')`，并**检查它的布尔返回值**。
 * 都失败返回 `false`，由调用方如实提示（文案引导手动选择复制）。
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* 落到下面的回退路径 */
    }
  }

  const ta = document.createElement('textarea');
  ta.value = text;
  // 只读：避免移动端弹键盘；移出可视区且透明：不闪、不占位
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.top = '0';
  ta.style.left = '-9999px';
  ta.style.opacity = '0';
  document.body.appendChild(ta);

  const sel = document.getSelection();
  const saved = sel && sel.rangeCount > 0 ? sel.getRangeAt(0) : null;
  let ok = false;
  try {
    ta.select();
    ta.setSelectionRange(0, text.length);
    ok = document.execCommand('copy') === true;
  } catch {
    ok = false;
  } finally {
    document.body.removeChild(ta);
    // 恢复用户原来的选区：复制是旁路动作，不该把界面上已有的选择弄丢
    if (saved && sel) {
      sel.removeAllRanges();
      sel.addRange(saved);
    }
  }
  return ok;
}

/**
 * 扣费金额格式化（上游 usage.credit）。
 * 单次调用常是 0.0x 量级，直接 toLocaleString 会显示成 0，因此小数值保留
 * 最多 4 位有效小数；整数则按千分位显示。
 */
/**
 * 金额（元）。用于「按官方价估算」的费用，issue #380。
 *
 * 为什么不用 fmtCredit：那是**积分**，整数为主、大额四舍五入到整数；费用是
 * 元、常见 0.0x 量级，抹掉小数就等于显示成 0。这里按量级给位数：
 * ≥100 两位、≥1 两位、<1 四位（0.0034 这种才是真实形态）。
 * null / undefined 一律「—」——「算不出」不等于「0 元」，不能显示 ¥0。
 */
export function fmtMoney(v: number | null | undefined, currency = 'CNY'): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const sign = currency === 'CNY' ? '¥' : '';
  if (v === 0) return `${sign}0`;
  const abs = Math.abs(v);
  if (abs >= 1) return `${sign}${v.toFixed(2)}`;
  if (abs >= 0.0001) return `${sign}${v.toFixed(4)}`;
  return `${sign}${v.toExponential(1)}`;
}

export function fmtCredit(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  if (v === 0) return '0';
  if (Math.abs(v) >= 100) return Math.round(v).toLocaleString(intlLocale());
  if (Math.abs(v) >= 1) return v.toFixed(2);
  return v.toFixed(4);
}

/**
 * 抓包页（/traces）的纯函数工具：格式化、摘要拼装、差异计数。
 *
 * 为什么单独一个模块：这些逻辑都**不依赖 React**，单独放才能被 `.test.mjs`
 * 直接 import（仓库里的纯函数惯例，见 `lib/section-nav.ts` 的说明）。
 * 页面上剩下的只有状态与渲染。
 */
import type {TapDiff, TapSession, TapTurnSummary} from './types';

/** 字节数 → 人类可读。抓包页要回答「这一轮多大」，用 B/KB 比裸数字直观。 */
export function fmtBytes(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  if (!Number.isFinite(v) || v <= 0) return '0 B';
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / 1024 / 1024).toFixed(2)} MB`;
}

/** 毫秒 → `1.2s` / `340ms`。 */
export function fmtMs(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  const v = Number(n);
  if (!Number.isFinite(v) || v < 0) return '—';
  if (v < 1000) return `${Math.round(v)}ms`;
  return `${(v / 1000).toFixed(1)}s`;
}

/**
 * 会话标题：模型 + 首轮概要。
 *
 * 抓包列表里没有「用户消息」可显示（那是详情页的事），标题只能靠模型与轮次
 * 让人认出「这是哪一次任务」。指纹前 6 位给一个稳定的短标识，便于在 issue 里
 * 引用具体某条会话。
 */
export function sessionTitle(s: TapSession): string {
  const model = s.model || '—';
  return `${model} · ${s.turns} 轮 · ${s.fingerprint.slice(0, 6)}`;
}

/**
 * 从 UA 取版本号（`name/1.2.3 (+url)` → `1.2.3`）。
 *
 * 为什么显示版本：同一个客户端的协议字段/工具形态会随版本变，只写名字在排障时
 * 不够用（「同一个 Claude Code，2.0 和 1.0 发的头就不一样」）。
 */
export function clientVersion(ua: string | null | undefined): string {
  if (!ua) return '';
  const first = ua.trim().split(' ')[0];
  if (!first.includes('/')) return '';
  return first.split('/')[1].replace(/[^0-9A-Za-z.-]/g, '').slice(0, 24);
}

/** 客户端家族 → 展示名。未知值原样返回（上游改了 UA 归类也不该显示空白）。 */
export function clientLabel(client: string | null | undefined): string {
  const key = (client || '').toLowerCase();
  const map: Record<string, string> = {
    claude: 'Claude Code',
    codex: 'Codex',
    dsh: 'DeepSeek Harness',
    cursor: 'Cursor',
    vscode: 'VS Code',
    cli: 'CLI',
    other: '其它',
    unknown: '未知',
  };
  // 认不出的家族：后端现在会回落成 UA 的 product token（如 `my-agent`），
  // 原样显示比「其它」有用 —— 至少知道是什么东西在调用。
  return map[key] || client || '未知';
}

/**
 * 一条摘要的人话（用于详情页顶部的「这一轮发了什么」）。
 *
 * 只统计**结构规模**，不评判内容：用户看这三个数字就知道上下文有没有按预期
 * 增长（例如工具定义突然从 12 个涨到 40 个）。
 */
export function summaryText(s: TapTurnSummary | undefined): string {
  if (!s) return '—';
  return `${s.messages} 条消息 / ${s.tools} 个工具 / system ${s.system_chars} 字`;
}

/**
 * 差异是否为空。
 *
 * 界面上「和上一轮比」在无差异时应当明确说「两轮请求完全一致」——
 * 空面板会被读成「还没加载」。
 */
export function diffIsEmpty(d: TapDiff | null | undefined): boolean {
  if (!d) return true;
  const s = d.summary;
  return !s.params && !s.system && !s.added && !s.removed && !s.changed && !s.tools;
}

/** 差异条数合计：列表上「+3 / ~1」这类徽标用。 */
export function diffCount(d: TapDiff | null | undefined): number {
  if (!d) return 0;
  const s = d.summary;
  return s.params + s.system + s.added + s.removed + s.changed + s.tools;
}

/**
 * 会话里「上一轮」是谁。
 *
 * 差异视图选的是**相邻两轮**：agent 每一轮都会把上一轮的结果追加进去，所以
 * 「这一轮比上一轮多了什么」就是它这一步实际推进的内容。返回 null 表示这是
 * 第一轮，没有可比的。
 */
export function prevTurn<T extends {id: number; seq: number}>(
  turns: T[],
  seq: number,
): T | null {
  const idx = turns.findIndex((t) => t.seq === seq);
  if (idx <= 0) return null;
  return turns[idx - 1];
}

/** 状态码 → 语义色（徽标）。 */
export function statusTone(status: number | null | undefined): 'ok' | 'warn' | 'error' {
  const s = Number(status ?? 0);
  if (s >= 200 && s < 300) return 'ok';
  if (s >= 400 && s < 500) return 'warn';
  return 'error';
}

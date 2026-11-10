'use client';

import {useEffect, useMemo, useRef, useState} from 'react';
import {ChevronRight} from 'lucide-react';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';

/**
 * JSON 预览（Preview）：可折叠的树，而不是一整块 `<pre>`。
 *
 * 为什么要它：抓包的请求体动辄几十 KB（上百条历史消息 + 113 个工具定义），
 * 直接甩一坨格式化 JSON 出来，用户只能 Ctrl+F 大海捞针 —— 而且行号一多，
 * 「这一轮到底改了什么」根本看不出来。树形视图把「结构」摆在前面：
 * 默认展开两层，其余按需点开；长字符串单独折叠，不把一行撑到天边。
 *
 * 与 Raw 的分工：Raw 保留原文（空格、键序、转义原样），用来逐字节对照；
 * Preview 用来读。两者都不是「唯一的真相」，所以做成可切换，而不是二选一。
 */

/** 长字符串的折叠阈值：超过它默认收起，给「展开」按钮。 */
const LONG_STRING = 240;
/** 数组/对象在折叠状态下的摘要里最多显示多少项。 */
const SUMMARY_ITEMS = 6;

export type JsonPath = (string | number)[];
type Path = JsonPath;

function typeOf(value: unknown): 'object' | 'array' | 'string' | 'number' | 'boolean' | 'null' {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  const t = typeof value;
  if (t === 'string' || t === 'number' || t === 'boolean') return t;
  return 'object';
}

/** 一行的缩进：用 padding 而不是空格串，选中/高亮时背景能铺满。 */
function indent(depth: number) {
  return {paddingLeft: `${depth * 14 + 4}px`};
}

/**
 * 高亮定位：diff 里点「定位」跳到 Preview 时，需要把目标节点**展开并滚到眼前**。
 *
 * 用「路径前缀」判定：`["messages", 3]` 高亮时，它的祖先 `["messages"]` 也要展开，
 * 否则目标节点根本不在 DOM 里。这里由父节点逐层判断，不依赖外部滚动逻辑。
 */
function isPrefix(prefix: Path, path: Path): boolean {
  if (prefix.length > path.length) return false;
  return prefix.every((seg, i) => String(seg) === String(path[i]));
}

export function JsonView({
  value,
  name,
  path = [],
  depth = 0,
  defaultExpand = 2,
  highlight,
}: {
  value: unknown;
  /** 这一层的键名（根节点不传）。 */
  name?: string;
  path?: Path;
  depth?: number;
  defaultExpand?: number;
  /** 需要高亮的目标路径（diff 定位用）。 */
  highlight?: Path | null;
}) {
  const kind = typeOf(value);
  const container = kind === 'object' || kind === 'array';
  const highlighted = Boolean(highlight && highlight.length === path.length
    && isPrefix(highlight, path) && path.length > 0);
  // 默认展开：浅层直接摊开；带高亮时，祖先链必须全部展开
  const shouldOpen = highlight
    ? isPrefix(path, highlight) || depth < defaultExpand
    : depth < defaultExpand;
  const [open, setOpen] = useState(shouldOpen);
  const [showLong, setShowLong] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  // 高亮节点滚进视野（只在真正命中时滚一次）。
  //
  // 用 rAF 推迟一帧：从差异页「定位」过来时，节点是在**同一次提交**里刚展开的，
  // 立刻 scrollIntoView 拿到的还是旧布局（实测滚不到位，目标停在视口外 2000px）。
  useEffect(() => {
    if (!highlight || !highlighted) return;
    // 两段式：rAF 保证节点已经提交进 DOM，setTimeout 再等一次布局稳定。
    // 只做 rAF 时实测滚不到位（目标停在视口外 2000px）—— 树是在同一次提交里
    // 展开的，父节点的展开会继续改变高度，滚动位置随之作废。
    // 用 `block: 'center'` + 默认（非 smooth）行为：定位是"跳转"，不是"浏览"。
    const scroll = () => ref.current?.scrollIntoView({block: 'nearest'});
    const raf = window.requestAnimationFrame(() => {
      scroll();
      window.setTimeout(scroll, 120);
    });
    return () => window.cancelAnimationFrame(raf);
  }, [highlight, highlighted]);

  if (!container) {
    const isString = kind === 'string';
    const text = isString ? (value as string) : JSON.stringify(value);
    const long = isString && text.length > LONG_STRING;
    return (
      <div
        ref={ref}
        id={jsonAnchor(path)}
        style={indent(depth)}
        className={cn(
          'flex items-start gap-1 py-[1px] font-mono text-[11px] leading-5',
          highlighted && 'rounded bg-amber-500/20',
        )}
      >
        {name !== undefined && <span className="shrink-0 text-sky-600 dark:text-sky-400">{name}:</span>}
        <span className={cn('min-w-0 whitespace-pre-wrap break-all',
          kind === 'string' && 'text-emerald-700 dark:text-emerald-400',
          kind === 'number' && 'text-violet-600 dark:text-violet-400',
          kind === 'boolean' && 'text-orange-600 dark:text-orange-400',
          kind === 'null' && 'text-muted-foreground')}>
          {isString ? `"${long && !showLong ? `${text.slice(0, LONG_STRING)}…` : text}"` : text}
        </span>
        {long && (
          <button type="button" onClick={() => setShowLong((v) => !v)}
                  className="shrink-0 rounded border px-1 text-[10px] text-muted-foreground hover:bg-muted">
            {showLong ? '−' : `+${text.length - LONG_STRING}`}
          </button>
        )}
      </div>
    );
  }

  const entries: [string | number, unknown][] = kind === 'array'
    ? (value as unknown[]).map((v, i) => [i, v])
    : Object.entries(value as Record<string, unknown>);
  const summary = kind === 'array'
    ? `[…${entries.length}]`
    : `{…${entries.length}}`;
  const preview = entries.slice(0, SUMMARY_ITEMS)
    .map(([k, v]) => `${k}: ${shortOf(v)}`)
    .join(', ');

  return (
    <div ref={ref} id={jsonAnchor(path)}>
      <div
        style={indent(depth)}
        className={cn(
          'flex cursor-pointer items-center gap-1 py-[1px] font-mono text-[11px] leading-5 hover:bg-muted/50',
          highlighted && 'rounded bg-amber-500/20',
        )}
        onClick={() => setOpen((v) => !v)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setOpen((v) => !v); }}
      >
        <ChevronRight className={cn('h-3 w-3 shrink-0 text-muted-foreground transition-transform',
          open && 'rotate-90')} />
        {name !== undefined && <span className="shrink-0 text-sky-600 dark:text-sky-400">{name}:</span>}
        <span className="text-muted-foreground">{summary}</span>
        {!open && entries.length > 0 && (
          <span className="truncate text-muted-foreground/70">{preview}</span>
        )}
      </div>
      {open && entries.map(([key, child]) => (
        <JsonView
          key={String(key)}
          value={child}
          name={String(key)}
          path={[...path, key]}
          depth={depth + 1}
          defaultExpand={defaultExpand}
          highlight={highlight}
        />
      ))}
    </div>
  );
}

/** 折叠状态下的单行摘要：对象/数组给类型，字符串截断。 */
function shortOf(value: unknown): string {
  const kind = typeOf(value);
  if (kind === 'object') return '{…}';
  if (kind === 'array') return `[${(value as unknown[]).length}]`;
  if (kind === 'string') {
    const s = value as string;
    return `"${s.length > 24 ? `${s.slice(0, 24)}…` : s}"`;
  }
  return JSON.stringify(value);
}

/** DOM 锚点：diff 定位靠它滚动，路径进 id 时把非字母数字换掉。 */
export function jsonAnchor(path: Path): string {
  return `json-${path.map((seg) => String(seg).replace(/[^\w-]/g, '_')).join('.') || 'root'}`;
}

/** 把任意值渲染成 Preview 树的薄封装（供页面少写点 props）。 */
export function JsonPreview({value, highlight, defaultExpand = 2}: {
  value: unknown;
  highlight?: Path | null;
  defaultExpand?: number;
}) {
  const t = useT();
  // 顶层不是对象/数组时（例如一段纯文本），直接按标量渲染
  const isScalar = useMemo(() => {
    const kind = typeOf(value);
    return kind !== 'object' && kind !== 'array';
  }, [value]);
  if (value === undefined) {
    return <p className="text-[11px] text-muted-foreground">{t('trace.notCaptured')}</p>;
  }
  return (
    <div className="max-h-80 overflow-auto rounded-md bg-muted/30 py-1">
      <JsonView value={value} defaultExpand={isScalar ? 0 : defaultExpand} highlight={highlight} />
    </div>
  );
}

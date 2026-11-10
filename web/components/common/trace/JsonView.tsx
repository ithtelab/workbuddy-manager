'use client';

/**
 * 抓包报文的 JSON 预览。
 *
 * 用 [**react-obj-view**](https://github.com/vothanhdat/react-obj-view)（虚拟化渲染、
 * 零运行时依赖、React 19 原生）。换掉 @uiw/react-json-view 的原因：
 *
 * 1. **按需加载是库内建能力**，不用在数据层预切：`arrayGroupSize` / `objectGroupSize`
 *   把大集合变成「分组节点」（`0 … 9`、`10 … 19`），点开才枚举那一组 —— 这正是
 *   「默认前 N 项、点加载更多继续看」的交互，而且是**无限层**的（每组里还能再分组），
 *   比一次性渲染 20 条再「加载更多」按钮流畅得多。
 * 2. **虚拟化**：只渲染可见行，10 万+ 节点不卡。抓包的 325 条消息展开后是几千行，
 *    前一个库没有虚拟化，DOM 数量随展开线性涨。
 * 3. 自研的 `salvageJson`（截断体救援）与 `jsonAnchor`（定位锚点）保留，仍被
 *    page 层使用。
 *
 * 分组大小取 10：与「默认前 10 项、每次多看 10 项」的直觉一致。
 */
import {ObjectView, themeDefault} from 'react-obj-view';
import type {ObjectViewHandle} from 'react-obj-view';
import 'react-obj-view/dist/react-obj-view.css';
import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import type {JsonPath} from '@/lib/json-path';



/** 分组大小：每组 10 项 —— 与「默认前 10、每次 +10」的交互直觉一致。 */
const GROUP_SIZE = 10;

/**
 * 从**被截断**的 JSON 文本里抢救出「能解析的前缀」，补齐未闭合的括号。
 *
 * 截断体本身是非法 JSON（半条消息、半个工具定义）。逐字符扫描，记录字符串
 * 边界与括号栈，扫到结尾后按栈补齐右括号 —— 得到「原文的前缀 + 合法闭合」，
 * 树就能正常渲染。返回 null 表示连前缀都切不出来，调用方退回原文。
 */
export function salvageJson(text: string): unknown | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return null;
  const stack: Array<'{' | '['> = [];
  let inString = false;
  let escaped = false;
  let end = 0;
  for (let i = 0; i < trimmed.length; i++) {
    const ch = trimmed[i] as string;
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') { inString = true; continue; }
    if (ch === '{' || ch === '[') { stack.push(ch as '{' | '['); continue; }
    if (ch === '}' || ch === ']') {
      stack.pop();
      if (stack.length === 0) { end = i + 1; break; }
      end = i + 1;
    }
    if (ch === ',' && stack.length > 0) end = i;
  }
  if (end <= 0) return null;
  let prefix = trimmed.slice(0, end);
  for (const open of stack.reverse()) prefix += open === '{' ? '}' : ']';
  prefix = prefix.replace(/[,:\s]+$/, '');
  try {
    return JSON.parse(prefix);
  } catch {
    return null;
  }
}

/** DOM 锚点：diff 定位靠它滚动，路径进 id 时把非字母数字换掉。 */
export function jsonAnchor(path: JsonPath): string {
  return `json-${path.map((seg) => String(seg).replace(/[^\w-]/g, '_')).join('.') || 'root'}`;
}

export function JsonPreview({value, expandAll, focusPath, maxHeight = 'max-h-[60vh]'}: {
  value: unknown;
  highlight?: JsonPath | null;
  /** true=全展开，false=全收起，undefined=默认展开 2 层。 */
  expandAll?: boolean;
  /**
   * 要定位到的路径（diff「定位」用）。传进来会自动**展开沿途节点**并滚到那一行。
   *
   * 为什么由这里做而不是页面层：库的 `scrollToPaths` 只会滚动，**不会展开** ——
   * 目标节点在折叠状态下根本不在 DOM 里，滚过去也是空的。库的
   * `expandAndGetIndex` 正好做这件事：沿途展开并返回行索引。
   */
  focusPath?: JsonPath | null;
  maxHeight?: string;
}) {
  const t = useT();
  const stable = useMemo(() => value, [value]);
  const viewRef = useRef<ObjectViewHandle | undefined>(undefined);
  const lastFocus = useRef<string>('');
  /**
   * 定位路径的**根前缀**：完整体的根就是 body 本身，而截断体的根是
   * {headers, upstream_headers, body} —— 两种形状差一层。传进来的路径是相对
   * **body** 的（diff 那边比的就是 body），所以这里按实际形状决定要不要补
   * 'body' 这一层（补错就找不到那一条）。
   */
  const rootPrefix = useMemo(() => {
    if (!value || typeof value !== 'object') return [];
    const keys = Object.keys(value as Record<string, unknown>);
    // 根里有 body 且它是个对象 → 截断体形状，消息在 body 下
    if (keys.includes('body') && typeof (value as Record<string, unknown>).body === 'object') {
      return ['body'];
    }
    return [];
  }, [value]);

  /**
   * 定位模式下**关闭分组**并临时全展开。
   *
   * 为什么必须关分组：库的分组会把大数组代理成「`50 … 60`」这样的**复合键**，
   * 路径 `messages.60` 在分组态下根本不存在 —— 库的路径查找拿到的是分组代理，
   * 按索引找不到任何东西（实测：61 条消息的会话，定位后仍停在分组列表上）。
   *
   * 为什么必须全展开：`scrollToPaths` 只负责**滚动**，不改展开状态；折叠着的
   * `messages` 下并没有 `messages[60]` 这一行，目标行不在 DOM 里，滚过去是空的。
   *
   * 代价是展开后行数变多 —— 但虚拟化只渲染可见行，DOM 数量不涨。
   */
  const [focusMode, setFocusMode] = useState(false);

  /** 展开到目标路径并滚过去。路径不存在时静默返回（不报错、不打断）。 */
  const focus = useCallback(async (path: JsonPath) => {
    const handle = viewRef.current;
    if (!handle || path.length === 0) return;
    // 树可能还没初始化完（库的 state 未就绪时会抛 "Paths not inited"），
    // 所以重试几次：每次间隔一帧，等它准备好。
    for (let attempt = 0; attempt < 8; attempt++) {
      try {
        await handle.scrollToPaths(path as PropertyKey[]);
        return;
      } catch {
        await new Promise((r) => window.setTimeout(r, 60));
      }
    }
    // 路径不存在于当前数据（截断体抢救出来的前缀可能缺这一段）：不打扰用户
  }, []);

  useEffect(() => {
    if (!focusPath || focusPath.length === 0) return;
    const key = focusPath.join('.');
    if (key === lastFocus.current) return;
    lastFocus.current = key;
    setFocusMode(true);
    // 等树按新数据挂载完、并完成这一轮展开后再定位
    const timer = window.setTimeout(() => void focus([...rootPrefix, ...focusPath]), 220);
    return () => window.clearTimeout(timer);
  }, [focusPath, focus, rootPrefix]);

  if (value === undefined || value === null) {
    return <p className="text-[11px] text-muted-foreground">{t('trace.notCaptured')}</p>;
  }

  return (
    // min-w-0：树内长字符串靠库的省略号折叠，容器不能被撑开
    <div className={cn(
      'min-w-0 overflow-auto rounded-md border bg-background/60',
      maxHeight,
    )}>
      <ObjectView
        ref={viewRef}
        valueGetter={() => stable}
        // 分组：大集合变成「0 … 9 / 10 … 19」的分组节点，点开才枚举 —— 按需加载。
        // 定位模式下关掉：分组键是「50 … 60」这种复合键，按索引的路径找不到。
        arrayGroupSize={focusMode ? 0 : GROUP_SIZE}
        objectGroupSize={focusMode ? 0 : GROUP_SIZE}
        // expandLevel：默认展开 2 层（与「默认前 10 项」的直觉一致）；
        // expandAll 信号切换全展开/全收起；定位模式下临时全展开（见上方注释）
        expandLevel={focusMode ? 100 : expandAll === undefined ? 2 : expandAll ? 100 : false}
        showLineNumbers={false}
        lineHeight={18}
        overscan={40}
        style={themeDefault}
        className="text-[12px]"
      />
    </div>
  );
}

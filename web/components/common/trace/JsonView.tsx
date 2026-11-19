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
import {CopyButton} from '@/components/ui/copy-button';
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
  // 安全切点：只在**字符串之外**记录 —— 截断常发生在某个字符串中间
  // （例如 `"name": "bas`），那样的切点会留下一个未闭合的字符串，
  // JSON.parse 必然失败，整个抢救作废（实测：minimax 那条 512 KB 记录，
  // 请求体在界面上完全空白）。
  const safeEnds: number[] = [];
  let lastComma = -1;
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
      if (stack.length) stack.pop();
      safeEnds.push(i + 1);
      if (stack.length === 0) break;
      continue;
    }
    if (ch === ',' && stack.length > 0) lastComma = i;
  }
  if (lastComma > 0) safeEnds.push(lastComma);
  if (!safeEnds.length) return null;
  // 从后往前试：越靠后给的内容越全，但必须能解析
  for (const end of [...new Set(safeEnds)].sort((a, b) => b - a)) {
    if (end <= 0) continue;
    let prefix = trimmed.slice(0, end);
    for (const open of bracketDepth(trimmed, end).reverse()) {
      prefix += open === '{' ? '}' : ']';
    }
    prefix = prefix.replace(/[,:\s]+$/, '');
    if (!prefix) continue;
    try {
      return JSON.parse(prefix);
    } catch {
      continue;
    }
  }
  return null;
}

/** 文本前 upto 个字符处仍未闭合的括号（栈）。 */
function bracketDepth(text: string, upto: number): Array<'{' | '['> {
  const stack: Array<'{' | '['> = [];
  let inString = false;
  let escaped = false;
  for (const ch of text.slice(0, upto)) {
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') { inString = true; continue; }
    if (ch === '{' || ch === '[') stack.push(ch as '{' | '[');
    else if ((ch === '}' || ch === ']') && stack.length) stack.pop();
  }
  return stack;
}

/** DOM 锚点：diff 定位靠它滚动，路径进 id 时把非字母数字换掉。 */
export function jsonAnchor(path: JsonPath): string {
  return `json-${path.map((seg) => String(seg).replace(/[^\w-]/g, '_')).join('.') || 'root'}`;
}

/**
 * 从容器里按**键**取子值。
 *
 * 库把数组元素渲染成 `0`、`1` 这样的键名，所以行里的键是字符串形式的下标。
 */
function childOf(container: unknown, key: string): unknown {
  if (Array.isArray(container)) {
    const idx = Number(key);
    return Number.isInteger(idx) && idx >= 0 ? container[idx] : undefined;
  }
  if (container && typeof container === 'object') {
    return (container as Record<string, unknown>)[key];
  }
  return undefined;
}

/** 按路径取数据里的值。 */
function getByPath(root: unknown, path: JsonPath): unknown {
  let cur: unknown = root;
  for (const seg of path) {
    cur = childOf(cur, String(seg));
    if (cur === undefined) return undefined;
  }
  return cur;
}

/** 分组节点（`0 … 10`）在路径上是**透明**的：它只是把一批真实键包起来。 */
function isGroupLabel(key: string): boolean {
  return key.indexOf('\u2026') >= 0;
}

/**
 * 可见行 → 数据路径。
 *
 * 为什么不用「按内容匹配」：内容匹配**有歧义** —— 悬停 `role: "system"` 时，
 * "system" 也出现在下一条消息的正文里，取「最长匹配」就会跳到别人的内容上
 * （实测：悬停 system，弹出来的是 16267 字符的下一条消息正文）。
 *
 * 改成**按层级重建路径**：每行带着缩进层级和键名，从 ROOT 顺着层级把容器
 * 一层层解出来 —— 路径唯一，指向的值必然正确，不受同名内容干扰。
 */
function rowPaths(container: HTMLElement, root: unknown): Map<Element, JsonPath> {
  const map = new Map<Element, JsonPath>();
  const stack: {container: unknown; path: JsonPath}[] = [];
  const rows = container.querySelectorAll('.row');
  for (const row of rows) {
    const nameEl = row.querySelector('.name');
    if (!nameEl) continue;
    const depth = row.querySelector('.tree-indents')?.children.length ?? 0;
    const key = nameEl.textContent ?? '';
    if (depth === 0) {
      stack[0] = {container: root, path: []};
      map.set(row, []);
      continue;
    }
    const parent = stack[depth - 1];
    if (!parent) continue;
    if (isGroupLabel(key)) {
      stack[depth] = parent; // 分组透明：它的孩子才是真实键
      continue;
    }
    const child = childOf(parent.container, key);
    if (child === undefined) {
      stack[depth] = parent;
      continue;
    }
    const path: JsonPath = [...parent.path, key];
    stack[depth] = {container: child, path};
    map.set(row, path);
  }
  return map;
}

/**
 * preview 行里某个值对应的**键名**。
 *
 * 库把折叠的对象渲染成 `{role: 'system', content: '...'}` —— 每个键名前面
 * 有一个 `.name` span，取值节点**前面最近**的那个就是它的键。
 */
function previewKeyOf(holder: HTMLElement): string | null {
  let prev = holder.previousElementSibling;
  while (prev) {
    if (prev.classList.contains('name')) return prev.textContent?.trim() || null;
    const inner = prev.querySelector('.name');
    if (inner?.textContent) return inner.textContent.trim();
    prev = prev.previousElementSibling;
  }
  return null;
}

/**
 * 兜底：从 DOM 里读出字符串原文。
 *
 * 库把长字符串渲染成 `<span class="popover-anchor">"摘要..."</span>` +
 * `<pre class="popover-content">完整转义文本</pre>` —— `pre` 里是**完整**的
 * JSON 转义串，解析它就得到精确原文（换行是真的换行）。
 */
function domStringValue(holder: HTMLElement): string | null {
  const pre = holder.querySelector('pre.popover-content');
  if (pre) {
    const t = (pre.textContent ?? '').trim();
    if (t) {
      try {
        const parsed = JSON.parse('"' + t + '"');
        if (typeof parsed === 'string') return parsed;
      } catch {
        /* 落到下面 */
      }
    }
  }
  const own = (holder.textContent ?? '').trim();
  if (own.length >= 2 && own[0] === '"' && own[own.length - 1] === '"') {
    try {
      const parsed = JSON.parse(own);
      if (typeof parsed === 'string') return parsed;
    } catch {
      /* 不是合法 JSON 串 */
    }
  }
  return null;
}

/**
 * 悬停的值 → 原文（精确）。
 *
 * 优先级：① 按路径从数据里取（唯一、不会认错）；② 兜底从 DOM 的 popover 全文
 * 里解析；③ 都没有 → 返回 null（不开窗，不给用户看错的内容）。
 */
function resolveHoveredString(
  holder: HTMLElement,
  root: unknown,
  container: HTMLElement,
): string | null {
  const row = holder.closest('.row');
  if (row) {
    const path = rowPaths(container, root).get(row);
    if (path) {
      const node = getByPath(root, path);
      if (typeof node === 'string') return node;
      if (node && typeof node === 'object') {
        // preview 行：值在对象的某个键下，取悬停值对应的键
        const key = previewKeyOf(holder);
        if (key !== null) {
          const inner = (node as Record<string, unknown>)[key];
          if (typeof inner === 'string') return inner;
        }
      }
    }
  }
  return domStringValue(holder);
}


/**
 * 悬停浮窗：显示字符串的完整内容，带 **Preview / Raw** 切换。
 *
 * 为什么改成浮窗（而不是点开整页详情）：字符串可能又长又多（system prompt、
 * 工具描述），点一下跳走再回来太打断；浮窗随指针出现，扫一眼就够。
 *
 * · **Preview（默认）**：按**真实换行**渲染，段落结构保留 —— 库里原生浮窗把
 *   换行显示成字面量 `\n`，读起来像一坨（实测：minimax 第 155 轮的 system
 *   prompt 满屏 `\n`）。
 * · **Raw**：JSON 转义后的原文，逐字节对照 / 复制成合法 JSON 片段用。
 *
 * **位置固定**：浮窗出现在指针处之后就**钉住不动**，鼠标可以移上去点 Preview/Raw、
 * 按复制 —— 之前它跟着指针走，想点按钮就得先把指针按在原处，不能移过去。
 */
function StringHover({value, x, y, panelRef, onEnter, onLeave}: {
  value: string;
  x: number;
  y: number;
  panelRef: React.RefObject<HTMLDivElement | null>;
  /** 指针进/出浮窗：进则取消「延迟关闭」，出则重新开始计时。 */
  onEnter?: () => void;
  onLeave?: () => void;
}) {
  const t = useT();
  const [mode, setMode] = useState<'preview' | 'raw'>('preview');
  const raw = useMemo(() => {
    try {
      return JSON.stringify(value);
    } catch {
      return value;
    }
  }, [value]);
  /**
   * 贴边保护：浮窗不越出视口。
   *
   * 关键是**贴着指针放**，而不是一律放到「视口底部往上 420px」的位置 ——
   * 那样做虽然不越界，但指针在屏幕下方时浮窗会被甩到很远的地方，鼠标根本
   * 够不着（实测：指针在树的中下部，浮窗却出现在视口顶部附近）。
   *
   * 做法：默认放在指针**下方**；下方空间不够就翻到**上方**；左右同理。
   * 这样浮窗始终紧挨指针，鼠标挪过去就能进浮窗。
   */
  const PANEL_W = 600;
  const PANEL_H = 420;
  const gap = 18;
  let left = x + 12;
  if (left + PANEL_W > window.innerWidth - 8) left = x - PANEL_W - 12;
  left = Math.max(8, Math.min(left, window.innerWidth - PANEL_W - 8));
  let top = y + gap;
  if (top + PANEL_H > window.innerHeight - 8) top = y - PANEL_H - gap;
  top = Math.max(8, Math.min(top, window.innerHeight - PANEL_H - 8));
  return (
    <div
      ref={panelRef}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
      className="pointer-events-auto fixed z-50 w-[600px] max-w-[calc(100vw-16px)] overflow-hidden rounded-lg border bg-popover shadow-xl"
      style={{left, top}}
      role="dialog"
      aria-label={t('trace.stringDetail')}
    >
      <div className="flex items-center justify-between gap-2 border-b px-2 py-1">
        <div className="flex items-center gap-1 rounded-full border p-0.5 text-[10px]">
          {(['preview', 'raw'] as const).map((key) => (
            <button
              key={key}
              type="button"
              aria-pressed={mode === key}
              onClick={() => setMode(key)}
              className={cn(
                'rounded-full px-2 py-0.5 transition-colors',
                mode === key
                  ? 'bg-foreground/10 font-medium'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {t(`trace.stringMode.${key}`)}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-muted-foreground">
            {t('trace.stringStats', {
              chars: value.length,
              lines: value.split('\n').length,
            })}
          </span>
          <CopyButton value={mode === 'raw' ? raw : value} label={t('trace.copyValue')} />
        </div>
      </div>
      <pre className="max-h-[360px] overflow-auto whitespace-pre-wrap break-words p-2 font-mono text-[11px] leading-5">
        {mode === 'preview' ? value : raw}
      </pre>
    </div>
  );
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
  /**
   * 字符串详情：点树里的字符串值打开。
   *
   * 为什么用**事件委托**而不是重写整行渲染：库的 `RowRenderer` 要连带重做缩进、
   * 展开箭头、键名、粘性表头，与它的虚拟化/粘性逻辑冲突，改起来脆。委托只要在
   * 容器上听一次 click，命中 `.value.type-string` 就取出文本 —— 库的类名是稳定的
   * 公开约定（CSS 里明确定义），比复制它的内部渲染稳。
   */
  /** 悬停浮窗：当前指向的字符串原文 + 指针位置（首次出现时钉住）。 */
  const [hovered, setHovered] = useState<{value: string; x: number; y: number} | null>(null);
  /** 浮窗 DOM：指针移到浮窗上时要认得它，别当成「离开了字符串」而关掉。 */
  const panelRef = useRef<HTMLDivElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);


  /**
   * 点到字符串值 → 打开详情。
   *
   * 匹配方式：**内容前缀**。库里把值渲染成「JSON 转义 + 引号」，长字符串**完整**
   * 渲染（不截断），对象里的字符串则被折进 preview 摘要 —— 摘要里也有内容片段，
   * 所以按内容前缀找就能对上（实测 6916 字符的 system prompt 能精确匹配到）。
   *
   * 为什么不按「第几个」：库的虚拟化只渲染可见行，位置假设必然错位（第一版就是
   * 这么写的，长字符串一个都弹不出来）。
   */
  /**
   * 离开时**延迟关闭**。
   *
   * 之前一离开文本行就立刻关掉，鼠标根本来不及挪到浮窗上（浮窗现在紧挨指针，
   * 但中间仍有一段距离）。给 1 秒的宽限期：这段时间里如果指针进了浮窗就保留，
   * 否则才关。
   */
  const closeTimer = useRef<number | null>(null);
  const cancelClose = useCallback(() => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }, []);
  const scheduleClose = useCallback(() => {
    cancelClose();
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null;
      setHovered(null);
    }, 1000);
  }, [cancelClose]);
  // 卸载时清掉定时器，别在组件没了之后还去 setState
  useEffect(() => () => {
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
  }, []);

  /**
   * 点击浮窗**外面** → 立即关闭。
   *
   * 为什么不能只靠「延迟关闭」：鼠标移出文本行后，浮窗会挂在那儿 1 秒。
   * 这 1 秒里用户点了别处（比如想点页面上的另一个控件），浮窗却还杵在屏幕
   * 中间挡着 —— 该关的时候没关。
   *
   * 为什么用 `pointerdown` 而不是 `click`：`pointerdown` 在按下瞬间就触发，
   * 用户点别的控件时，浮窗会在**那个控件收到点击之前**消失，不会出现
   * 「浮窗先抢走焦点、按钮点不动」。
   *
   * 捕获阶段监听 + 不阻止传播：只监听，不干预别人的点击。
   */
  useEffect(() => {
    if (!hovered) return;
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node | null;
      // 点在浮窗里 → 不关（还要让人点 Preview/Raw、复制）
      if (t && panelRef.current?.contains(t)) return;
      cancelClose();
      setHovered(null);
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [hovered, cancelClose]);

  /**
   * 悬停到字符串上 → 弹出带 Preview/Raw 切换的浮窗。
   *
   * 取原文靠**路径**而不是内容匹配：内容匹配有歧义（悬停 `role: "system"` 会
   * 匹配到下一条消息正文里的 "system"，弹出来的是别人的内容）。
   */
  const onTreeMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement | null;
    // 指针在浮窗上（准备点 Preview/Raw 或复制）：**保持浮窗**，既不换内容也不挪位置
    if (target && panelRef.current?.contains(target)) {
      cancelClose();
      return;
    }
    // **只认最内层的值节点**（.value.type-string）：父对象的 preview 摘要
    // （`{role:'system', content:'…'}`）不是值节点，悬停它不弹 —— 否则整行到处
    // 都弹窗，反而看不清指的是哪个值。
    //
    // 注意：没命中时**不能直接关**。指针从文本行挪到浮窗的路上会经过别的
    // 元素（键名、行间空白），直接关掉的话浮窗在鼠标到达之前就没了 ——
    // 所以这里走「延迟关闭」，给 1 秒宽限期。
    const holder = target?.closest('.value.type-string');
    if (!holder) {
      scheduleClose();
      return;
    }
    const raw = resolveHoveredString(
      holder as HTMLElement, stable, rootRef.current ?? (holder as HTMLElement),
    );
    if (raw === null || (raw.length <= 80 && !raw.includes('\n'))) {
      scheduleClose();
      return;
    }
    // 命中了：取消关闭计时（浮窗留着，内容换成当前这个字符串）
    cancelClose();
    setHovered((prev) => {
      // 同一个字符串：**位置钉住不动**（只更新内容）—— 浮窗跟着指针走的话，
      // 鼠标没法移到浮窗上去点按钮；换字符串才重新定位。
      if (prev && prev.value === raw) return prev;
      return {value: raw, x: e.clientX, y: e.clientY};
    });
  }, [stable, cancelClose, scheduleClose]);

  const onTreeLeave = useCallback(() => scheduleClose(), [scheduleClose]);
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
    <div
      ref={rootRef}
      // 悬停监听挂在 **capture 阶段**：库的字符串预览用了 CSS popover，
      // 它的容器会 stopPropagation —— 冒泡阶段根本收不到 mousemove，
      // 浮窗就不出现（实测：minimax 第 155 轮的 system prompt 悬停无反应）。
      onMouseMoveCapture={onTreeMove}
      onMouseLeave={onTreeLeave}
      className={cn(
        'min-w-0 overflow-auto rounded-md border bg-background/60',
        maxHeight,
      )}
    >
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
        // 关掉「数据更新时闪红」：库比较的是**对象引用**，我们每次重拉数据都是
        // 新的对象 → 每次刷新 ROOT 都闪一下红，看着像在报错（实测：自动刷新
        // 一开，ROOT 每 20 秒红一次）。
        // 这个闪烁本来是给「同一棵树内某个值真的变了」的提示用的，但抓包页
        // 每次刷新整棵树都是新的，区分不出「哪一行变了」，只会一直闪。
        highlightUpdate={false}
        showLineNumbers={false}
        lineHeight={18}
        overscan={40}
        style={themeDefault}
        className="text-[12px]"
      />
      {hovered !== null && (
        <StringHover
          value={hovered.value}
          x={hovered.x}
          y={hovered.y}
          panelRef={panelRef}
          onEnter={cancelClose}
          onLeave={scheduleClose}
        />
      )}
    </div>
  );
}


/* ── 字符串详情弹层 ─────────────────────────────────────────── */

/**
 * 字符串值的详情视图。
 *
 * 为什么要有：JSON 里的长文本（system prompt、工具描述、模型回复）在树里只显示
 * 一小截，想看全文得点开 —— 而库里自带的字符串 popover **只有原文一种视图**，
 * 且把换行显示成字面量 `\n`（实测：读 system prompt 时满屏 `\n`，根本没法读）。
 *
 * 两种视图：
 * · **Preview（默认）**：按真实换行渲染，保留段落结构；可读性最好。
 * · **Raw**：JSON 转义后的原文，逐字节对照用（复制出去就是合法 JSON 片段）。
 */
function StringDetail({value, onClose}: {value: string; onClose: () => void}) {
  const t = useT();
  const [mode, setMode] = useState<'preview' | 'raw'>('preview');

  // Esc 关闭：弹层不是模态，Esc 是最顺手的退出键
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const raw = useMemo(() => {
    try {
      return JSON.stringify(value);
    } catch {
      return value;
    }
  }, [value]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-6"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={t('trace.stringDetail')}
    >
      <div
        className="mt-12 flex max-h-[75vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border bg-background shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2">
          <div className="flex items-center gap-1 rounded-full border p-0.5 text-[11px]">
            {(['preview', 'raw'] as const).map((key) => (
              <button
                key={key}
                type="button"
                aria-pressed={mode === key}
                onClick={() => setMode(key)}
                className={cn(
                  'rounded-full px-2 py-0.5 transition-colors',
                  mode === key
                    ? 'bg-foreground/10 font-medium'
                    : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {t(`trace.stringMode.${key}`)}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-muted-foreground">
              {t('trace.stringStats', {
                chars: value.length,
                lines: value.split('\n').length,
              })}
            </span>
            <CopyButton value={mode === 'raw' ? raw : value}
                        label={t('trace.copyValue')} />
            <button
              type="button"
              onClick={onClose}
              className="rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-muted"
            >
              {t('common.close')}
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          {mode === 'preview' ? (
            // 真实换行：white-space: pre-wrap 让 \n 变成换行；不转义引号与缩进
            <pre className="whitespace-pre-wrap break-words p-3 font-mono text-[11px] leading-5">
              {value}
            </pre>
          ) : (
            <pre className="whitespace-pre-wrap break-words p-3 font-mono text-[11px] leading-5">
              {raw}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * 树里的字符串值：短的直接显示；长的渲染成可点击的摘要，点开看详情。
 *
 * 悬停 vs 点击：这里用**点击**（悬停会在滚动时不断弹层，反而干扰阅读）。
 */
export function StringValue({value}: {value: string}) {
  const [open, setOpen] = useState(false);
  const t = useT();
  const long = value.length > 80 || value.includes('\n');
  const preview = useMemo(() => {
    const oneLine = value.replace(/\s+/g, ' ').trim();
    return oneLine.length > 120 ? `${oneLine.slice(0, 120)}…` : oneLine;
  }, [value]);

  if (!long) {
    return (
      <span className="break-words text-emerald-700 dark:text-emerald-400">
        &quot;{value}&quot;
      </span>
    );
  }
  return (
    <>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen(true);
        }}
        title={t('trace.stringDetail')}
        className="max-w-full break-words text-left text-emerald-700 underline decoration-dotted underline-offset-2 dark:text-emerald-400"
      >
        &quot;{preview}&quot;
      </button>
      {open && <StringDetail value={value} onClose={() => setOpen(false)} />}
    </>
  );
}

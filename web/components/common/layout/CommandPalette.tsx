'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {useRouter} from 'next/navigation';
import {CornerDownLeft, Search} from 'lucide-react';

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {matchCommands, navCommands, type PaletteItem} from '@/lib/command-palette';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';

/**
 * 命令面板（⌘K / Ctrl+K）—— 批次 5 ③。
 *
 * **它解决什么**：见 `@/lib/command-palette` 的说明。这里只说界面上那几个
 * 不显然的决定。
 *
 * ## 为什么入口是一个可见的按钮，而不只是快捷键
 *
 * 快捷键是**给已经知道它的人**用的。这一批的标题是「可发现性」，所以右上角
 * 控件行里有一个能点的「搜索」按钮（与语言 / 版本并排）——不知道 ⌘K 的人
 * 也能看见它。快捷键只是同一件事的第二条路径。
 *
 * ## 为什么面板里**只有跳转**，没有任何动作
 *
 * `PaletteItem` 的类型留了「有 `id` 无 `href` 就走动作」的口子，但这一版一条
 * 动作都不放。理由：⌘K 的用法是「敲几个字 + 回车」，回车之前的那个词往往只
 * 打了一半；在这种地方放「退出登录」「重启容器」这类不可逆操作，等于把
 * **最容易误触的交互**接到**最危险的按钮**上。真要加，得先有二次确认的形态，
 * 那是另一批的事。`server/tests/test_command_palette.py` 会把这条钉住。
 *
 * ## 为什么不做「最近使用」置顶
 *
 * 空查询时的顺序就是清单顺序（`NAV_COMMANDS` 的排列即约定，且被守卫测试
 * 逐条比对）。按使用历史重排会让同一句查询在不同机器上给出不同结果，也让
 * 「第一项是什么」变得无法预测——用户按方向键的肌肉记忆会失效。省下的那一次
 * 输入，不值得换掉一个稳定的顺序。
 *
 * ## 为什么跳转用 `router.push` 而**不**补 basePath
 *
 * `next/navigation` 的 `router.push` 由 Next 自己加部署前缀；再套一层
 * `withBasePath` 会变成 `/wb/wb/tasks`。这与 `components/ui/floating-dock.tsx`
 * 相反——那边是原生 `<a href>`，Next 管不到，所以**必须**自己补。两处看着
 * 矛盾，其实判据只有一条：**这段路径经不经 Next 的路由器**。
 */
export function CommandPalette() {
  const t = useT();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  /**
   * 快捷键标签要按平台渲染（Mac 是 ⌘K，其它是 Ctrl K），而服务端不知道用户
   * 用的是什么系统——首帧就渲染会在水合时报警告。挂载后再渲染，代价是按钮
   * 上的那一小格晚一帧出现（`ManagementBar` 的 `mounted` 是同一取舍）。
   */
  const [mounted, setMounted] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const items = useMemo(() => navCommands(t), [t]);
  const results = useMemo(() => matchCommands(items, query), [items, query]);

  useEffect(() => {
    setMounted(true);
  }, []);

  // 全局快捷键。挂在 window 上而不是面板内部：面板没打开时它根本不存在。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
      if (event.key.toLowerCase() !== 'k') return;
      // 必须拦掉默认行为：浏览器自带的 Ctrl+K 会去聚焦地址栏/搜索框，
      // 焦点被抢走之后面板即使打开也收不到键盘。
      event.preventDefault();
      setOpen((current) => !current);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  // 每次打开都从干净状态开始。带着上一次的查询词会让人以为「搜不到东西」，
  // 而实际上只是被过滤空了。放在 effect 里而不是每个入口各清一次：
  // 入口有两个（按钮 + 快捷键），写两处迟早漏一处。
  useEffect(() => {
    if (!open) return;
    setQuery('');
    setActiveIndex(0);
  }, [open]);

  const activate = useCallback(
    (item: PaletteItem | undefined) => {
      if (!item?.href) return;
      setOpen(false);
      router.push(item.href);
    },
    [router],
  );

  const move = useCallback(
    (delta: number) => {
      setActiveIndex((current) => {
        const count = results.length;
        if (count === 0) return 0;
        // 取模让它首尾相接：在最后一项按 ↓ 回到第一项，比「卡住不动」好——
        // 卡住会让人以为键盘坏了。
        return (current + delta + count) % count;
      });
    },
    [results.length],
  );

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLInputElement>) => {
      // 中文/日文输入法：回车是「确认候选词」，不是「打开这一项」。
      // 不拦掉的话，用拼音打 `zhanghao` 再按回车会直接跳走，而且跳到哪一条
      // 取决于当时高亮的是谁——用户完全无法预期。
      if (event.nativeEvent.isComposing) return;

      if (event.key === 'ArrowDown') {
        event.preventDefault();
        move(1);
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        move(-1);
      } else if (event.key === 'Home') {
        event.preventDefault();
        setActiveIndex(0);
      } else if (event.key === 'End') {
        event.preventDefault();
        setActiveIndex(Math.max(0, results.length - 1));
      } else if (event.key === 'Enter') {
        event.preventDefault();
        activate(results[activeIndex]);
      }
    },
    [activate, activeIndex, move, results],
  );

  // 键盘移动高亮时把它滚进视野。用 `block: 'nearest'` 而不是 `center`：
  // 一次只挪一格，居中会让整个列表跟着跳，反而看不清移动到了哪里。
  useEffect(() => {
    if (!open) return;
    const node = listRef.current?.querySelector<HTMLElement>(`[data-index="${activeIndex}"]`);
    node?.scrollIntoView({block: 'nearest'});
  }, [activeIndex, open, results]);

  const activeId = results.length > 0 ? optionId(activeIndex) : undefined;

  return (
    <>
      <button
        type="button"
        data-slot="command-palette-trigger"
        onClick={() => setOpen(true)}
        aria-label={t('palette.triggerHint')}
        title={t('palette.triggerHint')}
        className="inline-flex h-6 items-center gap-1.5 rounded-full border border-border/60 bg-muted/60 px-2.5 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <Search className="h-3 w-3 shrink-0 opacity-70" />
        <span className="hidden sm:inline">{t('palette.trigger')}</span>
        {mounted && (
          <kbd className="hidden rounded border border-border/60 bg-background/70 px-1 font-sans text-[11px] leading-4 text-muted-foreground sm:inline">
            {isMacPlatform() ? '⌘K' : 'Ctrl K'}
          </kbd>
        )}
      </button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          data-slot="command-palette"
          showCloseButton={false}
          from="top"
          // 靠上而不是居中：面板是「临时借一下视线」的东西，居中会把正文整个
          // 盖住；靠上则下方内容还在，用户扫一眼就知道自己没离开这一页。
          className="top-[10vh] max-w-[560px] translate-y-0 p-0"
          onOpenAutoFocus={(event) => {
            // 默认会聚焦到第一个可聚焦元素。显式指定输入框，免得将来在它前面
            // 加了个按钮就把焦点抢走（那时表现为「打开面板后打字没反应」）。
            event.preventDefault();
            inputRef.current?.focus();
          }}
        >
          {/* Radix 要求弹窗里有标题与说明，否则控制台会报警。
              它们不该出现在界面上（面板的意图一眼就能看出来），所以 sr-only。 */}
          <DialogTitle className="sr-only">{t('palette.title')}</DialogTitle>
          <DialogDescription className="sr-only">{t('palette.desc')}</DialogDescription>

          <div className="flex items-center gap-2 border-b border-border/60 px-4">
            <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
            <input
              ref={inputRef}
              data-slot="command-palette-input"
              role="combobox"
              aria-expanded
              aria-controls="command-palette-listbox"
              aria-activedescendant={activeId}
              aria-autocomplete="list"
              aria-label={t('palette.placeholder')}
              placeholder={t('palette.placeholder')}
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                // 换了查询词，高亮回到第一条：留着上一次的下标会让回车跳到
                // 一个和眼前列表无关的条目上。
                setActiveIndex(0);
              }}
              onKeyDown={handleKeyDown}
              className="h-12 w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            />
          </div>

          {results.length === 0 ? (
            <div data-slot="command-palette-empty" className="px-4 py-10 text-center">
              <div className="text-sm text-foreground">{t('palette.empty')}</div>
              <div className="mt-1 text-xs text-muted-foreground">{t('palette.emptyHint')}</div>
            </div>
          ) : (
            <div
              id="command-palette-listbox"
              ref={listRef}
              role="listbox"
              aria-label={t('palette.title')}
              className="max-h-[min(58vh,420px)] overflow-y-auto py-1"
            >
              {/*
                条目是 `div` 而不是 `button`：键盘由输入框统一处理（标准 combobox
                形态），条目自己不接收焦点。做成可聚焦按钮反而会让 Tab 键在
                十几项之间走一遍，而 Tab 在面板里应该是「离开面板」。
              */}
              {results.map((item, index) => (
                <div
                  key={item.id}
                  id={optionId(index)}
                  data-index={index}
                  data-slot="command-palette-item"
                  // 目标路径挂成 data 属性：验收脚本要核对「搜出来的这一条**是**哪一页」，
                  // 而它的可见文本只有标签与归属（两者都可能与路径不同名）。
                  data-href={item.href}
                  role="option"
                  aria-selected={index === activeIndex}
                  onClick={() => activate(item)}
                  // 鼠标移过就跟着高亮，否则鼠标和键盘会各指一处，
                  // 回车打开的是「另一条」。
                  onMouseMove={() => setActiveIndex(index)}
                  className={cn(
                    'flex cursor-pointer items-center justify-between gap-3 px-4 py-2.5 text-sm',
                    index === activeIndex ? 'bg-muted text-foreground' : 'text-foreground/80',
                  )}
                >
                  <span className="truncate">{item.label}</span>
                  {/* 右侧归属提示：光看名字「用户」不知道是设置页里的那一项，
                      还是账号池里的用户。 */}
                  <span className="shrink-0 text-xs text-muted-foreground">{item.group}</span>
                </div>
              ))}
            </div>
          )}

          <div className="flex items-center justify-between gap-3 border-t border-border/60 px-4 py-2 text-[11px] text-muted-foreground">
            <div className="hidden items-center gap-3 sm:flex">
              <span className="flex items-center gap-1">
                <Kbd>↑</Kbd>
                <Kbd>↓</Kbd>
                {t('palette.hintMove')}
              </span>
              <span className="flex items-center gap-1">
                <Kbd>
                  <CornerDownLeft className="h-2.5 w-2.5" />
                </Kbd>
                {t('palette.hintOpen')}
              </span>
              <span className="flex items-center gap-1">
                <Kbd>esc</Kbd>
                {t('palette.hintClose')}
              </span>
            </div>
            {/* 触屏上没有键盘，这几条提示只会占地方；数量本身还是有用的
                （能一眼看出「是我打错了」还是「确实只有这几条」）。 */}
            <span data-slot="command-palette-count" className="ml-auto tabular-nums">
              {t('palette.results', {n: results.length})}
            </span>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

/** 选项的 DOM id。用**下标**而不是 href：href 里带 `/`，当 id 合法但难读。 */
function optionId(index: number): string {
  return `command-palette-option-${index}`;
}

function isMacPlatform(): boolean {
  if (typeof navigator === 'undefined') return false;
  return /mac|iphone|ipad|ipod/i.test(navigator.userAgent);
}

function Kbd({children}: {children: React.ReactNode}) {
  return (
    <kbd className="rounded border border-border/60 bg-muted/60 px-1 font-sans text-[10px] leading-4">
      {children}
    </kbd>
  );
}

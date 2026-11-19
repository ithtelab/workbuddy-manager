'use client';

import {useEffect, useState} from 'react';
import {useRouter} from 'next/navigation';
import {ArrowUpRight, Megaphone, Sparkles, Star, X} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {systemApi} from '@/lib/api';
import {useI18n} from '@/lib/i18n/provider';
import {RichText} from '@/lib/i18n/rich-text';
import type {Changelog, ChangelogItem} from '@/lib/types';

/**
 * 「本次更新」公告弹窗。
 *
 * 为什么需要它：升级后用户打开面板，看不到这一版改了什么 —— 除非自己去翻
 * 「设置 → 更新日志」。而这一版常常包含**行为变化**（默认值、冷却时长、
 * 路径策略），不知道就会把新行为当成 bug。所以升级后主动弹一次。
 *
 * 三个边界：
 *   · 每个版本**只弹一次**（localStorage 记已读版本）。关掉后不再打扰，
 *     即使同一浏览器里反复刷新；
 *   · 只在**已登录**的面板里弹（挂在主布局，登录页不弹）；
 *   · 拿不到更新日志时不弹 —— 弹窗本身不该成为新的报错来源。
 *
 * 版式（2026-10-28 重做）：**只有更新内容滚动**，标题、致谢、免责声明、底部
 * 按钮都固定。此前是整块内容一起滚，12 条记录把弹窗顶到 774px 高，Star 与免责
 * 声明要滚到底才看得到，底部按钮还会跟着内容跑出视口。
 */
const SEEN_KEY = 'wb.whats-new.seen';

/** 仓库地址：Star 模块与免责声明链接共用，避免两处各写一份而漂移。 */
const REPO_URL = 'https://github.com/ithtelab/workbuddy-manager';
/** 界面上展示的仓库简写（链接文字用，省略协议与末尾斜杠）。 */
const REPO_PATH = 'github.com/ithtelab/workbuddy-manager';

/** 分类色点（与「设置 → 更新日志」同一套语义色） */
const SECTION_DOT: Record<string, string> = {
  安全: 'bg-rose-500',
  新增: 'bg-emerald-500',
  修复: 'bg-amber-500',
  改进: 'bg-sky-500',
  说明: 'bg-muted-foreground/50',
  计划中: 'bg-violet-500',
};

const SECTION_LABEL_KEYS: Record<string, string> = {
  安全: 'changelog.sectionSecurity',
  新增: 'changelog.sectionAdded',
  修复: 'changelog.sectionFixed',
  改进: 'changelog.sectionImproved',
  说明: 'changelog.sectionNotes',
  计划中: 'changelog.sectionPlanned',
};

function readSeen(): string {
  try {
    return window.localStorage.getItem(SEEN_KEY) || '';
  } catch {
    return '';
  }
}

function writeSeen(version: string): void {
  try {
    window.localStorage.setItem(SEEN_KEY, version);
  } catch {
    /* 写不进去只影响下次仍会弹，不影响阅读 */
  }
}

/** 行内 `**加粗**` / `` `代码` ``，与更新日志页同一套轻量渲染 */
function InlineText({text}: {text: string}) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return (
    <>
      {parts.map((part, i) => {
        if (part.startsWith('**') && part.endsWith('**')) {
          return <strong key={i} className="font-medium text-foreground">{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith('`') && part.endsWith('`')) {
          return (
            <code key={i} className="rounded bg-muted px-1 py-0.5 font-mono text-[11px]">
              {part.slice(1, -1)}
            </code>
          );
        }
        return <span key={i}>{part}</span>;
      })}
    </>
  );
}

function ItemList({items}: {items: ChangelogItem[]}) {
  return (
    <ul className="space-y-2">
      {items.map((it, i) => (
        <li
          key={i}
          className={
            'text-[12px] leading-[1.75] text-muted-foreground break-words'
            + (it.level > 0 ? ' mt-1.5 ml-3 border-l border-border pl-3' : '')
          }
        >
          <InlineText text={it.text} />
        </li>
      ))}
    </ul>
  );
}

/**
 * 分区标题：色点 + 名称 + 一条通到右边的细线。
 *
 * 以前是胶囊标签，12 条更新记录排下来像一排按钮，视觉噪音大；色点信息量一样、
 * 安静得多。
 */
function SectionHeader({title}: {title: string}) {
  const {t} = useI18n();
  const label = SECTION_LABEL_KEYS[title] ? t(SECTION_LABEL_KEYS[title]) : title;
  return (
    <div className="mb-2 flex items-center gap-2">
      <span className={'size-1.5 shrink-0 rounded-full ' + (SECTION_DOT[title] || 'bg-muted-foreground/50')} />
      <span className="shrink-0 text-[11px] font-medium text-foreground">{label}</span>
      <span className="h-px flex-1 bg-border" />
    </div>
  );
}

export function WhatsNewDialog() {
  const {t} = useI18n();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<Changelog | null>(null);

  useEffect(() => {
    let alive = true;
    systemApi.changelog()
      .then((res) => {
        if (!alive || !res.available || !res.current) return;
        // 已读过这一版就不再弹；版本号变化即视为「刚升级」。
        if (readSeen() === res.current) return;
        setData(res);
        setOpen(true);
      })
      .catch(() => { /* 拿不到就不弹，不当成错误打扰用户 */ });
    return () => { alive = false; };
  }, []);

  function dismiss() {
    if (data?.current) writeSeen(data.current);
    setOpen(false);
  }

  function openFullChangelog() {
    dismiss();
    router.push('/settings/changelog');
  }

  // 只展示**当前版本**那一节：历史版本用户自己去「更新日志」看。
  //
  // 版本号一律按去掉 `v` 前缀的形态处理，两个原因：
  //   1. `/api/system/changelog` 的 `current` 是 `v2.11.1`（来自 `.version`），
  //      而 CHANGELOG 的段标题是 `[2.11.1]`。严格相等会让 find 永远落空 ——
  //      弹窗照常出现，但一条更新内容都不显示。「更新日志」页也是按这个口径
  //      归一的，这里跟它保持一致。
  //   2. 标题模板只留版本号、`v` 由界面自己加：直接填 `v2.11.1` 会显示成
  //      `本版更新：vv2.11.1`（截图里就是这个样子）。
  const norm = (v: string) => v.replace(/^v/i, '').trim();
  const version = norm(data?.current || '');
  const current = data?.versions?.find(
    (v) => !v.unreleased && norm(v.version) === version);
  const sections = current?.sections ?? [];
  const itemCount = sections.reduce((n, s) => n + s.items.length, 0);

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) dismiss(); }}>
      <DialogContent className="flex max-h-[min(600px,calc(100dvh-4rem))] max-w-[540px] flex-col gap-0 overflow-hidden p-0">
        <DialogHeader className="gap-1.5 border-b px-5 py-3.5">
          <DialogTitle className="flex items-center gap-2 pr-7 text-[15px]">
            <span className="grid size-6 shrink-0 place-items-center rounded-full bg-primary/10">
              <Sparkles className="size-3.5 text-primary" />
            </span>
            <span className="min-w-0 truncate">
              {version ? t('whatsNew.titlePlain') : t('whatsNew.titleFallback')}
            </span>
            {version && (
              <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 font-mono text-[10px] font-normal text-primary">
                v{version}
              </span>
            )}
            {itemCount > 0 && (
              <span className="ml-auto hidden shrink-0 text-[10px] font-normal text-muted-foreground sm:inline">
                {t('whatsNew.itemCount', {n: itemCount})}
              </span>
            )}
          </DialogTitle>
          <DialogDescription className="text-[11.5px] leading-relaxed">
            {t('whatsNew.thanks')}
          </DialogDescription>
        </DialogHeader>

        {/* 只有这一段滚动：条目再多也不会把下面的免责声明与按钮挤出视口。
            这里**刻意不用** DialogBody（Radix ScrollArea）：它的 Viewport 用
            `size-full` 取高度，在 flex 列里算出的是内容高度（实测 1357px 而非
            320px），于是整段内容压到页脚上。普通 div 在 flex 里的高度是确定的。 */}
        {sections.length > 0 && (
          <div className="relative flex min-h-0 flex-1 flex-col">
            {/* 用 flex 链而不是 h-full：百分比高度在 flex 子项上没有确定基准，
                实测会退回内容高度（1357px），内容再次压到页脚上。 */}
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain [scrollbar-width:thin]">
              <div className="space-y-3.5 px-5 py-3.5">
                {sections.map((section) => (
                  <div key={section.title}>
                    <SectionHeader title={section.title} />
                    <ItemList items={section.items} />
                  </div>
                ))}
              </div>
            </div>
            {/* 底部渐隐：正文被裁断时提示「下面还有」，否则半句截断看起来像渲染坏了 */}
            <div className="pointer-events-none absolute inset-x-0 bottom-0 h-6 bg-gradient-to-t from-background to-transparent" />
          </div>
        )}

        {/* 固定区：Star 与使用边界各压成一行，不再各占一个色块 */}
        <div className="space-y-2 border-t px-5 py-3">
          <div className="flex flex-wrap items-center gap-2.5 rounded-lg bg-sky-500/[0.08] px-3 py-2">
            <Star className="size-3.5 shrink-0 fill-sky-500 text-sky-500" />
            <p className="min-w-0 flex-1 text-[11px] leading-snug text-sky-800 dark:text-sky-200">
              <span className="font-medium">{t('whatsNew.starTitle')}</span>
              <span className="mx-1 opacity-50">·</span>
              {t('whatsNew.starBody')}
            </p>
            {/* Star 落点是仓库主页，不是 stargazers 列表页：GitHub 已把该列表
                收窄为管理员与协作者可见，其他人（含已登录的普通账号）打开一律 404。
                Star 按钮本来就在仓库页右上角，落到主页即可点，对谁都有效。 */}
            <a
              href={REPO_URL}
              target="_blank"
              rel="noreferrer"
              className="inline-flex shrink-0 items-center gap-1 rounded-full bg-sky-600 px-2.5 py-1 text-[10.5px] font-medium text-white transition-opacity hover:opacity-90 dark:bg-sky-500"
            >
              <Star className="size-2.5 fill-current" />
              {t('whatsNew.starLink')}
              <ArrowUpRight className="size-2.5" />
            </a>
          </div>

          {/* 使用边界与免责声明：每次升级都重申一次，而不是只在 README 里躺着。
              文案与 README 首屏一致，避免两处说法漂移。

              窄屏只显示一句摘要 + 全文链接：这段每次内容都一样，在 390px 宽的
              屏幕上展开会吃掉一半正文区，而正文才是用户这次真正要看的。 */}
          <div className="flex items-start gap-2.5 rounded-lg bg-amber-500/[0.08] px-3 py-2">
            <Megaphone className="mt-0.5 size-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
            <p className="min-w-0 flex-1 text-[11px] leading-[1.5] text-amber-800 dark:text-amber-200">
              <span className="font-medium text-amber-700 dark:text-amber-300">
                {t('whatsNew.boundaryTitle')}：
              </span>
              {/* 窄屏只给一句摘要：这段每次内容都一样，390px 宽时展开会吃掉
                  一半正文区，而当次更新内容才是用户真正要看的。 */}
              <span className="sm:hidden">
                <RichText text={t('whatsNew.boundaryShort')} />
              </span>
              <span className="hidden sm:inline">
                <RichText text={t('whatsNew.boundary')} />
              </span>{' '}
              <a
                href={`${REPO_URL}#使用边界与免责声明`}
                target="_blank"
                rel="noreferrer"
                className="whitespace-nowrap underline underline-offset-2 hover:text-amber-900 dark:hover:text-amber-100"
              >
                {t('whatsNew.disclaimerLink')}
              </a>
            </p>
          </div>
        </div>

        <DialogFooter className="items-center border-t px-5 py-3">
          <a
            href={REPO_URL}
            target="_blank"
            rel="noreferrer"
            className="mr-auto hidden items-center gap-1 font-mono text-[10px] text-muted-foreground underline-offset-2 hover:underline sm:inline-flex"
          >
            {REPO_PATH}
            <ArrowUpRight className="size-2.5" />
          </a>
          <Button variant="ghost" size="sm" className="rounded-full" onClick={dismiss}>
            <X className="mr-1 size-3.5" />
            {t('common.close')}
          </Button>
          <Button size="sm" className="rounded-full" onClick={openFullChangelog}>
            {t('whatsNew.fullChangelog')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

'use client';

import Link from 'next/link';
import type {ReactNode} from 'react';

import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';

export type SectionTabItem = {
  /** 站内绝对路径。`Link` 自己处理 basePath 与 export 模式的尾斜杠 */
  href: string;
  label: string;
  icon?: ReactNode;
};

/**
 * 页面内的二级导航（批次 4）。
 *
 * **它和 `ui/tabs.tsx` 的分工**：`Tabs` 是「同一个页面里的若干块内容」，切换靠
 * 组件内部状态，地址栏不动；这里每一项都是**一条真实路由**，切换是一次导航——
 * 于是可以分享、可以收藏、可以按后退键回到上一个 Tab。
 *
 * 为什么用 `<Link>` 而不是给 `TabsTrigger` 挂 `onValueChange` 里 `router.push`：
 * 后者会在「点下去」和「路由真的变了」之间留一帧，两个 Tab 同时是 active 的；
 * 而链接本身就是目标，浏览器与读屏软件都能直接看到「这个 Tab 通向哪里」。
 *
 * ⚠️ 视觉上刻意与 `ui/tabs.tsx` 的 `pill` 变体保持一致（胶囊底 + 白色滑块）。
 * 两处若各写一套，同一个应用里会出现两种「看起来一样、行为不一样」的 Tab，
 * 那正是本批次要消灭的那类不一致。
 */
export function SectionTabs({
  items,
  active,
  className,
}: {
  items: SectionTabItem[];
  /** 当前项（与某项的 href 相等）。由调用方给，不读路由——见下方说明 */
  active: string;
  className?: string;
}) {
  const t = useT();

  return (
    <nav
      data-slot="section-tabs"
      aria-label={t('nav.sectionTabs')}
      className={cn(
          'inline-flex h-auto w-fit items-center justify-center gap-1 rounded-full bg-muted p-1 text-muted-foreground',
          className,
      )}
    >
      {items.map((item) => {
        const isActive = item.href === active;
        return (
          <Link
            key={item.href}
            href={item.href}
            data-slot="section-tab"
            data-active={isActive ? 'true' : 'false'}
            // 读屏软件据此播报「当前页」，而 `data-active` 只给验收脚本看
            aria-current={isActive ? 'page' : undefined}
            className={cn(
                'inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-full px-3 py-1.5 text-xs font-medium transition-[background-color,color,box-shadow]',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
                isActive ?
                  'bg-white text-foreground shadow-sm dark:bg-white/[0.08]' :
                  'hover:text-foreground',
            )}
          >
            {item.icon}
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

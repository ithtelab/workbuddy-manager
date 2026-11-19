import {Skeleton} from '@/components/ui/skeleton';
import {cn} from '@/lib/utils';

/**
 * 骨架里的一根「条」。
 *
 * 为什么不直接用 `Skeleton`：它自带 `bg-accent`，而各页的卡片、表格块用的是
 * `bg-muted` —— 这两个颜色在 globals.css 里**明暗两套主题下都是同一个字面量**
 * （浅色同为 oklch(0.967 0.001 286.375)，深色同为 oklch(0.274 0.006 286.033)），
 * 直接放上去等于画了看不见的条。改用前景色的低透明度：浅色下压暗、深色下提亮，
 * 两种主题都能看出形状。
 *
 * 反过来，落在**页面背景**（不是 `bg-muted` 块）上的骨架不需要这一层，
 * 直接用 `Skeleton` 就好——在浅色背景上 `bg-accent` 是看得见的。
 */
export function SkeletonBar({className}: {className?: string}) {
  return <Skeleton className={cn('bg-foreground/10', className)} />;
}

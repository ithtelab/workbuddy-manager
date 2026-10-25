'use client';

import {motion} from 'motion/react';
import type {ReactNode} from 'react';

export function PageHeader({
  title,
  description,
  actions,
  stacked,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  /**
   * 上下布局：标题与描述占满一整行，actions 另起一行一字排开。
   * 给按钮多的页面用（如账号页 8 个操作按钮）——默认的「标题左、按钮右」
   * 两侧挤压会让按钮区折行、尾部按钮孤零零掉到第二行。
   */
  stacked?: boolean;
}) {
  return (
    <motion.div
      initial={{opacity: 0, y: -6}}
      animate={{opacity: 1, y: 0}}
      transition={{duration: 0.3}}
      className={
        stacked
          ? 'flex flex-col gap-3'
          : 'flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between'
      }
    >
      <div className="space-y-1">
        <h1 className="text-lg font-semibold tracking-[-0.01em]">{title}</h1>
        {description && (
          <p className="text-xs text-muted-foreground">{description}</p>
        )}
      </div>
      {actions && (
        <div
          className={
            'flex flex-wrap items-center gap-2' + (stacked ? ' justify-end' : '')
          }
        >
          {actions}
        </div>
      )}
    </motion.div>
  );
}

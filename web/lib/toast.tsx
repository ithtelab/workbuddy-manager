'use client';

import {motion} from 'motion/react';
import {CircleCheck, CircleX, Info, TriangleAlert, X} from 'lucide-react';
import {toast} from 'sonner';
import {cn} from '@/lib/utils';
import {t} from '@/lib/i18n';

/**
 * 统一提示：自定义渲染的卡片式通知。
 *
 * 为什么用 toast.custom 而不是默认样式：
 * sonner 默认会给 [data-sonner-toast] 加内联背景/边框，与自定义样式冲突，
 * 且只能拿到字符串图标。自定义渲染可以完全控制结构，并用 motion 做弹簧入场。
 */
type Variant = 'success' | 'error' | 'warning' | 'info';

const META: Record<Variant, {Icon: typeof Info; accent: string}> = {
  success: {Icon: CircleCheck, accent: '#10b981'},
  error: {Icon: CircleX, accent: '#ef4444'},
  warning: {Icon: TriangleAlert, accent: '#f59e0b'},
  info: {Icon: Info, accent: '#3b82f6'},
};

function ToastCard({
  id,
  variant,
  message,
  description,
  duration,
}: {
  id: string | number;
  variant: Variant;
  message: string;
  description?: string;
  duration: number;
}) {
  const {Icon, accent} = META[variant];

  return (
    <motion.div
      data-wb-toast=""
      initial={{opacity: 0, y: -16, scale: 0.94}}
      animate={{opacity: 1, y: 0, scale: 1}}
      transition={{type: 'spring', stiffness: 420, damping: 32, mass: 0.7}}
      className={cn(
        'group pointer-events-auto relative flex w-full items-start gap-3',
        'overflow-hidden rounded-[16px] border border-border/60 bg-popover/95 px-3.5 py-3',
        'backdrop-blur-md',
        'shadow-[0_16px_40px_-18px_rgba(15,23,42,0.30),0_2px_8px_-4px_rgba(15,23,42,0.10)]',
        'dark:border-border/70 dark:shadow-[0_18px_44px_-18px_rgba(0,0,0,0.7)]',
      )}
    >
      {/* 语义色图标：淡色圆底 + 同色图标，比 emoji 更克制清晰 */}
      <span
        className="mt-px grid size-7 shrink-0 place-items-center rounded-full"
        style={{background: `color-mix(in srgb, ${accent} 14%, transparent)`, color: accent}}
      >
        <Icon className="size-[15px]" strokeWidth={2.4} />
      </span>

      <div className="min-w-0 flex-1 pt-[3px]">
        <p className="text-[13px] font-semibold leading-[18px] tracking-[-0.01em] text-foreground">
          {message}
        </p>
        {description && (
          <p className="mt-0.5 text-[11.5px] leading-4 text-muted-foreground">{description}</p>
        )}
      </div>

      {/* 关闭按钮：悬停浮现，不干扰阅读 */}
      <button
        type="button"
        aria-label={t('common.close')}
        onClick={() => toast.dismiss(id)}
        className="-mr-1 -mt-0.5 grid size-6 shrink-0 place-items-center rounded-full text-muted-foreground opacity-0 transition-all hover:bg-muted hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100"
      >
        <X className="size-3.5" />
      </button>

      {/* 底部剩余时间进度：细线，颜色跟随语义 */}
      <span
        aria-hidden
        className="absolute inset-x-0 bottom-0 h-[2px] origin-left"
        style={{
          background: accent,
          opacity: 0.5,
          animation: `wb-toast-bar ${duration}ms linear forwards`,
        }}
      />
    </motion.div>
  );
}

function show(variant: Variant, message: string, description?: string, duration = 3600) {
  return toast.custom(
    (id) => (
      <ToastCard
        id={id}
        variant={variant}
        message={message}
        description={description}
        duration={duration}
      />
    ),
    {duration},
  );
}

export const notify = {
  /** 操作成功，例如保存完成、授权成功 */
  ok: (message: string, description?: string) => show('success', message, description),
  /** 失败，需要用户处理 */
  err: (message: string, description?: string) => show('error', message, description, 5200),
  /** 需要注意但不阻断，例如即将过期、配额将满 */
  warn: (message: string, description?: string) => show('warning', message, description, 5200),
  /** 中性提示 */
  info: (message: string, description?: string) => show('info', message, description),
  /**
   * 关掉某条提示（用上面几个方法返回的 id）。
   *
   * 用途是「进行中 → 结果」的替换：先弹一条「正在拉取」，拿到结果后关掉它再弹
   * 成功/失败，否则界面上会同时挂着「已经拉完了」和「正在拉取」两条相反的提示。
   * 直接把 sonner 暴露给页面也可以，但那等于各页自己拼提示样式，这里收一道口子。
   */
  dismiss: (id: string | number) => toast.dismiss(id),
};

export {toast};

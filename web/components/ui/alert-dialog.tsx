'use client';

import * as React from 'react';
import * as AlertDialogPrimitive from '@radix-ui/react-alert-dialog';

import {cn} from '@/lib/utils';
import {buttonVariants} from '@/components/ui/button';
import {ScrollArea} from '@/components/ui/scroll-area';

function AlertDialog({
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Root>) {
  return <AlertDialogPrimitive.Root data-slot="alert-dialog" {...props} />;
}

function AlertDialogTrigger({
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Trigger>) {
  return (
    <AlertDialogPrimitive.Trigger data-slot="alert-dialog-trigger" {...props} />
  );
}

function AlertDialogPortal({
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Portal>) {
  return (
    <AlertDialogPrimitive.Portal data-slot="alert-dialog-portal" {...props} />
  );
}

function AlertDialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Overlay>) {
  return (
    <AlertDialogPrimitive.Overlay
      data-slot="alert-dialog-overlay"
      className={cn(
          'data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 fixed inset-0 z-50 bg-black/16 backdrop-blur-[2px] dark:bg-black/55',
          className,
      )}
      {...props}
    />
  );
}

function AlertDialogContent({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Content>) {
  return (
    <AlertDialogPortal>
      <AlertDialogOverlay />
      <AlertDialogPrimitive.Content
        data-slot="alert-dialog-content"
        className={cn(
            // 短屏（横屏 640×360、小屏 360×640）下，长说明会把底部按钮挤出视口，
            // 而内容区是 overflow-hidden —— 按钮直接不可达。给整框一个视口上限。
            'max-h-[calc(100dvh-2rem)]',
            // 不透明背景 + 单一边框：半透明背景会让遮罩透出、与不透明的
            // header/footer 形成明暗分界，叠加 ring 还会多出一圈描边。
            'bg-background data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 fixed top-[50%] left-[50%] z-50 grid w-full max-w-[calc(100%-2rem)] translate-x-[-50%] translate-y-[-50%] gap-0 overflow-hidden rounded-[24px] border border-border/60 p-0 shadow-[0_24px_60px_rgba(15,23,42,0.10)] duration-200 sm:max-w-lg dark:border-border/70 dark:bg-background dark:shadow-[0_24px_60px_rgba(0,0,0,0.42)]',
            className,
        )}
        {...props}
      />
    </AlertDialogPortal>
  );
}

function AlertDialogHeader({
  className,
  ...props
}: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="alert-dialog-header"
      className={cn('flex flex-col gap-1.5 px-6 py-4 text-left', className)}
      {...props}
    />
  );
}

function AlertDialogFooter({
  className,
  ...props
}: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="alert-dialog-footer"
      className={cn(
          'flex flex-row justify-end gap-2 px-6 py-4',
          className,
      )}
      {...props}
    />
  );
}

function AlertDialogBody({className, children, ...props}: React.ComponentProps<typeof ScrollArea>) {
  return (
    <ScrollArea
      data-slot="alert-dialog-body"
      className={cn('min-h-0 flex-1', className)}
      {...props}
    >
      {children}
    </ScrollArea>
  );
}

function AlertDialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Title>) {
  return (
    <AlertDialogPrimitive.Title
      data-slot="alert-dialog-title"
      className={cn(
          // 标题常插值用户数据（账号名、密钥名），按「一定会折行」来设计：
          // - leading-snug：leading-none 下折行的中文上下几乎贴在一起；
          // - wrap-anywhere 而非 break-words：二者都允许在长串内断开，但只有 anywhere
          //   计入最小内容宽度。否则邮箱这类无空格长串会把弹窗的 grid 列撑宽，
          //   溢出部分被 overflow-hidden 裁掉，靠右的确认键跟着看不见。
          'text-lg font-semibold leading-snug tracking-tight wrap-anywhere',
          className,
      )}
      {...props}
    />
  );
}

function AlertDialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Description>) {
  return (
    <AlertDialogPrimitive.Description
      data-slot="alert-dialog-description"
      className={cn(
          // 说明里同样会插值长名称，wrap-anywhere 的原因见 AlertDialogTitle
          'text-xs text-muted-foreground pt-1 wrap-anywhere',
          className,
      )}
      {...props}
    />
  );
}

function AlertDialogAction({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Action>) {
  return (
    <AlertDialogPrimitive.Action
      className={cn(buttonVariants(), 'h-8 rounded-full px-3 text-xs shadow-none', className)}
      {...props}
    />
  );
}

function AlertDialogCancel({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Cancel>) {
  return (
    <AlertDialogPrimitive.Cancel
      className={cn(buttonVariants({variant: 'ghost'}), 'h-8 rounded-full px-3 text-xs shadow-none', className)}
      {...props}
    />
  );
}

export {
  AlertDialog,
  AlertDialogPortal,
  AlertDialogOverlay,
  AlertDialogTrigger,
  AlertDialogContent,
  AlertDialogHeader,
  AlertDialogFooter,
  AlertDialogBody,
  AlertDialogTitle,
  AlertDialogDescription,
  AlertDialogAction,
  AlertDialogCancel,
};

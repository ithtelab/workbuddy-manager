'use client';

import {useState, type ReactNode} from 'react';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog';
import {useT} from '@/lib/i18n/provider';
import {errText} from '@/lib/api';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';

export function ConfirmDialog({
  trigger,
  title,
  description,
  content,
  contentClassName,
  confirmText,
  destructive = false,
  confirmDisabled = false,
  onConfirm,
  onOpenChange,
}: {
  trigger: ReactNode;
  title: string;
  description?: string;
  /**
   * 插在说明与按钮之间的额外内容，用于「确认前还要填点什么」的场景
   * （目前只有设置页的重置密码：要输新密码）。
   *
   * 为什么做成插槽而不是给 ConfirmDialog 加业务 props：弹窗本身只负责
   * 「问一句、等一个答复」，把密码框的校验规则塞进来会让它变成一个懂业务的
   * 组件，下次再要别的输入就得再加一组 props。
   */
  content?: ReactNode;
  /** 覆盖弹窗内容区域样式，供特定确认场景匹配对应业务弹窗尺寸。 */
  contentClassName?: string;
  confirmText?: string;
  destructive?: boolean;
  /** 条件不满足时锁住确认键（例如密码不足 12 位），比点完再报错少一次往返 */
  confirmDisabled?: boolean;
  onConfirm: () => void | Promise<void>;
  /**
   * 开关变化时通知调用方。给 `content` 里的受控输入用：弹窗关掉后要清空，
   * 否则下次打开会带着上一次输的密码（同一个弹窗按用户名复用时会串）。
   */
  onOpenChange?: (open: boolean) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        onOpenChange?.(next);
      }}
    >
      <AlertDialogTrigger asChild>{trigger}</AlertDialogTrigger>
      <AlertDialogContent className={cn(contentClassName)}>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          {description && <AlertDialogDescription>{description}</AlertDialogDescription>}
        </AlertDialogHeader>
        {content && <div className="px-6">{content}</div>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>{t('common.cancel')}</AlertDialogCancel>
          <AlertDialogAction
            disabled={busy || confirmDisabled}
            className={destructive ? 'bg-destructive text-white hover:bg-destructive/90' : ''}
            onClick={async (e) => {
              e.preventDefault();
              setBusy(true);
              try {
                await onConfirm();
                setOpen(false);
              } catch (err) {
                // 以前只有 finally：业务失败会变成未处理的 rejection，弹窗不关、
                // 界面毫无反应 —— 用户以为没点上，反复点。这里当场说清原因并
                // 保持弹窗打开，让人能改完重试。
                notify.err(errText(err));
              } finally {
                setBusy(false);
              }
            }}
          >
            {confirmText ?? t('common.confirm')}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

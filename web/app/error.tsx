'use client';

import {LoadError} from '@/components/common/states/LoadError';
import {useT} from '@/lib/i18n/provider';

/**
 * 根级兜底：`(main)/error.tsx` 兜不到的那一层。
 *
 * React 的错误边界只能接住**子树**的异常——`(main)/error.tsx` 接不住
 * `(main)/layout.tsx` 自己抛的错。而 `(main)/layout.tsx` 恰恰挂着底栏、版本
 * 切换与 RealmProvider，是「整页白屏」最难受的一处。`(auth)/login` 与公开的
 * `/claim` 也不在 `(main)` 之下，同样需要这一层。
 *
 * 这里已经在 `app/layout.tsx` 之内，所以 ThemeProvider / I18nProvider 都还在，
 * 语言与主题照常生效；不在 `(main)` 之下则意味着没有底栏，因此提示里必须留一句
 * 「回首页」以外的出路——`reset` 之外再给一次整页刷新由用户自己决定。
 */
export default function RootError({
  reset,
}: {
  /** 声明但不取用：React 自己已经打了完整堆栈，这里重复打没有意义 */
  error: Error & {digest?: string};
  reset: () => void;
}) {
  const t = useT();

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-xl">
        <LoadError
          variant="page"
          message={t('error.crashTitle')}
          hint={t('error.crashHint')}
          onRetry={reset}
        />
      </div>
    </div>
  );
}

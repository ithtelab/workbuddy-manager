'use client';

import {LoadError} from '@/components/common/states/LoadError';
import {useT} from '@/lib/i18n/provider';

/**
 * 页面渲染期异常的兜底（P0-3）。
 *
 * 为什么必须有：在此之前 `app/` 下只有 `not-found.tsx`，任何一处渲染抛错
 * （最常见的是对还没到位的取数结果直接取属性）都会让整页白屏——用户看到的
 * 是一片空白，既没有解释、也没有出路，只能自己猜是不是网络断了。
 *
 * 为什么复用 LoadError 而不是新做一个页面：两者说的是同一件事——「这份内容
 * 没出来」——所以视觉语言该一致。区别在来源，文案因此分开写：`LoadError` 的
 * 默认文案是「请求失败」，重试往往就好；这里是**代码抛错**，重试大概率还是错，
 * 用户真正该做的是把控制台的报错交出来。这两种「下一步动作」不同，不能共用
 * 一句提示。
 *
 * 为什么放在 `(main)` 而不是 `app/` 根：这样它渲染在 `(main)/layout.tsx` 内部，
 * 底栏还在，用户可以直接切去别的页面，而不是被困在一个孤岛页上。代价是它
 * 兜不住 `(main)/layout.tsx` 自身的异常——那由 `app/error.tsx` 兜。
 */
export default function MainError({
  reset,
}: {
  /**
   * 声明但不取用：React 自己已经把完整堆栈打到控制台了（生产构建也会打），
   * 这里再 console.error 一遍只会重复。类型保留是为了让签名与 Next 的约定
   * 一致，改的人一眼能看到有哪些可用。
   */
  error: Error & {digest?: string};
  reset: () => void;
}) {
  const t = useT();

  return (
    <LoadError
      variant="page"
      message={t('error.crashTitle')}
      hint={t('error.crashHint')}
      onRetry={reset}
    />
  );
}

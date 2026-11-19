'use client';

import {memo, useEffect} from 'react';
import {useRouter} from 'next/navigation';
import {ManagementBar} from '@/components/common/layout/ManagementBar';
import {CommandPalette} from '@/components/common/layout/CommandPalette';
import {WhatsNewDialog} from '@/components/common/layout/WhatsNewDialog';
import {LanguageToggle} from '@/components/common/layout/LanguageToggle';
import {RealmToggle} from '@/components/common/layout/RealmToggle';
import {RealmProvider} from '@/lib/realm-context';
import {useAuth} from '@/lib/auth-context';

const MemoizedManagementBar = memo(ManagementBar);

export default function MainLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const {me, loading} = useAuth();
  const router = useRouter();

  // 仅在确认未登录时跳转；不阻塞内容渲染，避免每次切页闪一下
  useEffect(() => {
    if (!loading && !me) router.replace('/login');
  }, [loading, me, router]);

  return (
    <RealmProvider>
      <div className="min-h-screen flex flex-col">
        <MemoizedManagementBar />
        <div className="flex flex-1 flex-col">
          <div className="@container/main flex flex-1 flex-col gap-2">
            <div className="flex min-h-0 flex-1 flex-col px-4 pt-12 py-8 sm:px-6 md:px-8 lg:px-12">
              <div className="mx-auto flex min-h-0 w-full max-w-7xl flex-1 flex-col gap-4 pb-24 md:gap-6">
                {/*
                  版本切换固定在右上角：底栏是照 LDC 原样保留的，控件不往那里加。
                  移动端只显示图标（compact），避免窄屏被它占掉一行。

                  命令面板（⌘K）挂在同一行，且放在最左：它是**入口**，语言与版本是
                  **状态**，入口排在状态前面才符合扫读顺序。放在布局里而不是某一页里，
                  是因为它必须在每一页都能按得出来（快捷键监听也挂在它自己身上）。
                */}
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <CommandPalette />
                  <LanguageToggle />
                  <RealmToggle />
                </div>
                {children}
              </div>
            </div>
          </div>
        </div>
      </div>
      {/* 升级后的「本次更新」公告：每个版本只弹一次（按 localStorage 记录的版本号）。 */}
      <WhatsNewDialog />
    </RealmProvider>
  );
}

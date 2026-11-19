/**
 * `/settings` —— 规范地址是第一个 Tab 的子路由，这里把地址换过去。
 *
 * 内容不在这里渲染：外壳（标题 + 二级导航 + 当前 Tab 的内容）挂在同级的
 * `layout.tsx` 上，它不随子路由重挂载，设置页的表单状态才活得下来。详见
 * `layout.tsx` 顶部的说明。
 */
'use client';

import {useEffect} from 'react';
import {useRouter} from 'next/navigation';

import {DEFAULT_SETTINGS_TAB, settingsTabHref} from '@/lib/settings-tabs';

export default function SettingsIndexRedirect() {
  const router = useRouter();

  useEffect(() => {
    // replace 而不是 push：`/settings` 与 `/settings/upstream` 是同一屏内容，
    // 留两条历史记录会让「后退」看起来点了没反应。
    router.replace(settingsTabHref(DEFAULT_SETTINGS_TAB));
  }, [router]);

  return null;
}

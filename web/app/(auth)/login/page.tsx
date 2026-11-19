'use client';

import {useEffect, useState} from 'react';
import {useRouter} from 'next/navigation';
import {motion} from 'motion/react';
import {Bot, Loader2, LogIn} from 'lucide-react';
import {errText} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {LanguageToggle} from '@/components/common/layout/LanguageToggle';
import {useT} from '@/lib/i18n/provider';

export default function LoginPage() {
  const {me, loading, login} = useAuth();
  const router = useRouter();
  const t = useT();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  // 会话过期跳转（`api.ts` 的 401 拦截器会带 `?expired=1`）。读
  // `window.location.search` 而不是 `useSearchParams`：静态导出下后者必须包一层
  // Suspense，为一行提示不值得改整页结构。
  const [expired, setExpired] = useState(false);
  // 静态导出的页在 JS 到位前是原生表单；此时提交会走浏览器默认 GET，把
  // ?username=…&password=… 写进地址栏（浏览记录 / 反代日志 / Referer）。
  // 挂载前禁用提交按钮，并给 form 加 method="post" 作为第二道保险。
  const [ready, setReady] = useState(false);

  useEffect(() => setReady(true), []);

  useEffect(() => {
    if (!loading && me) router.replace('/dashboard');
  }, [loading, me, router]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    setExpired(new URLSearchParams(window.location.search).get('expired') === '1');
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError('');

    // 提交值以**表单元素的实际值**为准，而不是 React state。
    //
    // 为什么必须这样：浏览器自动填充是**直接改写 DOM 的 value、不派发 `input`
    // 事件**，而受控组件的 `onChange` 依赖 `input` 事件——于是 state 始终是空串，
    // 提交出去的就是空密码。界面显示有值（来自 DOM）、服务端收到的却是空值，
    // 用户只看到「用户名或密码错误」，会以为是自己记错了密码，反复重试直到
    // 触发登录锁定（实测复现：DOM 里 13 位密码，请求体是
    // `{"username":"","password":""}`）。
    //
    // 从 DOM 读值同时覆盖三种情况：手输（state 与 DOM 一致）、自动填充（只有
    // DOM 有值）、密码管理器注入——都不依赖事件是否派发。
    const form = e.currentTarget as HTMLFormElement;
    const domUser = (form.elements.namedItem('username') as HTMLInputElement | null)?.value ?? '';
    const domPass = (form.elements.namedItem('password') as HTMLInputElement | null)?.value ?? '';
    const finalUser = (domUser || username).trim();
    const finalPass = domPass || password;

    // 读不到值：给一条**指向真正原因**的提示。落到后端只会得到
    // 「用户名或密码错误」，把「填充没被识别」误报成「密码错」，用户无从下手。
    if (!finalUser || !finalPass) {
      setError(t('login.emptySubmit'));
      return;
    }

    setBusy(true);
    try {
      await login(finalUser, finalPass);
      router.replace('/dashboard');
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="bg-background relative flex min-h-svh flex-col items-center justify-center gap-6 p-6 md:p-10">
      {/* 未登录也要能换语言：看不懂当前语言的人得先能切过去 */}
      <div className="absolute right-4 top-4">
        <LanguageToggle />
      </div>
      <motion.div
        initial={{opacity: 0, y: 12}}
        animate={{opacity: 1, y: 0}}
        transition={{duration: 0.35}}
        className="w-full max-w-sm"
      >
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <div className="grid h-11 w-11 place-items-center rounded-2xl bg-primary text-primary-foreground">
            <Bot className="h-5 w-5" />
          </div>
          <div className="space-y-1">
            <h1 className="text-lg font-semibold tracking-[-0.01em]">WorkBuddy Manager</h1>
            <p className="text-xs text-muted-foreground">
              {t('login.subtitle')}
            </p>
          </div>
        </div>

        <form onSubmit={submit} method="post" className="rounded-[24px] bg-muted p-5">
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="username" className="text-[11px] text-muted-foreground">
                {t('login.username')}
              </Label>
              <Input
                id="username"
                name="username"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder={t('login.usernamePlaceholder')}
                className="bg-background"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password" className="text-[11px] text-muted-foreground">
                {t('login.password')}
              </Label>
              <Input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t('login.passwordPlaceholder')}
                className="bg-background"
              />
            </div>

            {expired && !error && (
              <p className="text-xs text-amber-600 dark:text-amber-500">{t('login.expiredHint')}</p>
            )}

            {error && <p className="text-xs text-red-500">{error}</p>}

            {/* 登录失败才提示初始密码在哪：issue #179 —— 随机密码只打印一次，
                日志被轮转 / 安装脚本没抓到 / 用户翻不过日志，都会让人彻底卡在
                登录页外面。指路到那份落盘文件是唯一能自救的办法。成功登录就
                永远看不到这行，所以不打扰老用户。 */}
            {error && (
              <p className="text-xs text-muted-foreground">{t('login.initialPwdHint')}</p>
            )}

            <Button type="submit" disabled={busy || !ready} className="w-full rounded-full">
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogIn className="h-4 w-4" />}
              {t('login.submit')}
            </Button>
          </div>
        </form>

        <p className="mt-6 text-center text-[11px] text-muted-foreground">
          {t('login.legal')}
        </p>
      </motion.div>
    </div>
  );
}

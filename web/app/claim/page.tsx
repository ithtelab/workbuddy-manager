'use client';

/**
 * 红包抽奖页（公开）。
 *
 * 为什么不放在 `(main)/` 下：那一组的 layout 会做两件事——检查登录态、
 * 渲染管理端导航栏。而这里是**给收到链接的人看的**（同事、朋友，多半没有
 * 账号），既不该被踢去登录页，也不该看到管理入口。
 *
 * 抽奖码从 URL 的 `?code=` 取。用 `window.location` 而不是 `useSearchParams`：
 * 静态导出下后者要求外面包一层 Suspense，而这个页面没有任何服务端内容，
 * 直接读 location 更省事（也避免多一个 loading 态）。
 *
 * 抽到之后要一次给全三样东西，否则用户还得回来问：**额度**（这红包值多少）、
 * **密钥**（怎么认证）、**调用地址**（往哪儿发请求）。前两样来自接口，
 * 调用地址只能由浏览器自己算（`origin + basePath`），见 `endpoint`。
 */
import {useCallback, useEffect, useMemo, useState} from 'react';
import {Gift, Copy, Check, Loader2, Sparkles} from 'lucide-react';
import {notify} from '@/lib/toast';
import {claimApi, errText} from '@/lib/api';
import {BASE_PATH} from '@/lib/base-path';
import type {ClaimInfo, DrawResult} from '@/lib/types';
import {fmtDateTime, fmtNumber} from '@/lib/format';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
import {useT} from '@/lib/i18n/provider';
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/animate-ui/radix/dialog';

/**
 * 中奖时的彩屑（纯 CSS 动画，不引第三方）。
 *
 * 几条克制：
 *   · `prefers-reduced-motion` 下整块不渲染 —— 前庭敏感的同事打开链接就被撒一脸
 *     碎片是很糟的体验；
 *   · 数量 36 片、`pointer-events-none`、2.6 秒后自灭：它盖在弹窗之上，绝不能
 *     截走点击（「复制密钥」必须一直点得到）；
 *   · 位置与时长用确定性伪随机（由抽奖码派生），避免每次重渲染都重新随机 ——
 *     否则用户会看到彩屑「跳」一下。
 */
function Confetti({seed}: {seed: string}) {
  const pieces = useMemo(() => {
    let h = 0;
    for (const ch of seed) h = (h * 31 + ch.charCodeAt(0)) & 0xffff;
    const rand = (i: number) => {
      const x = Math.sin(h + i * 12.9898) * 43758.5453;
      return x - Math.floor(x);
    };
    const colors = ['#f59e0b', '#10b981', '#3b82f6', '#ef4444', '#8b5cf6'];
    return Array.from({length: 36}, (_, i) => ({
      left: rand(i) * 100,
      delay: rand(i + 100) * 0.6,
      duration: 2 + rand(i + 200) * 1.2,
      rotate: rand(i + 300) * 360,
      color: colors[i % colors.length],
      w: 5 + rand(i + 400) * 4,
    }));
  }, [seed]);

  return (
    <>
      <style>{`
        @keyframes wb-confetti-fall {
          0%   { transform: translateY(-12vh) rotate(0deg); opacity: 1; }
          100% { transform: translateY(96vh) rotate(680deg); opacity: 0; }
        }
        @media (prefers-reduced-motion: reduce) { .wb-confetti { display: none; } }
      `}</style>
      <div className="wb-confetti pointer-events-none fixed inset-0 z-[60]" aria-hidden="true">
        {pieces.map((p, i) => (
          <span
            key={i}
            className="absolute block rounded-[1px]"
            style={{
              left: `${p.left}%`,
              top: '-12vh',
              width: p.w,
              height: p.w * 1.8,
              backgroundColor: p.color,
              transform: `rotate(${p.rotate}deg)`,
              animation: `wb-confetti-fall ${p.duration}s ${p.delay}s cubic-bezier(.3,.6,.6,1) forwards`,
            }}
          />
        ))}
      </div>
    </>
  );
}

export default function ClaimPage() {
  const t = useT();
  const [code, setCode] = useState('');
  const [info, setInfo] = useState<ClaimInfo | null>(null);
  const [got, setGot] = useState<DrawResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  // 下游调用地址：只有浏览器知道当前 origin（静态导出阶段为空）。
  // 子路径部署时网关也挂在前缀下，所以必须带上 basePath，否则会给一个 404 的地址。
  const endpoint =
    typeof window !== 'undefined' ? `${window.location.origin}${BASE_PATH}/v1` : '';

  useEffect(() => {
    const c = new URLSearchParams(window.location.search).get('code') || '';
    setCode(c);
    if (!c) {
      setErr(t('claim.noCode'));
      setLoading(false);
      return;
    }
    claimApi.info(c)
      .then(setInfo)
      .catch((e) => setErr(errText(e)))
      .finally(() => setLoading(false));
  }, [t]);

  const draw = useCallback(async () => {
    setBusy(true);
    try {
      setGot(await claimApi.draw(code));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }, [code]);

  const closeAndCopy = useCallback(async () => {
    if (got && navigator.clipboard) {
      await navigator.clipboard.writeText(got.key).then(
        () => notify.ok(t('claim.autoCopied'), t('claim.autoCopiedDetail')),
        () => notify.err(t('claim.autoCopyFailed')),
      );
    }
    setGot(null);
  }, [got, t]);

  /** 复制「上次领到的那份」（第二次打开时用）。 */
  async function copyMine() {
    const k = info?.my_key;
    if (!k) return;
    try {
      await navigator.clipboard.writeText(k);
      notify.ok(t('claim.copied'));
    } catch {
      notify.err(t('claim.copyFailed'));
    }
  }

  async function copyKey() {
    if (!got) return;
    try {
      await navigator.clipboard.writeText(got.key);
      notify.ok(t('claim.copied'));
    } catch {
      notify.err(t('claim.copyFailed'));
    }
  }

  function amountText(v: number, kind: string): string {
    return kind === 'token' ? fmtNumber(v) : v.toFixed(2);
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm animate-in fade-in-0 zoom-in-95 rounded-[24px] bg-muted px-6 py-8 text-center duration-300">
        <Gift className="mx-auto mb-4 h-12 w-12 text-amber-500" />

        {loading ? (
          <div className="flex items-center justify-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />{t('common.loading')}
          </div>
        ) : err ? (
          <>
            <div className="text-sm font-medium">{t('claim.failed')}</div>
            <p className="mt-2 text-xs leading-5 text-muted-foreground">{err}</p>
          </>
        ) : info ? (
          <>
            <div className="text-base font-medium">
              {info.title || t('claim.untitled')}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {t('claim.subtitle', {left: info.left, total: info.shares})}
            </p>
            {info.models.length > 0 && (
              <p className="mt-1 text-[11px] text-muted-foreground">
                {t('redPacket.modelsScoped', {list: info.models.join(', ')})}
              </p>
            )}
            <p className="mt-1 text-[11px] text-muted-foreground">
              {t('claim.expiresAt', {at: fmtDateTime(info.expires_at)})}
            </p>

            {/* 抽之前就说清「拿到之后往哪儿用」——省得领完还要回来问 */}
            <div className="mt-4 rounded-2xl bg-background px-3 py-2 text-left">
              <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
                {t('claim.endpoint')}
              </div>
              <div className="mt-1 flex items-center gap-1.5">
                <code className="min-w-0 flex-1 break-all font-mono text-[11px]">{endpoint}</code>
                <CopyButton value={endpoint} title={t('claim.copyEndpoint')} />
              </div>
            </div>

            <div className="mt-5">
              {info.expired ? (
                <div className="text-xs text-amber-600 dark:text-amber-400">
                  {t('claim.expired')}
                </div>
              ) : info.left <= 0 ? (
                <div className="text-xs text-amber-600 dark:text-amber-400">
                  {t('claim.empty')}
                </div>
              ) : info.claimed && info.my_key ? (
                /* 已经领过：把上次那份**直接显示出来**。
                   关掉弹窗才想起没存是很常见的，而明文只显示那一次 ——
                   刷新就能找回来，比「请联系发红包的人」有用得多。
                   （后端只回「这个 IP 自己领的那一份」，不是别人的。） */
                <div className="text-left">
                  <div className="mb-2 rounded-full bg-amber-500/15 px-3 py-1 text-center text-xs text-amber-600 dark:text-amber-400">
                    {t('claim.alreadyWithKey')}
                  </div>
                  <div className="rounded-2xl bg-background px-4 py-4 text-center">
                    <div className="text-2xl font-semibold tabular-nums text-amber-600 dark:text-amber-400">
                      {amountText(info.my_amount ?? 0, info.quota_kind)}
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {info.quota_kind === 'token' ? t('claim.unitTokens') : t('claim.unitCredits')}
                    </div>
                  </div>
                  <div className="mt-2 break-all rounded-xl bg-background px-3 py-2 font-mono text-[11px] leading-5">
                    {info.my_key}
                  </div>
                  <Button
                    variant="outline"
                    className="mt-2 w-full rounded-full"
                    onClick={copyMine}
                  >
                    <Copy className="mr-1.5 h-4 w-4" />{t('claim.copy')}
                  </Button>
                </div>
              ) : info.claimed ? (
                <div className="text-xs text-muted-foreground">{t('claim.already')}</div>
              ) : (
                <Button
                  size="lg"
                  className="w-full rounded-full bg-amber-500 text-white transition-transform hover:bg-amber-600 active:scale-[0.98]"
                  disabled={busy}
                  onClick={draw}
                >
                  {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Sparkles className="mr-2 h-4 w-4" />}
                  {t('claim.open')}
                </Button>
              )}
            </div>
          </>
        ) : null}
      </div>

      {/* 中奖弹窗：额度在上、密钥在中、调用地址在下；关闭时自动复制密钥 */}
      {got && <Confetti seed={code} />}
      <Dialog open={!!got} onOpenChange={(v) => { if (!v) void closeAndCopy(); }}>
        <DialogContent className="max-w-sm animate-in fade-in-0 zoom-in-95 rounded-[24px] duration-300">
          <DialogHeader>
            <DialogTitle className="flex items-center justify-center gap-2 text-center">
              <span className="animate-in zoom-in-50 inline-flex h-6 w-6 items-center justify-center rounded-full bg-emerald-500/15 duration-500">
                <Check className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
              </span>
              {t('claim.won')}
            </DialogTitle>
            <DialogDescription className="text-center">
              {t('claim.wonDesc')}
            </DialogDescription>
          </DialogHeader>

          {got && (
            /* px-6 pb-6：与 DialogHeader 的 px-6 对齐。原来只有 space-y-3，
               卡片与「已复制」那行提示会直接顶到弹窗边缘（圆角处被裁掉）。 */
            <div className="space-y-3 px-6 pb-6">
              {/* 额度：最显眼的位置 */}
              <div className="animate-in zoom-in-95 rounded-2xl bg-muted px-4 py-5 text-center duration-500">
                <div className="text-3xl font-semibold tabular-nums text-amber-600 dark:text-amber-400">
                  {amountText(got.amount, got.quota_kind)}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {got.quota_kind === 'token' ? t('claim.unitTokens') : t('claim.unitCredits')}
                </div>
              </div>

              {/* 密钥 + 复制 */}
              <div className="rounded-2xl bg-muted px-3 py-3">
                <div className="mb-1.5 text-[11px] text-muted-foreground">
                  {t('claim.yourKey')}
                </div>
                <div className="break-all rounded-xl bg-background px-3 py-2 font-mono text-[11px] leading-5">
                  {got.key}
                </div>
                <Button
                  variant="outline"
                  className="mt-2 w-full rounded-full transition-transform active:scale-[0.98]"
                  onClick={copyKey}
                >
                  <Copy className="mr-1.5 h-4 w-4" />{t('claim.copy')}
                </Button>
                {got.models.length > 0 && (
                  <div className="mt-1.5 text-[11px] leading-4 text-muted-foreground">
                    {t('redPacket.modelsScoped', {list: got.models.join(', ')})}
                  </div>
                )}
              </div>

              {/* 调用地址：拿到密钥后的下一个问题就是「往哪儿发请求」 */}
              <div className="rounded-2xl bg-muted px-3 py-3 text-left">
                <div className="text-[11px] text-muted-foreground">{t('claim.endpoint')}</div>
                <div className="mt-1.5 flex items-center gap-1.5">
                  <code className="min-w-0 flex-1 break-all rounded-xl bg-background px-3 py-2 font-mono text-[11px]">
                    {endpoint}
                  </code>
                  <CopyButton value={endpoint} title={t('claim.copyEndpoint')} />
                </div>
                <p className="mt-1.5 text-[11px] leading-4 text-muted-foreground">
                  {t('claim.endpointHint')}
                </p>
              </div>

              <Button className="w-full rounded-full transition-transform active:scale-[0.98]" onClick={closeAndCopy}>
                <Check className="mr-1.5 h-4 w-4" />{t('claim.done')}
              </Button>
              <p className="text-center text-[11px] leading-4 text-muted-foreground">
                {t('claim.closeHint')}
              </p>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

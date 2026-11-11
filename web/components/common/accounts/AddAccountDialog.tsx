'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {QRCodeSVG} from 'qrcode.react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {Loader2, CheckCircle2, AlertTriangle, ExternalLink} from 'lucide-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {accountApi, errText} from '@/lib/api';
import {useRealm, type Realm} from '@/lib/realm-context';
import {Button} from '@/components/ui/button';
import {CopyButton, ShareButton} from '@/components/ui/copy-button';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogHeader,
} from '@/components/animate-ui/radix/dialog';

type Phase = 'loading' | 'waiting' | 'success' | 'error'
  /**
   * 凭证已写入、但上游的「系统禁用」没解除（或还在后台尝试）。
   *
   * 与 `error` 分开：这时**重扫二维码没有意义**（凭证是对的，缺的是上游那一步），
   * 所以不能给「重新获取」按钮，否则用户会一遍遍重扫同一个已经成功的账号，
   * 却始终看到它留在队列里 —— 那正是这条修复要终结的循环。
   */
  | 'reviveFailed';

/**
 * 连续失败几次才打断轮询。
 *
 * 取 3（约 6 秒）：单次抖动/超时不该打断用户扫码，而持续失败（例如账号目录
 * 无权写入）必须让用户看到原因 —— 否则界面会一直转圈到 5 分钟后再报
 * 「二维码已失效」，把真正的故障藏起来（issue #26）。
 */
const POLL_FAIL_LIMIT = 3;

/**
 * 国际版可选地区（与后端 INTERNATIONAL_REGIONS 保持一致）。
 * 取自国际版官网的短名单；不预选，因为地区属于账号归属信息。
 * label 为 i18n 键：地区名要跟着界面语言走（代码本身是固定的 ISO 码）。
 */
const INTERNATIONAL_REGIONS = [
  {code: 'HK', key: 'region.HK'},
  {code: 'MO', key: 'region.MO'},
  {code: 'SG', key: 'region.SG'},
  {code: 'TH', key: 'region.TH'},
  {code: 'PH', key: 'region.PH'},
  {code: 'MY', key: 'region.MY'},
  {code: 'ID', key: 'region.ID'},
] as const;

/**
 * 「用户回到这个页面了」的信号源列表。
 *
 * 为什么不是只挂 visibilitychange：Chrome 对**被遮挡**的窗口（点授权链接后
 * 那扇页盖住面板就是这种情况）会把定时器节流到约 1 次/分钟，而此时
 * `document.hidden` 仍是 false、`visibilitychange` 一次都不触发 —— 实测
 * 2 秒的定时器在 64 秒里只跑了 2 次。所以还要挂在「人确实在操作」的事件上：
 * 他回来看结果必然会让窗口获得焦点、点一下或敲一下键盘，那一刻立即补一次，
 * 这些事件不受遮挡节流影响。
 *
 * 在**函数内**才去碰 window/document：模块顶层读它们会让静态导出在构建期
 * 预渲染时直接报错（本项目是 output: 'export'）。
 */
function wakeEvents(): Array<[EventTarget, string]> {
  if (typeof window === 'undefined') return [];
  return [
    [document, 'visibilitychange'],
    [window, 'focus'],
    [document, 'pointerdown'],
    [document, 'keydown'],
  ];
}

/**
 * 「重登队列」里点重登时带过来的目标账号。
 *
 * 为什么要把账号本身传进来，而不是像以前那样只打开一个通用的「添加账户」：
 * 这两件事在服务端是不同的语义（`auth_start` 的 `uid` 参数）——
 *
 *   · **版本**必须跟账号走。以前 realm 只取自界面上的全站开关：在国际版账号上
 *     重登、而界面停在国内版时，拿到的是国内版的码、落盘时 realm 还被改写成 cn，
 *     这个账号随后拿国内版令牌去请求 → 聊天 401 → 继续被系统禁用 → 重登多少次
 *     都留在队列里。目标账号已知，版本就不该再问界面。
 *   · **扫到的必须是它**。不校验时，扫另一个号等于「又加了一个账号」，目标账号
 *     一个字节没变，队列里那一行自然纹丝不动。
 *   · 界面要说得清「现在在重登谁」—— 否则用户扫完才发现修的是别的号。
 */
export interface ReloginTarget {
  uid: string;
  nickname?: string;
  realm: Realm;
  file?: string;
  /** 面板里被「临时停用」（文件名带 .disabled）：重登刷凭证，但不会自动启用它 */
  panel_disabled?: boolean;
}

export function AddAccountDialog({
  open,
  onOpenChange,
  onSuccess,
  upstreamId,
  target,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onSuccess?: () => void;
  /** 目标分组（多账号池）：新账号落进该分组的账号目录；null / 省略 = 默认分组 */
  upstreamId?: number | null;
  /** 非空 = 重登这个账号（不是添加新账号）；版本以它为准 */
  target?: ReloginTarget | null;
}) {
  const t = useT();
  const [phase, setPhase] = useState<Phase>('loading');
  /** 版本：重登时跟目标账号，添加时跟全站切换（国际版要选地区） */
  const {realm: uiRealm, label: uiRealmName} = useRealm();
  const realm: Realm = target?.realm ?? uiRealm;
  const realmName = target ? (realm === 'global' ? t('realm.global') : t('realm.cn')) : uiRealmName;
  /** 国际版地区代码（如 HK）。不预选：地区属于账号归属信息，交由用户决定 */
  const [region, setRegion] = useState('');
  const [proxy, setProxy] = useState<string | undefined>(undefined);
  const [proxyRoutes, setProxyRoutes] = useState<string[]>([]);
  const [routesReady, setRoutesReady] = useState(false);
  const [routeError, setRouteError] = useState('');
  const [authUrl, setAuthUrl] = useState('');
  const [message, setMessage] = useState('');
  const stateRef = useRef('');
  const timerRef = useRef<number | null>(null);
  /** 上一次 poll 是否还在飞：单次超过 2 秒时避免请求叠加 */
  const pollingRef = useRef(false);
  /** 连续轮询失败次数：偶发抖动不该打断用户，持续失败才报出来 */
  const failsRef = useRef(0);
  /**
   * 当前这轮轮询的 tick 函数。
   *
   * 存成 ref 是为了让 `visibilitychange` 监听器能拿到最新那份 —— 与 timer
   * 同生共死，避免「切回来时调用了一个已被 stopPoll 作废的闭包」。
   */
  const tickRef = useRef<(() => void) | null>(null);

  const stopPoll = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    // 一并摘掉唤醒监听：否则弹窗关掉后，切标签页/点页面仍会去调用已作废的
    // tick（轻则白发请求，重则对着已关闭的弹窗 setState）。
    // 与 start() 里用的是同一个列表，漏摘一个就是内存泄漏 + 白发请求。
    if (tickRef.current !== null) {
      wakeEvents().forEach(([target, event]) =>
        target.removeEventListener(event, tickRef.current!),
      );
      tickRef.current = null;
    }
    pollingRef.current = false;
  }, []);

  useEffect(() => {
    if (!open) { setRoutesReady(false); return; }
    let active = true;
    setRoutesReady(false);
    setRouteError('');
    accountApi.proxies().then((data) => {
      if (!active) return;
      setProxyRoutes(data.routes);
      setProxy(data.default || '');
      setRoutesReady(true);
    }).catch((error) => {
      if (!active) return;
      setRouteError(errText(error));
      // 线路表只是一个可选出口：拉取失败不该把「加账号」卡死，否则二维码
      // 永远不出现、弹窗看起来像在无限加载。退回直连继续扫码，错误仍留在上方。
      setProxy('');
      setRoutesReady(true);
    });
    return () => { active = false; };
  }, [open]);

  const start = useCallback(async () => {
    stopPoll();
    failsRef.current = 0;
    setPhase('loading');
    setMessage(t('addAccount.requesting'));
    setAuthUrl('');
    try {
      // region 一起发：服务端要替这张码盯着（前端被节流也不影响），而地区
      // 登记必须在落盘前完成，它那条路径读不到弹窗里的 state，只能在这里给。
      const data = await accountApi.start(realm, upstreamId, region || undefined, proxy,
                                          target?.uid);
      stateRef.current = data.state;
      setAuthUrl(data.authUrl);
      setPhase('waiting');
      setMessage(t('addAccount.waiting'));

      const tick = async () => {
        // 标签页在后台就不查：浏览器本来也会把定时器节流到约 1 次/分钟，
        // 与其让它零星触发，不如等用户切回来时补一次（与 useHeartbeat 同口径）。
        // 判断放在 tick 内部而不是调用处 —— 因为 visibilitychange 在**隐藏与
        // 显示时都会触发**，放外面的话「切走」那一下也会白发一次请求。
        if (document.hidden) return;
        if (pollingRef.current) return;  // 上一次还没回来，跳过本轮
        pollingRef.current = true;
        try {
          const res = await accountApi.poll(stateRef.current, realm, region || undefined,
                                            upstreamId);
          // 拿到任何一次正常响应就清零：计数要表达的是「**连续**失败」，
          // 而不是「累计失败了几次」。不清零的话，几分钟内零散抖三次
          // （每次之间都恢复正常）也会触发中断，把一次正常的扫码打断。
          failsRef.current = 0;
          if (res.status === 'success') {
            stopPoll();
            // 重登：凭证写进去了，但上游的「系统禁用」位只有 revive 能清。
            // 这一步失败（管理接口没开 / api_key 不对 / 账号被封禁）时以前只写
            // 一行日志，界面上什么也没有 —— 用户看到的就是「重登成功了，可它还
            // 在重登队列里」。现在把原因原样摆出来，并保留重试入口。
            const revive = res.revive;
            if (revive && !revive.ok) {
              setPhase('reviveFailed');
              setMessage(revive.pending
                ? t('addAccount.revivePending')
                : t('addAccount.reviveStillDisabled', {reason: revive.message || ''}));
              // 账号本身已落盘，列表必须刷新（凭证确实是新的）
              window.dispatchEvent(new Event('workbuddy-manager:accounts-changed'));
              onSuccess?.();
              return;
            }
            setPhase('success');
            const accountName = res.nickname || res.uid || '';
            setMessage(
              res.updated
                ? t('addAccount.successUpdated', {name: accountName})
                : t('addAccount.success', {name: accountName}),
            );
            notify.ok(
              t('addAccount.success', {name: accountName}),
              res.realm === 'global'
                ? t('addAccount.successGlobal')
                : res.updated
                  ? t('addAccount.successToken')
                  : t('addAccount.successCheckin'),
            );
            window.dispatchEvent(new Event('workbuddy-manager:accounts-changed'));
            onSuccess?.();
            window.setTimeout(() => onOpenChange(false), 1600);
          } else if (res.status === 'expired') {
            // 真过了有效期（5 分钟）：重新取一张码才是对的
            stopPoll();
            setPhase('error');
            setMessage(t('addAccount.qrExpired'));
          } else if (res.status === 'invalid') {
            // 与「过期」分开报：state 不在缓存里意味着**服务端重启过**
            // （或部署成多进程），二维码本身没问题。以前两者共用一句
            // 「二维码已失效」，用户会一直重扫却怎么都不成功（issue #26）。
            stopPoll();
            setPhase('error');
            setMessage(t('addAccount.stateLost'));
          } else if (res.status === 'realm_mismatch') {
            stopPoll();
            setPhase('error');
            setMessage(t('addAccount.realmMismatch'));
          } else if (res.status === 'account_mismatch') {
            // 扫的是另一个账号：服务端没落盘（也不该落），目标账号原封不动。
            // 直接说清「扫到的是谁、要的是谁」，否则用户只会重复同一个动作。
            stopPoll();
            setPhase('error');
            setMessage(t('addAccount.accountMismatch', {
              got: res.nickname || res.uid || '?',
              want: target?.nickname || res.expected_name || res.expected_uid || '?',
            }));
          }
        } catch (e) {
          // 不再静默吞掉：轮询出错（落盘失败 / 网络抖动 / 上游超时）以前会被
          // 丢弃并继续轮询，最后必然走到「二维码已失效」——把真实原因藏了起来。
          // 现在连续失败到阈值就报出来，并保留重试入口。
          failsRef.current += 1;
          if (failsRef.current >= POLL_FAIL_LIMIT) {
            stopPoll();
            setPhase('error');
            setMessage(errText(e));
          }
        } finally {
          pollingRef.current = false;
        }
      };

      tickRef.current = tick;
      timerRef.current = window.setInterval(tick, 2000);
      // **兜底不能只挂可见性**：「切回标签页立即补一次」挂在 visibilitychange 上，
      // 但 Chrome 对**被遮挡**的窗口（授权页盖住面板就是这样）会节流定时器——
      // 实测 2 秒降到约 1 分钟一次——而 document.hidden 仍是 false、
      // visibilitychange 一次都不触发，那条兜底因此完全失效，用户切回来只能干等。
      //
      // 所以再挂在「用户确实在操作这个页面」的事件上：他回来看结果必然要点一下、
      // 敲一下或让窗口获得焦点，那一刻立即补一次。这些事件不受遮挡节流影响。
      // 检测本身已经在服务端跑了（见 server/routers/accounts.py 的说明），
      // 这里只是去读结果，所以补得越勤也只是读一次内存。
      wakeEvents().forEach(([target, event]) => target.addEventListener(event, tick));
    } catch (e) {
      setPhase('error');
      setMessage(errText(e));
    }
  }, [onOpenChange, onSuccess, stopPoll, realm, region, proxy, t, upstreamId,
      target?.uid]);

  useEffect(() => {
    if (open && routesReady) {
      start();
    } else {
      stopPoll();
    }
    return stopPoll;
    // 版本或地区变化时重新申请：扫码码是绑定端点的，旧码不能跨版本用
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, realm, region, upstreamId, proxy, routesReady, target?.uid]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[420px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>
            {target
              ? t('addAccount.reloginTitle', {name: target.nickname || target.uid})
              : t('addAccount.title', {realm: realmName})}
          </DialogTitle>
          <DialogDescription>
            {target
              ? t('addAccount.reloginDesc', {realm: realmName})
              : realm === 'global'
                ? t('addAccount.descGlobal')
                : t('addAccount.descCn')}
          </DialogDescription>
        </DialogHeader>

        <div className="flex w-full min-w-0 flex-col items-center gap-4 px-6 pb-6">
          {/* 重登时把「给谁修」摆明：版本已由该账号决定，界面上的全局切换在这里
              不参与（否则国际版账号会被写进一张国内版凭证）。 */}
          {target && (
            <div className="w-full rounded-xl border border-border/60 bg-muted/50 px-3 py-2 text-[11px] leading-4">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium">
                  {target.nickname || t('accounts.unnamed')}
                </span>
                <span className="rounded-full bg-background px-2 py-0.5 text-muted-foreground">
                  {realmName}
                </span>
              </div>
              <div className="mt-0.5 font-mono text-[10px] text-muted-foreground truncate">
                {target.uid}
              </div>
              {target.panel_disabled && (
                <div className="mt-1 text-amber-600 dark:text-amber-400">
                  {t('addAccount.reloginPanelDisabled')}
                </div>
              )}
            </div>
          )}
          {routeError && <p role="alert" className="text-xs text-destructive">{routeError}</p>}
          {/* 没有配置任何线路时整个选择器都不出现：那时下拉里只有「直连」一项，
              摆出来只会让人以为能选却选不动（线路表读自上游配置的 proxies）。 */}
          {proxyRoutes.length > 0 && (
            <div className="w-full space-y-1.5">
              <div className="text-[11px] font-medium">{t('accounts.proxyLine')}</div>
              <Select value={proxy ? `route:${proxy}` : 'default'} disabled={!routesReady}
                      onValueChange={(value) => setProxy(value === 'default' ? '' : value.slice(6))}>
                <SelectTrigger className="h-9 w-full rounded-full text-xs" aria-label={t('accounts.proxyLine')}>
                <SelectValue />
              </SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">{t('accounts.proxyDirect')}</SelectItem>
                  {proxy && !proxyRoutes.includes(proxy) &&
                    <SelectItem value={`route:${proxy}`}>{proxy} ({t('accounts.proxyMissing')})</SelectItem>}
                  {proxyRoutes.map((route) => <SelectItem key={route} value={`route:${route}`}>{route}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          )}
          {/* 国际版必须先完成地区注册，否则聊天报 14017。
              放在二维码之前：地区一变就要重新申请授权码，先选好再扫省得白扫。 */}
          {realm === 'global' && (
            <div className="w-full space-y-1.5">
              <div className="text-[11px] font-medium">{t('addAccount.regionLabel')}</div>
              <Select value={region} onValueChange={setRegion}>
                <SelectTrigger className="h-9 w-full rounded-full text-xs" aria-label={t('addAccount.regionLabel')}>
                  <SelectValue placeholder={t('addAccount.regionPlaceholder')} />
                </SelectTrigger>
                <SelectContent>
                  {INTERNATIONAL_REGIONS.map((r) => (
                    <SelectItem key={r.code} value={r.code} className="text-xs">
                      {t('addAccount.regionOption', {label: t(r.key), code: r.code})}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {!region && (
                <p className="text-[11px] leading-4 text-muted-foreground">
                  {t('addAccount.regionHint')}
                </p>
              )}
            </div>
          )}

          {/* 固定尺寸，避免 loading/waiting/error 各阶段弹窗高度跳动 */}
          <div className="grid h-[212px] w-[212px] shrink-0 place-items-center overflow-hidden rounded-2xl bg-white p-3 ring-1 ring-black/5">
            {phase === 'loading' && <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />}
            {(phase === 'error' || phase === 'reviveFailed') &&
              <AlertTriangle className="h-7 w-7 text-amber-500" />}
            {(phase === 'waiting' || phase === 'success') && authUrl && (
              <QRCodeSVG value={authUrl} size={188} level="M" />
            )}
          </div>

          {/* reviveFailed = 这张码已经用掉了（凭证写入成功），再点开它没有意义 */}
          {authUrl && phase !== 'reviveFailed' && (
            <div className="w-full space-y-2">
              {/* 链接本身可点开；旁边给复制与分享，便于把授权链接发给朋友 */}
              <div className="flex w-full items-center gap-1.5 rounded-full bg-muted px-3 py-1.5">
                <ExternalLink className="h-3 w-3 shrink-0 text-muted-foreground" />
                <a
                  href={authUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={authUrl}
                  className="min-w-0 flex-1 truncate text-[11px] text-blue-500 hover:underline"
                >
                  {authUrl}
                </a>
              </div>
              <div className="flex flex-wrap items-center justify-center gap-2">
                <CopyButton
                  value={authUrl}
                  size="sm"
                  showLabel
                  label={t('addAccount.copyLink')}
                  variant="outline"
                  className="rounded-full"
                />
                <ShareButton
                  title={t('addAccount.shareTitle')}
                  text={t('addAccount.shareText')}
                  url={authUrl}
                />
              </div>
            </div>
          )}

          <div
            className={
              'flex items-center gap-2 px-2 text-xs ' +
              (phase === 'success' ?
                'text-emerald-500' :
                phase === 'error' || phase === 'reviveFailed' ?
                  'text-red-500' :
                  'text-muted-foreground')
            }
          >
            {phase === 'waiting' && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />}
            {phase === 'success' && <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />}
            <span className="text-center">{message}</span>
          </div>

          <div className="flex w-full gap-2">
            <Button variant="outline" className="flex-1 rounded-full" onClick={() => onOpenChange(false)}>
              {t('common.cancel')}
            </Button>
            {phase === 'error' && (
              <Button className="flex-1 rounded-full" onClick={start}>
                {t('addAccount.retry')}
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

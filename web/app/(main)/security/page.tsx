'use client';

import {useState} from 'react';
import {ShieldCheck, Plus, Trash2, Ban, CircleCheck, Network} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useAsyncAll} from '@/lib/use-async-data';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {securityApi, errText} from '@/lib/api';
import type {AuditLog, IpAccessLog, IpRule, SecurityConfig} from '@/lib/types';
import {fmtDateTime} from '@/lib/format';
import {FileClock} from 'lucide-react';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {settingsApi} from '@/lib/api';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {Skeleton} from '@/components/ui/skeleton';
import {cn} from '@/lib/utils';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

/** 审计动作 → i18n 键（动作本身是后端固定的英文枚举，这里只做展示名映射） */
const AUDIT_LABEL_KEYS: Record<string, string> = {
  login: 'security.auditLogin',
  login_failed: 'security.auditLoginFailed',
  update_user: 'security.auditUpdateUser',
  delete_user: 'security.auditDeleteUser',
  add_user: 'security.auditAddUser',
};

/**
 * 审计详情本地化。
 *
 * 后端把详情存成中文（如「角色=admin；来源 1.2.3.4」）——存储层保持语言中立
 * （否则同一条记录会因写入者语言不同而不一致），展示时按固定分词翻译已知标记，
 * 认不出的片段原样保留。
 */
function auditDetail(detail: string | null | undefined, t: (key: string, params?: Record<string, string>) => string): string {
  if (!detail) return '—';
  return detail
    .split('；')
    .map((part) => {
      const from = /^来源\s+(.+)$/.exec(part);
      if (from) return t('security.detailFrom', {ip: from[1]});
      if (part === '密码=已重置') return t('security.detailPasswordReset');
      const role = /^角色=(.+)$/.exec(part);
      if (role) return t('security.detailRole', {role: role[1]});
      // 「段=upstream,pool」：本次实际改动过的配置段。段名是后端的键名，不翻译，
      // 只把「段=」这个标签换掉。
      const segment = /^段=(.*)$/.exec(part);
      if (segment) return t('security.detailSegment', {segments: segment[1]});
      // 「改密码、改角色」：用户资料变更标记，用「、」连接成一串，逐个词翻译后
      // 再用目标语言的连接符拼回去。认不全就整段原样保留（宁可不译也不译错）。
      const tokens = part.split('、').map((token) => {
        if (token === '改密码') return t('security.detailPasswordChanged');
        if (token === '改角色') return t('security.detailRoleChanged');
        return null;
      });
      if (tokens.every((v) => v !== null)) {
        return tokens.join(t('security.detailChangedSeparator'));
      }
      return part;
    })
    .join(t('security.detailSeparator'));
}

/**
 * 拦截原因短码的**兜底映射**：后端已并入 keysvc 的 code，加上网关自己的三个。
 *
 * 为什么要在前端做一次映射而不是后端直接存译文：日志表存的是稳定短码，
 * 界面切成英文时同一批记录要显示英文——存译文就做不到（要么历史记录永远是
 * 中文，要么改文案得迁移数据）。
 *
 * 认不出的码**原样显示**：将来后端加了新原因，界面不会显示成空白或「未知」，
 * 用户至少能看到那个码（拿它搜仓库能找到定义）。
 */
const REASON_KEYS: Record<string, string> = {
  // 网关层
  missing_key: 'security.reasonMissingKey',
  invalid_key: 'security.reasonInvalidKey',
  ip_blocked: 'security.reasonIpBlocked',
  rate_limited: 'security.reasonRateLimited',
  // 密钥层（keysvc 的 code）
  key_disabled: 'security.reasonKeyDisabled',
  key_expired: 'security.reasonKeyExpired',
  quota_exhausted: 'security.reasonQuotaExhausted',
  credit_quota_exhausted: 'security.reasonCreditQuota',
  ip_not_allowed: 'security.reasonIpNotAllowed',
  too_many_ips: 'security.reasonTooManyIps',
  realm_mismatch: 'security.reasonRealmMismatch',
  model_not_allowed: 'security.reasonModelNotAllowed',
};

function reasonText(t: (key: string, params?: Record<string, string>) => string, code: string): string {
  const key = REASON_KEYS[code];
  return key ? t(key) : code;
}

/**
 * 取数完成前的空值。必须是模块级同一份：写成 `values.rules ?? []` 的话每次渲染
 * 都会新建数组，进下游 useMemo 的依赖后每帧都变（同 dashboard 的处理）。
 */
const EMPTY_RULES: IpRule[] = [];
const EMPTY_LOGS: IpAccessLog[] = [];
const EMPTY_AUDIT: AuditLog[] = [];

export default function SecurityPage() {
  const {isAdmin} = useAuth();
  const t = useT();

  /**
   * 本页要的 4 份数据一次并发取回，成败逐项独立；加载态 / 失败态 / 是否正在刷新
   * 也由它给出，本页不再自己维护 `loading`。
   *
   * 为什么必须换掉原先手写的那套 `Promise.allSettled` + 4 个 useState：那套写法
   * **把失败静默丢掉了**（`if (x.status === 'fulfilled')` 后面没有 else，连 toast
   * 都没有），于是取数失败时界面照常渲染初始值——`config` 的初值是
   * `{enabled: false, mode: 'blacklist'}`，也就是**把「IP 访问控制：关闭」当成事实
   * 显示出来**。这一页讲的是访问控制，把「开着」说成「关着」比空白更糟：管理员会
   * 据此判断「拦截没生效」「没人被拦过」，而这两句话都没有任何依据。
   *
   * 现在：一次都没取到 → 整页错误态 + 重试；取到一部分 → 保留已取到的，
   * 顶部一条常驻提示说明有几项没刷上；`config` 单独没取到时，那张卡片如实说
   * 「未取到」而不是显示一个关闭状态的开关。
   */
  const {values, errors, isInitialLoading, isInitialFailed, isRefreshing, reload} = useAsyncAll({
    config: () => securityApi.config(),
    rules: () => securityApi.rules(),
    logs: () => securityApi.logs(200),
    audit: () => settingsApi.auditLogs(200),
  }, []);

  /** null = 这份数据还没取到（首屏加载中，或它自己失败了）。**不要**用假默认值兜底 */
  const config: SecurityConfig | null = values.config ?? null;
  const rules: IpRule[] = values.rules ?? EMPTY_RULES;
  const logs: IpAccessLog[] = values.logs ?? EMPTY_LOGS;
  /** 管理端审计日志：登录、改密码、增删用户等敏感操作留痕 */
  const audit: AuditLog[] = values.audit?.items ?? EMPTY_AUDIT;

  const [newKind, setNewKind] = useState<'allow' | 'deny'>('deny');
  const [newCidr, setNewCidr] = useState('');
  const [newNote, setNewNote] = useState('');
  const [busy, setBusy] = useState(false);
  /** 保存配置时的乐观值，见 saveConfig 的说明。null = 没有在途改动 */
  const [optimistic, setOptimistic] = useState<SecurityConfig | null>(null);
  /** 实际显示的那份配置：在途改动优先，否则用取回来的 */
  const shownConfig = optimistic ?? config;

  // IP 规则与访问日志会随流量变化，心跳刷新保持同步。
  // 走 reload（刷新模式）：只把新数据换上去，**不**重走首屏流程——否则骨架会每
  // 60 秒闪一次。
  useHeartbeat(reload, 60000);

  /** 有字段没刷新成功。此时至少还有一份数据在，用常驻提示如实说明，不清空内容 */
  const partialFailed = Object.keys(errors).length > 0;

  /**
   * 某一项**从未**取到（首屏那次就失败了，所以 values 里始终没有它）。
   *
   * 用来把「没有数据」与「不知道有没有数据」分开：两者都会渲染成空表，但含义
   * 完全相反。「暂无 IP 规则」是**一条规则都没有**的断言——在这一页等于告诉管理员
   * 「没有任何网段被放行或拦截」，而实际可能只是这次没取到。宁可说「未取到」。
   */
  const rulesFailed = 'rules' in errors;
  const logsFailed = 'logs' in errors;
  const auditFailed = 'audit' in errors;

  /**
   * 保存配置。乐观更新：先切到目标态让开关立刻响应，失败再回滚——否则界面会停在
   * 一个后端并未生效的状态上（要等刷新才暴露）。
   *
   * 两处与原先不同，都是因为 `config` 现在归 useAsyncAll 管、没有 setConfig 了：
   *  · 乐观值放在 `optimistic` 里并**优先于**取回的值显示；
   *  · 成功后**不能立刻撤掉**它——那一刻 values.config 还是上一次取回的旧值，
   *    撤早了开关会跳回旧态。要先 `await reload()` 把新值拉回来再撤
   *    （reload 会在这轮请求落地后才 resolve，见 lib/use-async-data.ts）。
   * 失败时撤掉乐观值即可：显示回落到**服务端最后确认过**的那份值。
   */
  async function saveConfig(next: SecurityConfig) {
    setOptimistic(next);
    try {
      await securityApi.saveConfig(next);
      notify.ok(t('security.configSaved'));
      await reload();
      setOptimistic(null);
    } catch (e) {
      setOptimistic(null);
      notify.err(errText(e));
    }
  }

  async function addRule() {
    if (!newCidr.trim()) {
      notify.err(t('security.ipRequired'));
      return;
    }
    setBusy(true);
    try {
      await securityApi.addRule({kind: newKind, cidr: newCidr.trim(), note: newNote.trim()});
      notify.ok(t('security.ruleAdded'));
      setNewCidr('');
      setNewNote('');
      // 等这轮刷新落地再解除 busy：用户看到的「成功」包含列表里真的多出这一条。
      // 若刷新失败，reload() 返回 false 而**不会**抛错——写操作是成功的，绝不能
      // 报成失败（用户会以为没加上、再点一次，于是多出一条重复规则）。
      // 「列表没刷上」由上面的常驻提示承担。
      await reload();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  const header = (
    <PageHeader
      title={t('security.title')}
      description={t('security.description')}
    />
  );

  // 首屏：一份都没取到。此时**不能**渲染下面的卡片——`config` 的初值是
  // `{enabled: false, mode: 'blacklist'}`，那等于把「IP 访问控制：关闭」当成事实
  // 显示出来。这一页讲的正是访问控制，把「开着」说成「关着」比空白更糟：管理员
  // 会据此判断「拦截没生效」「没人被拦过」，而这两句话都没有任何依据。
  // 一次都没取到（失败）同理：给错误态与「重试」，而不是一直转圈。
  if (isInitialFailed || isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? <LoadError variant="page" onRetry={reload} /> : <SecuritySkeleton />}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* 刷新时有请求失败：常驻提示，**不**顶掉已经显示出来的内容——那些数据
          仍然是对的，只是可能不是最新的。原先这套取数把失败静默丢掉了，连
          提示都没有，界面照常显示初始值。 */}
      {partialFailed && (
        <LoadError message={t('security.partialLoadFailed')} onRetry={reload} />
      )}

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-[20px] bg-muted p-4 lg:col-span-1">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <ShieldCheck className="h-4 w-4" />
            {t('security.policy')}
          </div>
          <div className="space-y-4">
            {shownConfig === null ? (
              /* config 单独没取到：如实说「未取到」。**不能**渲染一个处于「关闭」
                 状态的开关——那会把「不知道」说成「关着」，而这一页的开关正是
                 用户判断拦截是否生效的依据。 */
              <div className="text-[11px] text-muted-foreground">{t('security.notLoaded')}</div>
            ) : (
              <>
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-xs font-medium">{t('security.enableControl')}</div>
                    <div className="text-[11px] text-muted-foreground">{t('security.enableControlHint')}</div>
                  </div>
                  <Switch
                    checked={shownConfig.enabled}
                    disabled={!isAdmin}
                    onCheckedChange={(v) => saveConfig({...shownConfig, enabled: v})}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('security.mode')}</Label>
                  <Select
                    value={shownConfig.mode}
                    disabled={!isAdmin}
                    onValueChange={(v) => saveConfig({...shownConfig, mode: v as SecurityConfig['mode']})}
                  >
                    <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="blacklist">{t('security.modeBlacklist')}</SelectItem>
                      <SelectItem value="whitelist">{t('security.modeWhitelist')}</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </>
            )}
          </div>
        </div>

        <div className="rounded-[20px] bg-muted p-4 lg:col-span-2">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Network className="h-4 w-4" />
            {t('security.addRule')}
          </div>
          <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-4">
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">{t('security.type')}</Label>
              <Select value={newKind} onValueChange={(v) => setNewKind(v as 'allow' | 'deny')} disabled={!isAdmin}>
                <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="deny">{t('security.deny')}</SelectItem>
                  <SelectItem value="allow">{t('security.allow')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">IP / CIDR</Label>
              <Input value={newCidr} onChange={(e) => setNewCidr(e.target.value)} placeholder={t('security.cidrPlaceholder')} className="bg-background" disabled={!isAdmin} />
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">{t('security.note')}</Label>
              <Input value={newNote} onChange={(e) => setNewNote(e.target.value)} placeholder={t('common.optional')} className="bg-background" disabled={!isAdmin} />
            </div>
            <Button className="rounded-full" onClick={addRule} disabled={!isAdmin || busy}>
              <Plus />
              {t('security.add')}
            </Button>
          </div>

          <div className="mt-4 overflow-hidden rounded-2xl bg-background/60">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-3 text-[11px] text-muted-foreground">{t('security.type')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">IP / CIDR</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('security.note')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('security.createdAt')}</TableHead>
                  {isAdmin && <TableHead className="pr-3 text-right text-[11px] text-muted-foreground">{t('accounts.colActions')}</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {rules.map((r) => (
                  <TableRow key={r.id} className="border-b border-border/40">
                    <TableCell className="pl-3">
                      {r.kind === 'allow' ? (
                        <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
                          <CircleCheck className="h-3 w-3" />{t('security.allow')}
                        </Badge>
                      ) : (
                        <Badge variant="destructive" className="rounded-full">
                          <Ban className="h-3 w-3" />{t('security.deny')}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{r.cidr}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{r.note || '—'}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{fmtDateTime(r.created_at)}</TableCell>
                    {isAdmin && (
                      <TableCell className="pr-3 text-right">
                        {/* 二次确认是 README 明确承诺过的（「危险操作一律二次确认」），
                            而删 IP 规则此前是唯一漏网的一处：点一下就直接删了。
                            改的是**访问控制**——删掉一条 deny 规则等于当场放行该网段，
                            删掉一条 allow 规则等于当场把该网段关在门外，两者都不该由
                            一次误点决定。 */}
                        <ConfirmDialog
                          title={t('security.deleteRuleTitle')}
                          description={t('security.deleteRuleDesc')}
                          confirmText={t('keys.delete')}
                          destructive
                          onConfirm={async () => {
                            try {
                              await securityApi.removeRule(r.id);
                              notify.ok(t('keys.deleted'));
                              await reload();
                            } catch (e) {
                              notify.err(errText(e));
                            }
                          }}
                          trigger={
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7 rounded-md text-red-500 hover:text-red-600"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          }
                        />
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {!rules.length && (
              /* 空表有两种截然不同的含义，不能共用一句话：`noRules` 断言的是
                 「一条规则都没有」（在这一页等于说没有任何网段被放行或拦截），
                 而取数失败时我们**并不知道**有没有规则。 */
              <div className="py-8 text-center text-xs text-muted-foreground">
                {rulesFailed ? t('security.notLoaded') : t('security.noRules')}
              </div>
            )}
          </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex items-center justify-between px-4 py-3">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="text-sm font-medium">{t('security.accessLog')}</span>
            {/* 说明这张表**只记拦截**：用户看到记录变少时会以为是坏了，
                而实际上是被刻意收窄到安全信号（放行的明细在「请求日志」页）。
                不写这句，这个改动本身就是个新的困惑源。 */}
            <span className="text-[11px] text-muted-foreground">
              {t('security.accessLogScope')}
            </span>
          </div>
          {isAdmin && (
            <ConfirmDialog
              title={t('security.clearLogsTitle')}
              description={t('security.clearLogsDesc')}
              confirmText={t('common.clear')}
              destructive
              onConfirm={async () => {
                await securityApi.logsClear();
                notify.ok(t('logs.cleared'));
                await reload();
              }}
              trigger={
                <Button variant="outline" size="sm" className="rounded-full text-red-500">
                  <Trash2 />
                  {t('common.clear')}
                </Button>
              }
            />
          )}
        </div>
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('logs.colTime')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">IP</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('security.path')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('security.result')}</TableHead>
              <TableHead className="pr-4 text-[11px] text-muted-foreground">User-Agent</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {logs.map((l) => (
              <TableRow key={l.id} className="border-b border-border/40">
                <TableCell className="pl-4 text-xs text-muted-foreground">{fmtDateTime(l.ts)}</TableCell>
                <TableCell className="font-mono text-xs">{l.ip}</TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">{l.path}</TableCell>
                <TableCell>
                  {/* 原因直接跟在「已拦截」后面：只有「已拦截」两个字时，用户
                      不知道是没带密钥、密钥不认识还是 IP 规则拦的，而这三者的
                      处置方式完全不同（改客户端配置 / 重新发密钥 / 改规则）。
                      没有原因（放行，或升级前没记）时不占位。 */}
                  <div className="flex flex-wrap items-center gap-1.5">
                    {l.blocked ? (
                      <Badge variant="destructive" className="rounded-full">{t('security.blocked')}</Badge>
                    ) : (
                      <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">{t('security.allowed')}</Badge>
                    )}
                    {l.blocked && l.reason && (
                      <span className="text-[11px] text-muted-foreground">{reasonText(t, l.reason)}</span>
                    )}
                  </div>
                </TableCell>
                <TableCell className="max-w-[280px] truncate pr-4 text-[11px] text-muted-foreground">
                  {l.ua || '—'}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {!logs.length &&
          (logsFailed ? (
            <div className="py-14 text-center text-xs text-muted-foreground">{t('security.notLoaded')}</div>
          ) : (
            <EmptyState
              icon={ShieldCheck}
              title={t('security.noAccessLogs')}
              description={t('security.noAccessLogsDesc')}
              className="flex flex-col items-center justify-center py-14 text-center"
            />
          ))}
      </section>

      {/* 管理端审计日志：本次安全事件暴露的问题之一就是「改密码不留痕」，
          只能靠反代日志去猜。这里把敏感操作（登录成败、改密码、增删用户）
          长期留痕，便于事后追溯与发现异常尝试。 */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <FileClock className="h-4 w-4" />
            {t('security.auditLog')}
            <span className="hidden text-[11px] font-normal text-muted-foreground sm:inline">
              {t('security.auditLogHint')}
            </span>
          </div>
          <Badge variant="secondary" className="shrink-0 rounded-full tabular-nums">
            {t('security.auditCount', {count: audit.length, n: audit.length})}
          </Badge>
        </div>
        {audit.length ? (
          <Table>
            <TableHeader>
              <TableRow className="border-b border-border/60 hover:bg-transparent">
                <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('logs.colTime')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('security.action')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('security.actor')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('security.target')}</TableHead>
                <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('security.detail')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {audit.map((a) => (
                <TableRow key={a.id} className="border-b border-border/40">
                  <TableCell className="pl-4 text-xs tabular-nums text-muted-foreground">
                    {fmtDateTime(a.ts)}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="secondary"
                      className={
                        'rounded-full text-[10px] ' +
                        (a.action === 'login_failed'
                          ? 'bg-red-500/12 text-red-600 dark:text-red-400'
                          : a.action === 'login'
                            ? 'text-emerald-600 dark:text-emerald-400'
                            : '')
                      }
                    >
                      {AUDIT_LABEL_KEYS[a.action] ? t(AUDIT_LABEL_KEYS[a.action]) : a.action}
                    </Badge>
                  </TableCell>
                  {/* 登录失败时后端把「没填用户名」记成 (空) —— 这个标记也要跟着界面语言走 */}
                  <TableCell className="text-xs">
                    {a.actor === '(空)' ? t('security.actorEmpty') : a.actor || '—'}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{a.target || '—'}</TableCell>
                  <TableCell className="max-w-[420px] truncate pr-4 text-[11px] text-muted-foreground" title={a.detail}>
                    {auditDetail(a.detail, t)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : auditFailed ? (
          <div className="py-14 text-center text-xs text-muted-foreground">{t('security.notLoaded')}</div>
        ) : (
          <EmptyState
            icon={FileClock}
            title={t('security.noAudit')}
            description={t('security.noAuditDesc')}
            className="flex flex-col items-center justify-center py-14 text-center"
          />
        )}
      </section>
    </div>
  );
}

/**
 * 骨架里的一根「条」。
 *
 * 与 `dashboard` 同因：`Skeleton` 自带 `bg-accent`，而本页卡片用的是 `bg-muted`
 * ——这两个颜色在 globals.css 里**明暗两套主题下都是同一个字面量**，直接放上去
 * 等于画了看不见的条。改用前景色的低透明度：浅色下压暗、深色下提亮。
 */
function Bar({className}: {className?: string}) {
  return <Skeleton className={cn('bg-foreground/10', className)} />;
}

/**
 * 首屏骨架。结构与真实内容**逐块对应**（配置卡 + 加规则卡 / 访问日志表 / 审计表），
 * 而不是一坨居中的转圈：数据到位时版面不会整体跳一下。
 *
 * 页面每 60 秒心跳刷新一次，但只有「一份都没取到」才会走到这里
 * （见 use-async-data 的 isInitialLoading），所以不会一闪一闪。
 */
function SecuritySkeleton() {
  return (
    <>
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-[20px] bg-muted p-4 lg:col-span-1">
          <Bar className="mb-3 h-3.5 w-24" />
          <div className="space-y-4">
            <Bar className="h-9 w-full rounded-xl" />
            <Bar className="h-9 w-full rounded-xl" />
          </div>
        </div>
        <div className="rounded-[20px] bg-muted p-4 lg:col-span-2">
          <Bar className="mb-3 h-3.5 w-20" />
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
            {Array.from({length: 4}, (_, i) => (
              <Bar key={i} className="h-9 w-full rounded-xl" />
            ))}
          </div>
          <Bar className="mt-4 h-32 w-full rounded-2xl" />
        </div>
      </section>

      <section className="rounded-[20px] bg-muted p-4">
        <Bar className="mb-4 h-3.5 w-28" />
        <Bar className="h-40 w-full rounded-2xl" />
      </section>

      <section className="rounded-[20px] bg-muted p-4">
        <Bar className="mb-4 h-3.5 w-24" />
        <Bar className="h-40 w-full rounded-2xl" />
      </section>
    </>
  );
}

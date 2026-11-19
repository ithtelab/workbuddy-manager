'use client';

/**
 * 「设置 → 页面监控」：上游网页改版 / 活动入口的定时监控。
 *
 * 三件事在这张页上：**看**（上次检测做了什么、判读是什么）、**调**（间隔、
 * 目标、通知时机）、**验**（立即跑一轮、探测 SMTP 出口、发测试邮件 / QQ 消息）。
 *
 * 为什么所有输入共用一个 `form` 而不是每个字段一份 state：这张表单有 30 多个
 * 字段，保存时要把它们作为一个整体提交（后端以**当前配置**为底合并，少传一段
 * 就会退回默认值）。逐字段 state 会在「新增一个字段却忘了加进 payload」时静默
 * 丢配置——那类错不会报错，只会在下一次检测时表现成「怎么不监控了」。
 */
import {useCallback, useEffect, useState} from 'react';
import {
  Activity,
  Cpu,
  Loader2,
  Mail,
  MessageCircle,
  Play,
  PlugZap,
  Plus,
  Radar,
  RefreshCw,
  Save,
  Send,
  Trash2,
} from 'lucide-react';

import {errText, watchdogApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {fmtDateTime} from '@/lib/format';
import {RichText} from '@/lib/i18n/rich-text';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {
  WatchdogConfig,
  WatchdogEgress,
  WatchdogLlmConfig,
  WatchdogMailConfig,
  WatchdogQqBot,
  WatchdogQqConfig,
  WatchdogRunResult,
  WatchdogTarget,
  WatchdogVerdict,
} from '@/lib/types';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {Switch} from '@/components/ui/switch';

/** 数字输入：空串 / 非法值退回 fallback（后端仍会做范围校验，这里只防 NaN）。 */
function num(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : fallback;
}

function Card({
  title,
  icon,
  action,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-[20px] border border-border p-5">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium">
          {icon}
          {title}
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}

export function WatchdogPanel() {
  const {t} = useI18n();
  const {isAdmin} = useAuth();

  const [form, setForm] = useState<WatchdogConfig | null>(null);
  const [lastRun, setLastRun] = useState<WatchdogRunResult | null>(null);
  const [lastRunAt, setLastRunAt] = useState<number | null>(null);
  const [lastError, setLastError] = useState('');
  const [egress, setEgress] = useState<WatchdogEgress | null>(null);
  const [meta, setMeta] = useState<{due: boolean; next_run_at: number | null}>({
    due: false,
    next_run_at: null,
  });
  const [mailToText, setMailToText] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [probing, setProbing] = useState(false);
  const [mailTesting, setMailTesting] = useState(false);
  const [llmTesting, setLlmTesting] = useState(false);
  const [llmSample, setLlmSample] = useState<WatchdogVerdict | null>(null);
  const [qqChecking, setQqChecking] = useState(false);
  const [qqTesting, setQqTesting] = useState(false);
  const [qqBots, setQqBots] = useState<WatchdogQqBot[] | null>(null);
  const [qqError, setQqError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const r = await watchdogApi.status();
      setForm(r.config);
      setMailToText(r.config.mail.to.join(', '));
      setLastRun(r.state.last_result ?? null);
      setLastRunAt(r.state.last_run_at ?? null);
      setLastError(r.state.last_error ?? '');
      setEgress(r.state.egress ?? null);
      setMeta({due: r.due, next_run_at: r.next_run_at});
    } catch (e) {
      setLoadError(true);
      notify.err(errText(e));
    } finally {
      setLoading(false);
    }
  }, []);

  /** 只刷新状态，**不回填表单**：用户在表单里改了一半时不该被覆盖。 */
  const refreshStatus = useCallback(async () => {
    try {
      const r = await watchdogApi.status();
      setLastRun(r.state.last_result ?? null);
      setLastRunAt(r.state.last_run_at ?? null);
      setLastError(r.state.last_error ?? '');
      setEgress(r.state.egress ?? null);
      setMeta({due: r.due, next_run_at: r.next_run_at});
    } catch (e) {
      notify.err(errText(e));
    }
  }, []);

  useEffect(() => {
    if (!isAdmin) {
      setLoading(false);
      return;
    }
    void load();
  }, [isAdmin, load]);

  const patch = (next: Partial<WatchdogConfig>) =>
    setForm((current) => (current ? {...current, ...next} : current));
  const patchMail = (next: Partial<WatchdogMailConfig>) =>
    setForm((current) =>
      current ? {...current, mail: {...current.mail, ...next}} : current);
  const patchLlm = (next: Partial<WatchdogLlmConfig>) =>
    setForm((current) =>
      current ? {...current, llm: {...current.llm, ...next}} : current);
  const patchQq = (next: Partial<WatchdogQqConfig>) =>
    setForm((current) =>
      current ? {...current, qq: {...current.qq, ...next}} : current);
  const patchTarget = (index: number, next: Partial<WatchdogTarget>) =>
    setForm((current) => {
      if (!current) return current;
      const targets = current.targets.map((item, i) =>
        i === index ? {...item, ...next} : item);
      return {...current, targets};
    });

  async function save() {
    if (!form) return;
    setSaving(true);
    try {
      const r = await watchdogApi.saveConfig({
        ...form,
        interval_minutes: num(String(form.interval_minutes), 5),
        timeout_seconds: num(String(form.timeout_seconds), 20),
        asset_limit: num(String(form.asset_limit), 4),
        targets: form.targets.map((target) => ({
          ...target,
          name: target.name.trim(),
          url: target.url.trim(),
        })),
        llm: {...form.llm, timeout_seconds: num(String(form.llm.timeout_seconds), 60)},
        mail: {
          ...form.mail,
          port: num(String(form.mail.port), 465),
          // 空串会清掉已保存的收件人；打码的密码原样回传 = 保持不变
          to: mailToText.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean),
        },
        qq: {
          ...form.qq,
          timeout_seconds: num(String(form.qq.timeout_seconds), 15),
        },
      });
      setForm(r.config);
      setMailToText(r.config.mail.to.join(', '));
      if (r.warning) notify.warn(t('watchdog.saveWarn', {warning: r.warning}));
      else notify.ok(t('watchdog.saved'));
      void refreshStatus();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  async function runNow() {
    setRunning(true);
    try {
      const r = await watchdogApi.run();
      setLastRun(r);
      if (r.skipped) notify.warn(t('watchdog.busy'));
      else notify.ok(t('watchdog.runDone', {n: (r.changed ?? []).length}));
      void refreshStatus();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setRunning(false);
    }
  }

  async function resetBaseline() {
    try {
      const r = await watchdogApi.resetBaseline();
      notify.ok(t('watchdog.resetDone', {n: r.cleared}));
      void refreshStatus();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  async function checkEgress() {
    if (!form) return;
    setProbing(true);
    try {
      // 用表单当前值：用户填完主机就会点这个按钮，不该要求先保存
      const r = await watchdogApi.checkEgress({
        host: form.mail.host.trim(),
        port: form.mail.port,
      });
      setEgress(r);
      if (r.ok) notify.ok(t('watchdog.egressOk', {host: r.host, port: r.port ?? form?.mail.port ?? ''}));
      else notify.err(t('watchdog.egressFail', {error: r.error || '-'}));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setProbing(false);
    }
  }

  async function sendTestMail() {
    if (!form) return;
    setMailTesting(true);
    try {
      const r = await watchdogApi.testMail({
        ...form.mail,
        to: mailToText.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean),
      });
      setEgress(r.egress ?? null);
      if (r.ok) notify.ok(r.message || t('watchdog.mailSent'));
      else notify.err(r.message || t('watchdog.mailNotSent'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setMailTesting(false);
    }
  }

  async function testLlm() {
    if (!form) return;
    setLlmTesting(true);
    try {
      const r = await watchdogApi.testLlm({...form.llm});
      setLlmSample(r.verdict);
      notify.ok(r.verdict.summary || t('watchdog.testLlm'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setLlmTesting(false);
    }
  }

  async function checkQq() {
    if (!form) return;
    setQqChecking(true);
    setQqError('');
    try {
      const r = await watchdogApi.qqBots({...form.qq});
      if (r.ok) {
        setQqBots(r.bots ?? []);
        notify.ok(t('watchdog.qqOk', {
          bots: (r.bots ?? []).length,
        }));
      } else {
        setQqBots(null);
        setQqError(r.error || '-');
        notify.err(t('watchdog.qqFail', {error: r.error || '-'}));
      }
    } catch (e) {
      setQqError(errText(e));
      notify.err(errText(e));
    } finally {
      setQqChecking(false);
    }
  }

  async function sendTestQq() {
    if (!form) return;
    setQqTesting(true);
    setQqError('');
    try {
      const r = await watchdogApi.testQq({...form.qq});
      if (r.ok) {
        notify.ok(t('watchdog.qqSent'));
      } else {
        setQqError(r.error || '-');
        notify.err(t('watchdog.qqNotSent'));
      }
    } catch (e) {
      setQqError(errText(e));
      notify.err(errText(e));
    } finally {
      setQqTesting(false);
    }
  }

  if (!isAdmin) {
    return (
      <div className="rounded-[20px] bg-muted p-5 text-sm text-muted-foreground">
        {t('watchdog.adminOnly')}
      </div>
    );
  }
  if (loading) {
    return (
      <div className="rounded-[20px] bg-muted p-5 text-sm text-muted-foreground">
        {t('common.loading')}
      </div>
    );
  }
  if (loadError || !form) {
    return (
      <div className="rounded-[20px] bg-muted p-5">
        <div className="text-sm text-muted-foreground">{t('watchdog.loadFailed')}</div>
        <Button variant="outline" size="sm" className="mt-3" onClick={() => void load()}>
          {t('state.retry')}
        </Button>
      </div>
    );
  }

  const busy = saving || running;
  const targetStatus = (status: string): string => {
    if (status === 'changed') return t('watchdog.targetChanged');
    if (status === 'unchanged') return t('watchdog.targetUnchanged');
    if (status === 'baseline_created') return t('watchdog.targetBaseline');
    return t('watchdog.targetError');
  };
  const statusVariant = (status: string): 'destructive' | 'secondary' | 'outline' => {
    if (status === 'changed') return 'destructive';
    if (status === 'unchanged') return 'secondary';
    return 'outline';
  };

  return (
    <div className="space-y-4">
      {/* ── 说明 ── */}
      <div className="rounded-[20px] bg-muted p-5">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium text-foreground">
          <Radar className="h-4 w-4" />
          {t('watchdog.title')}
        </div>
        <RichText text={t('watchdog.desc')} className="text-xs leading-6 text-muted-foreground" />
      </div>

      {/* ── 运行状态 + 动作 ── */}
      <Card
        title={t('watchdog.statusTitle')}
        icon={<Activity className="h-4 w-4" />}
        action={
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" disabled={busy} onClick={() => void refreshStatus()}>
              <RefreshCw className="h-3.5 w-3.5" />
              {t('common.refresh')}
            </Button>
            <Button variant="outline" size="sm" disabled={busy} onClick={() => void runNow()}>
              {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
              {running ? t('watchdog.running') : t('watchdog.runNow')}
            </Button>
            <Button size="sm" disabled={busy} onClick={() => void save()}>
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
              {t('watchdog.save')}
            </Button>
          </div>
        }
      >
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground">
          <Badge variant={form.enabled ? 'secondary' : 'outline'}>
            {form.enabled ? t('watchdog.statusOn') : t('watchdog.statusOff')}
          </Badge>
          {meta.due && <Badge variant="destructive">{t('watchdog.due')}</Badge>}
          <span>
            {t('watchdog.lastRun')}: {lastRunAt ? fmtDateTime(lastRunAt) : t('watchdog.never')}
          </span>
          <span>
            {t('watchdog.nextRun')}: {meta.next_run_at ? fmtDateTime(meta.next_run_at) : t('watchdog.never')}
          </span>
        </div>
        {lastError && <p className="mt-2 text-xs text-destructive">{lastError}</p>}
        <p className="mt-3 text-xs text-muted-foreground">{t('watchdog.savedHint')}</p>
      </Card>

      {/* ── 基础设置 ── */}
      <Card title={t('watchdog.enable')} icon={<PlugZap className="h-4 w-4" />}>
        <div className="flex flex-wrap items-center gap-3">
          <Switch
            id="wd-enabled"
            checked={form.enabled}
            disabled={!isAdmin}
            onCheckedChange={(checked) => patch({enabled: checked})}
          />
          <Label htmlFor="wd-enabled">{t('watchdog.enable')}</Label>
          <span className="text-xs text-muted-foreground">{t('watchdog.enableHint')}</span>
        </div>
        <div className="mt-4 grid gap-4 sm:grid-cols-3">
          <div>
            <Label htmlFor="wd-interval">{t('watchdog.interval')}</Label>
            <Input
              id="wd-interval"
              type="number"
              min={5}
              value={String(form.interval_minutes)}
              disabled={!isAdmin}
              onChange={(e) => patch({interval_minutes: num(e.target.value, 5)})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('watchdog.intervalHint')}</p>
          </div>
          <div>
            <Label htmlFor="wd-timeout">{t('watchdog.timeout')}</Label>
            <Input
              id="wd-timeout"
              type="number"
              min={5}
              max={120}
              value={String(form.timeout_seconds)}
              disabled={!isAdmin}
              onChange={(e) => patch({timeout_seconds: num(e.target.value, 20)})}
            />
          </div>
          <div>
            <Label htmlFor="wd-assets">{t('watchdog.assetLimit')}</Label>
            <Input
              id="wd-assets"
              type="number"
              min={0}
              max={8}
              value={String(form.asset_limit)}
              disabled={!isAdmin}
              onChange={(e) => patch({asset_limit: num(e.target.value, 4)})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('watchdog.assetLimitHint')}</p>
          </div>
          <div>
            <Label>{t('watchdog.notifyOn')}</Label>
            <Select
              value={form.notify_on}
              disabled={!isAdmin}
              onValueChange={(value) => patch({notify_on: value as WatchdogConfig['notify_on']})}
            >
              <SelectTrigger aria-label={t('watchdog.notifyOn')}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="change">{t('watchdog.notifyChange')}</SelectItem>
                <SelectItem value="claimable">{t('watchdog.notifyClaimable')}</SelectItem>
                <SelectItem value="anomaly">{t('watchdog.notifyAnomaly')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="sm:col-span-2">
            <div className="flex items-center gap-2">
              <Switch
                id="wd-scan"
                checked={form.scan_compensation}
                disabled={!isAdmin}
                onCheckedChange={(checked) => patch({scan_compensation: checked})}
              />
              <Label htmlFor="wd-scan">{t('watchdog.scanCompensation')}</Label>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">{t('watchdog.scanCompensationHint')}</p>
            <div className="mt-3 flex items-center gap-2">
              <Switch
                id="wd-auto-claim"
                checked={form.auto_claim}
                disabled={!isAdmin || !form.scan_compensation}
                onCheckedChange={(checked) => patch({auto_claim: checked})}
              />
              <Label htmlFor="wd-auto-claim">{t('watchdog.autoClaim')}</Label>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">{t('watchdog.autoClaimHint')}</p>
          </div>
        </div>
      </Card>

      {/* ── 监控目标 ── */}
      <Card
        title={t('watchdog.targets')}
        icon={<Radar className="h-4 w-4" />}
        action={
          <Button
            variant="outline"
            size="sm"
            disabled={!isAdmin}
            onClick={() => patch({
              targets: [...form.targets, {
                name: `target-${form.targets.length + 1}`,
                url: '',
                kind: 'assets',
                enabled: true,
              }],
            })}
          >
            <Plus className="h-3.5 w-3.5" />
            {t('watchdog.addTarget')}
          </Button>
        }
      >
        <p className="mb-3 text-xs text-muted-foreground">{t('watchdog.targetsHint')}</p>
        {form.targets.length === 0 ? (
          <div className="rounded-2xl bg-muted/40 p-4 text-xs text-muted-foreground">
            {t('watchdog.noTargets')}
          </div>
        ) : (
          <div className="space-y-3">
            {form.targets.map((target, index) => (
              <div key={`${target.name}-${index}`} className="space-y-2 rounded-2xl bg-muted/40 p-3">
                <div className="flex flex-wrap items-end gap-2">
                  <div className="min-w-[140px] flex-1">
                    <Label>{t('watchdog.targetName')}</Label>
                    <Input
                      value={target.name}
                      disabled={!isAdmin}
                      onChange={(e) => patchTarget(index, {name: e.target.value})}
                    />
                  </div>
                  <div className="w-[180px]">
                    <Label>{t('watchdog.targetKind')}</Label>
                    <Select
                      value={target.kind}
                      disabled={!isAdmin}
                      onValueChange={(value) =>
                        patchTarget(index, {kind: value as WatchdogTarget['kind']})}
                    >
                      <SelectTrigger aria-label={t('watchdog.targetKind')}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="assets">{t('watchdog.kindAssets')}</SelectItem>
                        <SelectItem value="html">{t('watchdog.kindHtml')}</SelectItem>
                        <SelectItem value="js">{t('watchdog.kindJs')}</SelectItem>
                        <SelectItem value="text">{t('watchdog.kindText')}</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="flex items-center gap-2 pb-2">
                    <Switch
                      aria-label={t('watchdog.targetEnabled')}
                      checked={target.enabled}
                      disabled={!isAdmin}
                      onCheckedChange={(checked) => patchTarget(index, {enabled: checked})}
                    />
                    <span className="text-xs text-muted-foreground">
                      {t('watchdog.targetEnabled')}
                    </span>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={t('watchdog.removeTarget')}
                    title={t('watchdog.removeTarget')}
                    disabled={!isAdmin}
                    onClick={() => patch({
                      targets: form.targets.filter((_, i) => i !== index),
                    })}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
                <div>
                  <Label>{t('watchdog.targetUrl')}</Label>
                  <Input
                    value={target.url}
                    disabled={!isAdmin}
                    placeholder="https://www.workbuddy.cn/profile/plans-usage"
                    onChange={(e) => patchTarget(index, {url: e.target.value})}
                  />
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* ── LLM 判读 ── */}
      <Card
        title={t('watchdog.llmTitle')}
        icon={<Cpu className="h-4 w-4" />}
        action={
          <Button
            variant="outline"
            size="sm"
            disabled={!isAdmin || llmTesting}
            onClick={() => void testLlm()}
          >
            {llmTesting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Cpu className="h-3.5 w-3.5" />}
            {t('watchdog.testLlm')}
          </Button>
        }
      >
        <div className="mb-3 flex items-center gap-2">
          <Switch
            id="wd-llm"
            checked={form.llm.enabled}
            disabled={!isAdmin}
            onCheckedChange={(checked) => patchLlm({enabled: checked})}
          />
          <Label htmlFor="wd-llm">{t('watchdog.llmEnable')}</Label>
        </div>
        <p className="mb-3 text-xs text-muted-foreground">{t('watchdog.llmDesc')}</p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Label htmlFor="wd-llm-url">{t('watchdog.llmBaseUrl')}</Label>
            <Input
              id="wd-llm-url"
              value={form.llm.base_url}
              disabled={!isAdmin}
              placeholder="https://wb.kuruomi.cloud/v1"
              onChange={(e) => patchLlm({base_url: e.target.value})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('watchdog.llmBaseUrlHint')}</p>
          </div>
          <div>
            <Label htmlFor="wd-llm-key">{t('watchdog.llmApiKey')}</Label>
            <Input
              id="wd-llm-key"
              type="password"
              autoComplete="new-password"
              value={form.llm.api_key}
              disabled={!isAdmin}
              onChange={(e) => patchLlm({api_key: e.target.value})}
            />
          </div>
          <div>
            <Label htmlFor="wd-llm-model">{t('watchdog.llmModel')}</Label>
            <Input
              id="wd-llm-model"
              value={form.llm.model}
              disabled={!isAdmin}
              placeholder="gpt-4o-mini"
              onChange={(e) => patchLlm({model: e.target.value})}
            />
          </div>
          <div>
            <Label htmlFor="wd-llm-timeout">{t('watchdog.llmTimeout')}</Label>
            <Input
              id="wd-llm-timeout"
              type="number"
              min={5}
              max={300}
              value={String(form.llm.timeout_seconds)}
              disabled={!isAdmin}
              onChange={(e) => patchLlm({timeout_seconds: num(e.target.value, 60)})}
            />
          </div>
        </div>
        {llmSample && (
          <div className="mt-3 rounded-2xl bg-muted/40 p-3 text-xs text-muted-foreground">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="secondary">
                {llmSample.source === 'llm' ? t('watchdog.sourceLlm') : t('watchdog.sourceRule')}
              </Badge>
              <span>{t('watchdog.confidence', {n: llmSample.confidence.toFixed(2)})}</span>
              {llmSample.claimable && <Badge>{t('watchdog.claimable')}</Badge>}
              {llmSample.anomaly && <Badge variant="destructive">{t('watchdog.anomaly')}</Badge>}
            </div>
            <p className="mt-2">{llmSample.summary}</p>
            {llmSample.evidence.length > 0 && (
              <ul className="mt-1 list-disc pl-5">
                {llmSample.evidence.map((line, i) => <li key={i}>{line}</li>)}
              </ul>
            )}
          </div>
        )}
      </Card>

      {/* ── 邮件告警 ── */}
      <Card
        title={t('watchdog.mailTitle')}
        icon={<Mail className="h-4 w-4" />}
        action={
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" disabled={!isAdmin || probing} onClick={() => void checkEgress()}>
              {probing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <PlugZap className="h-3.5 w-3.5" />}
              {t('watchdog.checkEgress')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={!isAdmin || mailTesting}
              onClick={() => void sendTestMail()}
            >
              {mailTesting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
              {t('watchdog.testMail')}
            </Button>
          </div>
        }
      >
        <div className="mb-3 flex items-center gap-2">
          <Switch
            id="wd-mail"
            checked={form.mail.enabled}
            disabled={!isAdmin}
            onCheckedChange={(checked) => patchMail({enabled: checked})}
          />
          <Label htmlFor="wd-mail">{t('watchdog.mailEnable')}</Label>
        </div>
        <p className="mb-1 text-xs text-muted-foreground">{t('watchdog.mailDesc')}</p>
        <p className="mb-3 text-xs text-muted-foreground">{t('watchdog.mailEnableHint')}</p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <Label htmlFor="wd-mail-host">{t('watchdog.mailHost')}</Label>
            <Input
              id="wd-mail-host"
              value={form.mail.host}
              disabled={!isAdmin}
              placeholder="smtp.qq.com"
              onChange={(e) => patchMail({host: e.target.value})}
            />
          </div>
          <div>
            <Label htmlFor="wd-mail-port">{t('watchdog.mailPort')}</Label>
            <Input
              id="wd-mail-port"
              type="number"
              min={1}
              max={65535}
              value={String(form.mail.port)}
              disabled={!isAdmin}
              onChange={(e) => {
                const port = num(e.target.value, 465);
                // 465 与 587 的加密方式是配套的：只改端口不改方式，会拿 SSL
                // 去连 587（握手即失败），错误看起来像「出口被封」。
                const security = port === 465 ? 'ssl'
                  : port === 587 ? 'starttls' : form.mail.security;
                patchMail({port, security});
              }}
            />
          </div>
          <div>
            <Label>{t('watchdog.mailSecurity')}</Label>
            <Select
              value={form.mail.security}
              disabled={!isAdmin}
              onValueChange={(value) =>
                patchMail({security: value as WatchdogMailConfig['security']})}
            >
              <SelectTrigger aria-label={t('watchdog.mailSecurity')}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="ssl">SSL（465）</SelectItem>
                <SelectItem value="starttls">STARTTLS（587）</SelectItem>
                <SelectItem value="none">none</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div>
            <Label htmlFor="wd-mail-user">{t('watchdog.mailUsername')}</Label>
            <Input
              id="wd-mail-user"
              value={form.mail.username}
              disabled={!isAdmin}
              autoComplete="username"
              onChange={(e) => patchMail({username: e.target.value})}
            />
          </div>
          <div>
            <Label htmlFor="wd-mail-pass">{t('watchdog.mailPassword')}</Label>
            <Input
              id="wd-mail-pass"
              type="password"
              autoComplete="new-password"
              value={form.mail.password}
              disabled={!isAdmin}
              onChange={(e) => patchMail({password: e.target.value})}
            />
          </div>
          <div>
            <Label htmlFor="wd-mail-from">{t('watchdog.mailFrom')}</Label>
            <Input
              id="wd-mail-from"
              value={form.mail.from}
              disabled={!isAdmin}
              placeholder="alert@example.com"
              onChange={(e) => patchMail({from: e.target.value})}
            />
          </div>
          <div className="sm:col-span-2">
            <Label htmlFor="wd-mail-to">{t('watchdog.mailTo')}</Label>
            <Input
              id="wd-mail-to"
              value={mailToText}
              disabled={!isAdmin}
              placeholder="you@example.com, ops@example.com"
              onChange={(e) => setMailToText(e.target.value)}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('watchdog.mailToHint')}</p>
          </div>
          <div className="sm:col-span-2">
            <Label htmlFor="wd-mail-prefix">{t('watchdog.mailSubjectPrefix')}</Label>
            <Input
              id="wd-mail-prefix"
              value={form.mail.subject_prefix}
              disabled={!isAdmin}
              onChange={(e) => patchMail({subject_prefix: e.target.value})}
            />
          </div>
        </div>
        {egress && (
          <div className="mt-3 rounded-2xl bg-muted/40 p-3 text-xs">
            <div className={egress.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-destructive'}>
              {egress.ok
                ? t('watchdog.egressOk', {
                  host: egress.host,
                  port: egress.port ?? form.mail.port,
                })
                : t('watchdog.egressFail', {error: egress.error || '-'})}
            </div>
            {egress.checked_at > 0 && (
              <div className="mt-1 text-muted-foreground">{fmtDateTime(egress.checked_at)}</div>
            )}
            {egress.ports.length > 0 && (
              <div className="mt-2 text-muted-foreground">
                <span>{t('watchdog.egressPorts')}：</span>
                <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
                  {egress.ports.map((probe) => (
                    <li key={probe.port}>
                      {probe.port}: {probe.ok ? t('watchdog.portOk') : t('watchdog.portFail')}
                      {probe.banner ? ` · ${probe.banner}` : ''}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </Card>

      {/* ── QQ 通知（AstrBot） ── */}
      <Card
        title={t('watchdog.qqTitle')}
        icon={<MessageCircle className="h-4 w-4" />}
        action={
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={!isAdmin || qqChecking}
              onClick={() => void checkQq()}
            >
              {qqChecking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <PlugZap className="h-3.5 w-3.5" />}
              {t('watchdog.qqCheck')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={!isAdmin || qqTesting}
              onClick={() => void sendTestQq()}
            >
              {qqTesting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
              {t('watchdog.qqTest')}
            </Button>
          </div>
        }
      >
        <div className="mb-3 flex items-center gap-2">
          <Switch
            id="wd-qq"
            checked={form.qq.enabled}
            disabled={!isAdmin}
            onCheckedChange={(checked) => patchQq({enabled: checked})}
          />
          <Label htmlFor="wd-qq">{t('watchdog.qqEnable')}</Label>
        </div>
        <p className="mb-1 text-xs text-muted-foreground">{t('watchdog.qqDesc')}</p>
        <p className="mb-3 text-xs text-muted-foreground">{t('watchdog.qqEnableHint')}</p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Label htmlFor="wd-qq-base">{t('watchdog.qqBaseUrl')}</Label>
            <Input
              id="wd-qq-base"
              value={form.qq.base_url}
              disabled={!isAdmin}
              placeholder="https://astrbot.example.com"
              onChange={(e) => patchQq({base_url: e.target.value})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('watchdog.qqBaseUrlHint')}</p>
          </div>
          <div>
            <Label htmlFor="wd-qq-key">{t('watchdog.qqApiKey')}</Label>
            <Input
              id="wd-qq-key"
              type="password"
              autoComplete="new-password"
              value={form.qq.api_key}
              disabled={!isAdmin}
              placeholder="abk_..."
              onChange={(e) => patchQq({api_key: e.target.value})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('watchdog.qqApiKeyHint')}</p>
          </div>
          <div>
            <Label htmlFor="wd-qq-timeout">{t('watchdog.qqTimeout')}</Label>
            <Input
              id="wd-qq-timeout"
              type="number"
              min={3}
              max={120}
              value={String(form.qq.timeout_seconds)}
              disabled={!isAdmin}
              onChange={(e) => patchQq({timeout_seconds: num(e.target.value, 15)})}
            />
          </div>
          <div className="sm:col-span-2">
            <Label htmlFor="wd-qq-umo">{t('watchdog.qqUmo')}</Label>
            <Input
              id="wd-qq-umo"
              value={form.qq.umo}
              disabled={!isAdmin}
              placeholder="qq_official:GroupMessage:xxxxxxxx"
              onChange={(e) => patchQq({umo: e.target.value})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('watchdog.qqUmoHint')}</p>
          </div>
        </div>
        {(qqBots !== null || qqError) && (
          <div className="mt-3 rounded-2xl bg-muted/40 p-3 text-xs">
            {qqError ? (
              <div className="text-destructive">{t('watchdog.qqFail', {error: qqError})}</div>
            ) : (
              <div className="text-emerald-600 dark:text-emerald-400">
                {t('watchdog.qqOk', {bots: (qqBots ?? []).length})}
              </div>
            )}
            {qqBots !== null && (
              <div className="mt-1 text-muted-foreground">
                <span>{t('watchdog.qqBots')}：</span>
                {qqBots.length === 0 ? (
                  <span className="ml-1">{t('watchdog.qqNoBots')}</span>
                ) : (
                  <ul className="mt-1 list-disc pl-5">
                    {qqBots.map((bot, i) => (
                      <li key={i}>
                        {String(bot.name || bot.id || bot.type || JSON.stringify(bot))}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        )}
      </Card>

      {/* ── 最近一轮 ── */}
      <Card
        title={t('watchdog.runTitle')}
        icon={<Activity className="h-4 w-4" />}
        action={
          <ConfirmDialog
            destructive
            title={t('watchdog.resetConfirmTitle')}
            description={t('watchdog.resetConfirmDesc')}
            confirmText={t('watchdog.resetConfirmOk')}
            trigger={
              <Button variant="outline" size="sm" disabled={!isAdmin}>
                {t('watchdog.resetBaseline')}
              </Button>
            }
            onConfirm={() => void resetBaseline()}
          />
        }
      >
        {!lastRun ? (
          <div className="rounded-2xl bg-muted/40 p-4 text-xs text-muted-foreground">
            {t('watchdog.noRun')}
          </div>
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Badge variant="outline">
                {lastRun.trigger === 'schedule'
                  ? t('watchdog.triggerSchedule')
                  : t('watchdog.triggerManual')}
              </Badge>
              {lastRun.finished_at && <span>{fmtDateTime(lastRun.finished_at)}</span>}
              {lastRun.mail && (
                <Badge variant={lastRun.mail.ok ? 'secondary' : 'outline'}>
                  {lastRun.mail.ok ? t('watchdog.mailSent') : t('watchdog.mailNotSent')}
                </Badge>
              )}
              {lastRun.qq && (
                <Badge variant={lastRun.qq.ok ? 'secondary' : 'outline'}>
                  {lastRun.qq.ok ? t('watchdog.qqSent') : t('watchdog.qqNotSent')}
                </Badge>
              )}
            </div>
            {(lastRun.targets ?? []).length === 0 && (
              <div className="text-xs text-muted-foreground">{t('watchdog.noChanges')}</div>
            )}
            {(lastRun.targets ?? []).map((target) => (
              <div key={target.name} className="rounded-2xl bg-muted/40 p-3">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-medium">{target.name}</span>
                  <Badge variant={statusVariant(target.status)}>
                    {targetStatus(target.status)}
                  </Badge>
                  {target.diff && (
                    <span className="text-xs text-muted-foreground">
                      {t('watchdog.diffStat', {a: target.diff.added, r: target.diff.removed})}
                    </span>
                  )}
                  {target.verdict && (
                    <span className="text-xs text-muted-foreground">{target.verdict.summary}</span>
                  )}
                </div>
                {target.verdict && (
                  <div className="mt-2 space-y-1 text-xs text-muted-foreground">
                    <div className="flex flex-wrap items-center gap-2">
                      <span>
                        {target.verdict.source === 'llm'
                          ? t('watchdog.sourceLlm')
                          : t('watchdog.sourceRule')}
                      </span>
                      <span>{t('watchdog.confidence', {n: target.verdict.confidence.toFixed(2)})}</span>
                      {target.verdict.claimable && <Badge>{t('watchdog.claimable')}</Badge>}
                      {target.verdict.anomaly && (
                        <Badge variant="destructive">{t('watchdog.anomaly')}</Badge>
                      )}
                    </div>
                    {target.verdict.evidence.length > 0 && (
                      <div>
                        <span className="font-medium">{t('watchdog.evidence')}：</span>
                        <ul className="mt-1 list-disc pl-5">
                          {target.verdict.evidence.map((line, i) => <li key={i}>{line}</li>)}
                        </ul>
                      </div>
                    )}
                    {target.verdict.actions.length > 0 && (
                      <div>
                        <span className="font-medium">{t('watchdog.actions')}：</span>
                        <ul className="mt-1 list-disc pl-5">
                          {target.verdict.actions.map((line, i) => <li key={i}>{line}</li>)}
                        </ul>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
            {lastRun.scan && (
              <div className="text-xs text-muted-foreground">
                {lastRun.scan.ok
                  ? t('watchdog.scanResult', {
                    claimable: lastRun.scan.claimable ?? 0,
                    already: lastRun.scan.already ?? 0,
                    unknown: lastRun.scan.unknown ?? 0,
                    failed: lastRun.scan.failed ?? 0,
                  })
                  : `${lastRun.scan.error || ''}`}
              </div>
            )}
            {(lastRun.errors ?? []).length > 0 && (
              <ul className="text-xs text-destructive">
                {(lastRun.errors ?? []).map((item) => (
                  <li key={item.name}>{item.name}: {item.error}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        <p className="mt-3 text-xs text-muted-foreground">{t('watchdog.resetBaselineHint')}</p>
      </Card>
    </div>
  );
}

'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {CalendarClock, Loader2, Play, Plus, RotateCcw, Save, TriangleAlert, X} from 'lucide-react';
import {accountApi, errText, settingsApi} from '@/lib/api';
import type {
  DailyPlan,
  DailyTask,
  DailyTaskConfig,
  RefreshPlan,
  TaskSchedulerStatus,
  UpstreamConfig,
} from '@/lib/types';
import {
  ACTIVITY_REPORT_COUNT_MAX,
  ACTIVITY_REPORT_COUNT_MIN,
  activityReportCountInRange,
  defaultUpstreamSchedule,
  diffUpstreamSchedule,
  isUpstreamScheduleDirty,
  pickUpstreamSchedule,
  type UpstreamScheduleDraft,
} from '@/lib/upstream-schedule';
import {scheduleDelayError} from '@/lib/schedule-validation';
import {fmtDateTime} from '@/lib/format';
import {notify} from '@/lib/toast';
import {useI18n, useT} from '@/lib/i18n/provider';
import {UpstreamReloadNotice} from '@/components/common/settings/UpstreamReloadNotice';
import {Button} from '@/components/ui/button';
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

/** 0-23 整点下拉项 */
const HOURS = Array.from({length: 24}, (_, h) => h);
const REPORT_COUNTS = [1, 2, 3, 5, 10, 15, 20, 25, 30, 40, 50];
const RANDOM_DELAY_OPTIONS = [0, 5, 10, 15, 20, 30, 45, 60];
const ACCOUNT_DELAY_OPTIONS = [0, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300, 600];

/** 面板原生每日任务定义（保留 key 与 i18n 键兼容既有测试与逻辑） */
const DAILY_TASKS: {key: DailyTask; labelKey: string; noteKey: string; warn?: boolean}[] = [
  {key: 'checkin', labelKey: 'accounts.dailyCheckin', noteKey: 'accounts.dailyCheckinNote'},
  {key: 'compensation', labelKey: 'accounts.dailyCompensation',
   noteKey: 'accounts.dailyCompensationNote'},
  {key: 'claim', labelKey: 'accounts.dailyClaim', noteKey: 'accounts.dailyClaimNote'},
  {key: 'full', labelKey: 'accounts.dailyFull', noteKey: 'accounts.dailyFullWarn', warn: true},
];

function withCurrentOption(presets: number[], current: number): number[] {
  return presets.includes(current) ? presets : [...presets, current].sort((a, b) => a - b);
}

function HourSelect({
  value,
  onChange,
  ariaLabel,
  disabled,
}: {
  value: number;
  onChange: (h: number) => void;
  ariaLabel: string;
  disabled?: boolean;
}) {
  return (
    <Select value={String(value)} disabled={disabled} onValueChange={(v) => onChange(Number(v))}>
      <SelectTrigger className="h-8 w-24 bg-background" aria-label={ariaLabel}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {HOURS.map((h) => (
          <SelectItem key={h} value={String(h)}>
            {String(h).padStart(2, '0')}:00
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/**
 * 多时刻选择：每个已有时刻一个下拉 + 删除按钮，另有「添加时刻」下拉。
 *
 * 不能用「单时刻 / 双时刻」下拉硬套：旧设置页允许填任意多个整点（如 [8,12,18,22]），
 * 只渲染前 1-2 个会在用户改第一个时刻时把剩下的整段写回成 `[h]`，等于静默丢掉排程。
 * 这里完整渲染全部时刻，只有用户点删除或改某个下拉时才动那一个值。
 */
function HoursMultiSelect({
  hours,
  onChange,
  ariaLabel,
  disabled,
}: {
  hours: number[];
  onChange: (next: number[]) => void;
  ariaLabel: string;
  disabled?: boolean;
}) {
  const t = useT();
  const list = Array.from(new Set(hours.filter((h) => Number.isInteger(h) && h >= 0 && h <= 23)))
    .sort((a, b) => a - b);
  // 脏数据兜底：cleanHours 已保证非空，真为空时给一个可编辑的起点而不是崩掉。
  const shown = list.length ? list : [9];
  const remaining = HOURS.filter((h) => !shown.includes(h));

  const replaceAt = (idx: number, h: number) => {
    const next = [...shown];
    next[idx] = h;
    onChange(Array.from(new Set(next)).sort((a, b) => a - b));
  };
  const removeAt = (idx: number) => {
    if (shown.length <= 1) return;
    onChange(shown.filter((_, i) => i !== idx));
  };
  const add = (h: number) => {
    onChange([...shown, h].sort((a, b) => a - b));
  };

  return (
    <div className="flex flex-wrap items-center gap-1">
      {shown.map((h, idx) => (
        <span key={`hour-${idx}`} className="flex items-center gap-0.5">
          <HourSelect
            value={h}
            onChange={(v) => replaceAt(idx, v)}
            ariaLabel={`${ariaLabel} ${idx + 1}`}
            disabled={disabled}
          />
          {shown.length > 1 && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-7 text-muted-foreground"
              aria-label={`${t('tasks.removeHour')} ${String(h).padStart(2, '0')}:00`}
              disabled={disabled}
              onClick={() => removeAt(idx)}
            >
              <X className="size-3.5" />
            </Button>
          )}
        </span>
      ))}
      {remaining.length > 0 && (
        <Select value="" disabled={disabled} onValueChange={(v) => add(Number(v))}>
          <SelectTrigger className="h-8 w-11 bg-background" aria-label={t('tasks.addHour')}>
            <SelectValue placeholder={<Plus className="size-3.5" />} />
          </SelectTrigger>
          <SelectContent>
            {remaining.map((h) => (
              <SelectItem key={h} value={String(h)}>
                {String(h).padStart(2, '0')}:00
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </div>
  );
}

function UnitSelect({
  value,
  options,
  unit,
  ariaLabel,
  disabled,
  onChange,
}: {
  value: number;
  options: number[];
  unit: string;
  ariaLabel: string;
  disabled?: boolean;
  onChange: (n: number) => void;
}) {
  const merged = withCurrentOption(options, value);
  return (
    <Select value={String(value)} disabled={disabled} onValueChange={(v) => onChange(Number(v))}>
      <SelectTrigger className="h-8 w-24 bg-background tabular-nums" aria-label={ariaLabel}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {merged.map((n) => (
          <SelectItem key={n} value={String(n)}>
            {n} {unit}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/** 统一任务行组件：面板原生任务与上游排程任务完全共用同一套视觉与交互结构 */
function UnifiedTaskRow({
  checked,
  disabled,
  onCheckedChange,
  label,
  description,
  warn,
  ranToday,
  controls,
  runBusy,
  runDisabled,
  runTitle,
  onRunNow,
}: {
  checked: boolean;
  disabled?: boolean;
  onCheckedChange: (v: boolean) => void;
  label: string;
  description: string;
  warn?: boolean;
  ranToday?: boolean;
  controls: React.ReactNode;
  runBusy?: boolean;
  runDisabled?: boolean;
  runTitle?: string;
  onRunNow?: () => void;
}) {
  const t = useT();
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
      <Switch checked={checked} disabled={disabled} onCheckedChange={onCheckedChange} aria-label={label} />
      <div className="min-w-[9rem] flex-1">
        <div className="flex items-center gap-1.5 text-xs">
          {warn && <TriangleAlert className="h-3.5 w-3.5 shrink-0 text-amber-500" />}
          <span className={'font-medium ' + (warn ? 'text-amber-600 dark:text-amber-400' : '')}>
            {label}
          </span>
          {ranToday && (
            <span className="rounded-full bg-muted-foreground/10 px-1.5 text-[11px] text-muted-foreground">
              {t('tasks.dailyRan')}
            </span>
          )}
        </div>
        <div
          className={
            'text-[11px] leading-4 ' +
            (warn ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground')
          }
        >
          {description}
        </div>
      </div>
      {controls}
      <Button
        size="sm"
        variant="outline"
        className="rounded-full"
        disabled={runDisabled}
        title={runTitle}
        onClick={onRunNow}
      >
        {runBusy ? <Loader2 className="animate-spin" /> : <Play />}
        {t('tasks.planRunNow')}
      </Button>
    </div>
  );
}

/**
 * 计划任务卡片：令牌刷新 + 每日任务（整合面板任务与上游排程任务）+ 底部防风控高级设置。
 */
export function RefreshPlanCard({onRan}: {onRan?: () => void}) {
  const t = useT();
  return (
    <section className="space-y-3 rounded-[20px] bg-muted px-3.5 py-3">
      <div className="flex items-center gap-2">
        <CalendarClock className="h-4 w-4 shrink-0 text-muted-foreground" />
        <div className="text-sm font-medium">{t('tasks.planCardTitle')}</div>
      </div>
      <UpstreamReloadNotice />
      <TokenPlanSection onRan={onRan} />
      <div className="border-t border-border/60 pt-3">
        <DailyTasksSection onRan={onRan} />
      </div>
    </section>
  );
}

/** 令牌刷新计划：每 N 天自动全量续期一次所有账号。 */
function TokenPlanSection({onRan}: {onRan?: () => void}) {
  const t = useT();
  const [plan, setPlan] = useState<RefreshPlan | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [days, setDays] = useState(5);
  const [hour, setHour] = useState(3);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);

  const apply = useCallback((p: RefreshPlan) => {
    setPlan(p);
    setEnabled(p.enabled);
    setDays(p.interval_days);
    setHour(p.hour);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      apply(await accountApi.refreshPlan());
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setLoading(false);
    }
  }, [apply]);

  useEffect(() => {
    load();
  }, [load]);

  const dirty =
    !!plan &&
    (plan.enabled !== enabled || plan.interval_days !== days || plan.hour !== hour);

  const save = useCallback(async () => {
    setSaving(true);
    try {
      apply(await accountApi.saveRefreshPlan(enabled, days, hour));
      notify.ok(t('tasks.planSaved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }, [apply, enabled, days, hour, t]);

  const runNow = useCallback(async () => {
    setRunning(true);
    try {
      const r = await accountApi.runRefreshPlan();
      const detail = t('tasks.planRunDetail', {ok: r.renewed, total: r.checked});
      if (r.failed === 0) {
        notify.ok(t('tasks.planRunDone'), detail);
      } else {
        notify.warn(t('tasks.planRunPartial', {failed: r.failed}), detail);
      }
      await load();
      onRan?.();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setRunning(false);
    }
  }, [load, onRan, t]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-muted-foreground" aria-busy="true">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        {t('common.loading')}
      </div>
    );
  }

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <Switch checked={enabled} onCheckedChange={setEnabled}
                  aria-label={t('tasks.planTokenSection')} />
          <div className="min-w-0">
            <div className="text-xs font-medium">{t('tasks.planTokenSection')}</div>
            <div className="text-[11px] text-muted-foreground">{t('tasks.planDesc')}</div>
          </div>
        </div>
      </div>
      <div className="mt-1.5 text-[11px] leading-4 text-muted-foreground">
        {t('tasks.keepaliveHint')}
      </div>

      <div className="mt-2.5 flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <Label className="text-[11px] text-muted-foreground">{t('tasks.planInterval')}</Label>
          <div className="flex items-center gap-1.5">
            <Input type="number" min={1} max={365} value={days}
                   onChange={(e) => setDays(Number(e.target.value))}
                   className="h-8 w-20 bg-background" aria-label={t('tasks.planInterval')} />
            <span className="text-xs text-muted-foreground">{t('tasks.planDays')}</span>
          </div>
        </div>
        <div className="space-y-1">
          <Label className="text-[11px] text-muted-foreground">{t('tasks.planHour')}</Label>
          <HourSelect value={hour} onChange={setHour} ariaLabel={t('tasks.planHour')} />
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" className="rounded-full"
                  disabled={!dirty || saving} onClick={save}>
            {saving ? <Loader2 className="animate-spin" /> : <Save />}
            {t('common.save')}
          </Button>
          <Button size="sm" variant="outline" className="rounded-full"
                  disabled={running} onClick={runNow} title={t('tasks.planRunNowTitle')}>
            {running ? <Loader2 className="animate-spin" /> : <Play />}
            {t('tasks.planRunNow')}
          </Button>
        </div>
      </div>

      <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span>
          {t('tasks.planLast')}：
          {plan?.last_run ? fmtDateTime(plan.last_run) : t('tasks.planNever')}
        </span>
        <span>
          {t('tasks.planNext')}：
          {plan?.next_run ? fmtDateTime(plan.next_run) : t('tasks.planDisabled')}
        </span>
      </div>
    </div>
  );
}

function draftOf(p: DailyPlan): DailyPlan {
  const out = {} as DailyPlan;
  for (const {key} of DAILY_TASKS) out[key] = {...p[key]};
  return out;
}

/**
 * 统一每日任务 + 底部高级设置（防风控错峰）：
 * - 签到合并为单一「每日签到」入口（按当前激活侧自动绑定并消除重复）
 * - 猫猫旅行 / 活跃上报 / 自动保活 / 开学季任务 / 夜猫任务与原生每日任务完全共用 UnifiedTaskRow
 * - 防风控随机延迟与账号间隔独立置于面板最底部「高级设置」，共用单一「保存」按钮
 */
function DailyTasksSection({onRan}: {onRan?: () => void}) {
  const {t, tp} = useI18n();
  const [plan, setPlan] = useState<DailyPlan | null>(null);
  const [draft, setDraft] = useState<DailyPlan | null>(null);
  const [upCfg, setUpCfg] = useState<UpstreamConfig | null>(null);
  const [upError, setUpError] = useState<string | null>(null);
  const [upDraft, setUpDraft] = useState<UpstreamScheduleDraft>(defaultUpstreamSchedule);
  const upOrig = useRef<UpstreamScheduleDraft>(defaultUpstreamSchedule());
  /** 签到归属侧：面板已启用时走 panel，否则若上游自动签到开启则沿用 upstream */
  const [checkinSide, setCheckinSide] = useState<'panel' | 'upstream'>('panel');
  const [sched, setSched] = useState<TaskSchedulerStatus | null>(null);
  const [applying, setApplying] = useState<'upstream' | 'panel' | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState<DailyTask | 'keepalive' | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setUpError(null);
    try {
      const [p, uRes, s] = await Promise.all([
        accountApi.dailyPlan(),
        settingsApi.upstream().then(
          (v) => ({ok: true as const, value: v}),
          (e) => ({ok: false as const, error: errText(e)}),
        ),
        // 调度冲突是辅助信息：取不到时不能让整个每日任务面板变成错误态。
        accountApi.taskScheduler().catch(() => null),
      ]);
      setPlan(p);
      setDraft(draftOf(p));
      setSched(s);
      if (uRes.ok) {
        setUpCfg(uRes.value);
        if (uRes.value.available !== false) {
          const picked = pickUpstreamSchedule(uRes.value.schedule);
          setUpDraft(picked);
          upOrig.current = {
            ...picked,
            checkin_hours: [...picked.checkin_hours],
            travel_hours: [...picked.travel_hours],
            activity_hours: [...picked.activity_hours],
            keepalive_hours: [...picked.keepalive_hours],
            school_hours: [...picked.school_hours],
            cat_hours: [...picked.cat_hours],
          };
          setCheckinSide(!p.checkin.enabled && picked.checkin_enabled ? 'upstream' : 'panel');
        } else {
          setCheckinSide('panel');
        }
      } else {
        setUpCfg(null);
        setUpError(uRes.error);
        setCheckinSide('panel');
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const upReady = !!upCfg && upCfg.available !== false && !upError;

  const patchPanel = useCallback((key: DailyTask, next: Partial<DailyTaskConfig>) => {
    setDraft((d) => (d ? {...d, [key]: {...d[key], ...next}} : d));
  }, []);

  const patchUp = useCallback(<K extends keyof UpstreamScheduleDraft>(
    key: K,
    val: UpstreamScheduleDraft[K],
  ) => {
    setUpDraft((prev) => ({...prev, [key]: val}));
  }, []);

  // 单一「每日签到」开关与时刻绑定：保持底层原有绑定，并在双开重叠时自动收敛为单套
  const checkinEnabled = checkinSide === 'upstream' && upReady
    ? upDraft.checkin_enabled
    : Boolean(draft?.checkin.enabled);

  const toggleCheckin = useCallback((next: boolean) => {
    if (checkinSide === 'upstream' && upReady) {
      patchUp('checkin_enabled', next);
    } else {
      patchPanel('checkin', {enabled: next});
      if (next && upReady && upDraft.checkin_enabled) {
        patchUp('checkin_enabled', false);
      }
    }
  }, [checkinSide, patchPanel, patchUp, upDraft.checkin_enabled, upReady]);

  const panelDirty =
    !!plan && !!draft &&
    DAILY_TASKS.some(({key}) =>
      plan[key].enabled !== draft[key].enabled || plan[key].hour !== draft[key].hour);
  const upDirty = upReady && isUpstreamScheduleDirty(upDraft, upOrig.current);
  const dirty = panelDirty || upDirty;

  const delayErr = upReady ? scheduleDelayError({
    random_delay_minutes: upDraft.random_delay_minutes,
    account_delay_min_seconds: upDraft.account_delay_min_seconds,
    account_delay_max_seconds: upDraft.account_delay_max_seconds,
  }) : undefined;

  const revert = useCallback(() => {
    if (plan) setDraft(draftOf(plan));
    if (upReady) {
      const o = upOrig.current;
      setUpDraft({
        ...o,
        checkin_hours: [...o.checkin_hours],
        travel_hours: [...o.travel_hours],
        activity_hours: [...o.activity_hours],
        keepalive_hours: [...o.keepalive_hours],
        school_hours: [...o.school_hours],
        cat_hours: [...o.cat_hours],
      });
    }
  }, [plan, upReady]);

  /**
   * 一键收敛两套调度（issue #375）：`upstream` 关掉面板的签到/领取补偿，
   * `panel` 打开面板的这两项并关掉上游 checkin/activity。两套同时跑虽然幂等，
   * 但会让账号多一次写请求、也让「谁在跑」变得说不清，所以保留显式入口。
   */
  const applyKeep = useCallback(async (keep: 'upstream' | 'panel') => {
    setApplying(keep);
    try {
      const r = await accountApi.keepTaskScheduler(keep);
      setPlan(r.plan);
      setDraft(draftOf(r.plan));
      setSched(r.scheduler);
      // keep 会同时改上游 config（checkin/activity 开关），必须重取上游草稿，
      // 否则界面上的签到归属侧与上游开关会停在旧状态。
      await load();
      notify.ok(t(keep === 'upstream'
        ? 'tasks.schedulerKeptUpstream'
        : 'tasks.schedulerKeptPanel'));
      if (r.reload_hint) notify.warn(t('tasks.schedulerReloadPending'), r.reload_hint);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setApplying(null);
    }
  }, [load, t]);

  const saveAll = useCallback(async () => {
    if (!draft) return;
    // 延迟越界必须在提交前拦住：diff 只负责生成稀疏 patch，不做业务校验。
    if (upDirty && delayErr) {
      notify.err(tp(delayErr));
      return;
    }
    setSaving(true);
    try {
      let reloadHint: string | undefined;
      if (panelDirty) {
        await accountApi.saveDailyPlan(draft);
      }
      if (upDirty) {
        const patch = diffUpstreamSchedule(upDraft, upOrig.current);
        if (Object.keys(patch).length > 0) {
          const saved = await settingsApi.saveUpstream({schedule: patch});
          reloadHint = saved?.reload_hint;
        }
      }
      await load();
      if (reloadHint) {
        notify.warn(t('settings.savedManualStep'), reloadHint);
      } else if (upDirty) {
        notify.ok(t('settings.saved'), t('settings.applying'));
      } else {
        notify.ok(t('tasks.dailySaved'));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }, [draft, delayErr, load, panelDirty, t, tp, upDirty, upDraft]);

  const runNow = useCallback(async (task: DailyTask) => {
    setRunning(task);
    try {
      const r = await accountApi.runDailyTask(task);
      if (r.ok) {
        notify.ok(t('tasks.dailyRunDone'), r.message);
      } else {
        notify.warn(t('tasks.dailyRunFailed'), r.message);
      }
      await load();
      onRan?.();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setRunning(null);
    }
  }, [load, onRan, t]);

  const runKeepaliveNow = useCallback(async () => {
    setRunning('keepalive');
    try {
      const r = await accountApi.runRefreshPlan();
      const detail = t('tasks.planRunDetail', {ok: r.renewed, total: r.checked});
      if (r.failed === 0) {
        notify.ok(t('tasks.planRunDone'), detail);
      } else {
        notify.warn(t('tasks.planRunPartial', {failed: r.failed}), detail);
      }
      onRan?.();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setRunning(null);
    }
  }, [onRan, t]);

  if (loading || !draft) {
    return (
      <div className="flex items-center gap-2 text-xs text-muted-foreground" aria-busy="true">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        {t('common.loading')}
      </div>
    );
  }

  const today = new Date().toLocaleDateString('sv-SE');
  const unreadableMsg = upError || (upCfg && upCfg.available === false ? upCfg.error : undefined);

  return (
    <div className="space-y-3">
      <div>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-xs font-medium">{t('tasks.dailyTitle')}</div>
          <div className="text-[11px] text-muted-foreground">{t('tasks.dailyHint')}</div>
        </div>

        {sched && sched.conflicts.length > 0 && (
          <div className="mt-2 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-[11px] leading-5 text-amber-700 dark:text-amber-300">
            <div className="flex items-center gap-1.5 font-medium">
              <TriangleAlert className="h-3.5 w-3.5" />
              {t('tasks.schedulerConflictTitle')}
            </div>
            <div className="mt-1">
              {t('tasks.schedulerConflictDesc', {
                tasks: sched.conflicts
                  .map((k) => t(k === 'checkin' ? 'accounts.dailyCheckin' : 'accounts.dailyCompensation'))
                  .join(' / '),
              })}
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button size="sm" variant="outline" className="rounded-full"
                      disabled={applying !== null}
                      onClick={() => void applyKeep('upstream')}>
                {applying === 'upstream' ? <Loader2 className="animate-spin" /> : null}
                {t('tasks.schedulerKeepUpstream')}
              </Button>
              <Button size="sm" variant="outline" className="rounded-full"
                      disabled={applying !== null}
                      onClick={() => void applyKeep('panel')}>
                {applying === 'panel' ? <Loader2 className="animate-spin" /> : null}
                {t('tasks.schedulerKeepPanel')}
              </Button>
            </div>
          </div>
        )}

        <div className="mt-2.5 space-y-2.5">
          {/* 1. 统一每日签到（不重复渲染第二个签到项） */}
          <UnifiedTaskRow
            checked={checkinEnabled}
            onCheckedChange={toggleCheckin}
            label={t('accounts.dailyCheckin')}
            description={t('accounts.dailyCheckinNote')}
            ranToday={(plan?.checkin.last_run || '').startsWith(today)}
            controls={
              checkinSide === 'upstream' && upReady ? (
                <HoursMultiSelect
                  hours={upDraft.checkin_hours}
                  onChange={(next) => patchUp('checkin_hours', next)}
                  ariaLabel={t('accounts.dailyCheckin')}
                />
              ) : (
                <HourSelect
                  value={draft.checkin.hour}
                  onChange={(h) => patchPanel('checkin', {hour: h})}
                  ariaLabel={t('accounts.dailyCheckin')}
                />
              )
            }
            runBusy={running === 'checkin'}
            runDisabled={running !== null}
            onRunNow={() => void runNow('checkin')}
          />

          {/* 2-4. 面板原生每日任务：领补偿 / 领奖 / 做任务 */}
          {DAILY_TASKS.filter((item) => item.key !== 'checkin').map(({key, labelKey, noteKey, warn}) => {
            const cfg = draft[key];
            const ranToday = (plan?.[key].last_run || '').startsWith(today);
            return (
              <UnifiedTaskRow
                key={key}
                checked={cfg.enabled}
                onCheckedChange={(v) => patchPanel(key, {enabled: v})}
                label={t(labelKey)}
                description={t(noteKey)}
                warn={warn}
                ranToday={ranToday}
                controls={
                  <HourSelect
                    value={cfg.hour}
                    onChange={(h) => patchPanel(key, {hour: h})}
                    ariaLabel={t(labelKey)}
                  />
                }
                runBusy={running === key}
                runDisabled={running !== null}
                onRunNow={() => void runNow(key)}
              />
            );
          })}

          {/* 5-9. 上游内置任务：完全复用同一 UnifiedTaskRow 组件与下拉控件 */}
          {upReady ? (
            <>
              <UnifiedTaskRow
                checked={upDraft.travel_enabled}
                onCheckedChange={(v) => patchUp('travel_enabled', v)}
                label={tp('猫猫旅行')}
                description={tp('自动推进「猫猫旅行」：领养 / 派出 / 领取到站奖励，可获得积分')}
                controls={
                  <HoursMultiSelect
                    hours={upDraft.travel_hours}
                    onChange={(next) => patchUp('travel_hours', next)}
                    ariaLabel={tp('旅行时刻')}
                  />
                }
                runDisabled
                runTitle={t('tasks.scheduledOnlyTip')}
              />

              <UnifiedTaskRow
                checked={upDraft.activity_enabled}
                onCheckedChange={(v) => patchUp('activity_enabled', v)}
                label={tp('活跃上报')}
                description={tp('每日上报一次对话活跃，点亮连续登录并解锁领养前置任务（领猫需要）')}
                controls={
                  <>
                    <UnitSelect
                      value={upDraft.activity_report_count}
                      options={REPORT_COUNTS}
                      unit={tp('条')}
                      ariaLabel={tp('每次上报条数')}
                      onChange={(n) => patchUp('activity_report_count', n)}
                    />
                    {!activityReportCountInRange(upDraft.activity_report_count) && (
                      <span className="text-[11px] text-amber-600 dark:text-amber-400">
                        {t('tasks.reportCountOutOfRange', {
                          min: ACTIVITY_REPORT_COUNT_MIN,
                          max: ACTIVITY_REPORT_COUNT_MAX,
                        })}
                      </span>
                    )}
                    <HoursMultiSelect
                      hours={upDraft.activity_hours}
                      onChange={(next) => patchUp('activity_hours', next)}
                      ariaLabel={tp('上报时刻')}
                    />
                  </>
                }
                runDisabled
                runTitle={t('tasks.scheduledOnlyTip')}
              />

              <UnifiedTaskRow
                checked={upDraft.keepalive_enabled}
                onCheckedChange={(v) => patchUp('keepalive_enabled', v)}
                label={tp('自动保活')}
                description={tp('定期刷新登录令牌，避免账号因长期闲置掉线')}
                controls={
                  <HoursMultiSelect
                    hours={upDraft.keepalive_hours}
                    onChange={(next) => patchUp('keepalive_hours', next)}
                    ariaLabel={tp('保活时刻')}
                  />
                }
                runBusy={running === 'keepalive'}
                runDisabled={running !== null}
                runTitle={t('tasks.planRunNowTitle')}
                onRunNow={() => void runKeepaliveNow()}
              />

              <UnifiedTaskRow
                checked={upDraft.school_enabled}
                onCheckedChange={(v) => patchUp('school_enabled', v)}
                label={tp('开学季任务')}
                description={tp('自动完成开学季任务中心的活动并抽空抽奖余额（仅国内版；国际版由脚本内部跳过）')}
                controls={
                  <HoursMultiSelect
                    hours={upDraft.school_hours}
                    onChange={(next) => patchUp('school_hours', next)}
                    ariaLabel={tp('开学季时刻')}
                  />
                }
                runDisabled
                runTitle={t('tasks.scheduledOnlyTip')}
              />

              <UnifiedTaskRow
                checked={upDraft.cat_enabled}
                onCheckedChange={(v) => patchUp('cat_enabled', v)}
                label={tp('夜猫任务')}
                description={tp('在夜猫窗口（23:00–08:00）补做一次夜猫任务；窗口外会自动跳过')}
                controls={
                  <HoursMultiSelect
                    hours={upDraft.cat_hours}
                    onChange={(next) => patchUp('cat_hours', next)}
                    ariaLabel={tp('夜猫时刻')}
                  />
                }
                runDisabled
                runTitle={t('tasks.scheduledOnlyTip')}
              />
            </>
          ) : unreadableMsg ? (
            <div className="flex flex-wrap items-center gap-2 text-[11px] text-amber-600 dark:text-amber-400">
              <TriangleAlert className="h-3.5 w-3.5 shrink-0" />
              <span>{t('settings.cfgUnreadable')}：{unreadableMsg}</span>
              <Button size="sm" variant="ghost" className="h-6 rounded-full px-2 text-[11px]" onClick={() => void load()}>
                {t('state.retry')}
              </Button>
            </div>
          ) : null}
        </div>
      </div>

      {/* 底部独立「高级设置」区域：防风控随机延迟与账号间隔（与任务列表清晰区分） */}
      {upReady && (
        <div className="border-t border-border/60 pt-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs font-medium">{t('tasks.advancedPacingTitle')}</div>
            <div className="text-[11px] text-muted-foreground">{t('tasks.advancedPacingHint')}</div>
          </div>
          <div className="mt-2.5 flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label className="text-[11px] text-muted-foreground">{tp('每日任务随机延迟')}</Label>
              <UnitSelect
                value={upDraft.random_delay_minutes}
                options={RANDOM_DELAY_OPTIONS}
                unit={tp('分钟')}
                ariaLabel={tp('每日任务随机延迟')}
                onChange={(n) => patchUp('random_delay_minutes', n)}
              />
            </div>
            <div className="space-y-1">
              <Label className="text-[11px] text-muted-foreground">{tp('账号最短间隔')}</Label>
              <UnitSelect
                value={upDraft.account_delay_min_seconds}
                options={ACCOUNT_DELAY_OPTIONS}
                unit={tp('秒')}
                ariaLabel={tp('账号最短间隔')}
                onChange={(n) => patchUp('account_delay_min_seconds', n)}
              />
            </div>
            <div className="space-y-1">
              <Label className="text-[11px] text-muted-foreground">{tp('账号最长间隔')}</Label>
              <UnitSelect
                value={upDraft.account_delay_max_seconds}
                options={ACCOUNT_DELAY_OPTIONS}
                unit={tp('秒')}
                ariaLabel={tp('账号最长间隔')}
                onChange={(n) => patchUp('account_delay_max_seconds', n)}
              />
            </div>
          </div>
          {delayErr && (
            <div className="mt-1.5 text-[11px] text-destructive">{tp(delayErr)}</div>
          )}
        </div>
      )}

      {/* 全局唯一保存入口 */}
      <div className="flex items-center gap-2 pt-0.5">
        <Button size="sm" variant={dirty ? 'default' : 'outline'} className="rounded-full"
                disabled={!dirty || saving || !!delayErr} onClick={saveAll}>
          {saving ? <Loader2 className="animate-spin" /> : <Save />}
          {t('common.save')}
        </Button>
        {dirty && (
          <Button size="sm" variant="ghost" className="rounded-full text-muted-foreground"
                  disabled={saving} onClick={revert}>
            <RotateCcw className="h-3.5 w-3.5" />
            {t('settings.revert')}
          </Button>
        )}
      </div>
    </div>
  );
}

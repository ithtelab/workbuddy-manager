'use client';

import {useCallback, useEffect, useState} from 'react';
import {CalendarClock, Loader2, Play, Save, TriangleAlert} from 'lucide-react';
import {accountApi, errText} from '@/lib/api';
import type {DailyPlan, DailyTask, DailyTaskConfig, RefreshPlan, TaskSchedulerStatus} from '@/lib/types';
import {fmtDateTime} from '@/lib/format';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
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

/** 每日任务的展示顺序与文案键；`warn` 的带风险图标与红字提示 */
const DAILY_TASKS: {key: DailyTask; labelKey: string; noteKey: string; warn?: boolean}[] = [
  {key: 'checkin', labelKey: 'accounts.dailyCheckin', noteKey: 'accounts.dailyCheckinNote'},
  // 顺序与后端 dailyplan.TASKS 一致（计划里的顺序即界面顺序）。
  // compensation 默认**关**：上游的活跃上报（默认开）每天已经在领同一笔补偿，
  // 这一行的主要用法是点「立即执行」**马上领一次**（补偿公告带截止日期）。
  {key: 'compensation', labelKey: 'accounts.dailyCompensation',
   noteKey: 'accounts.dailyCompensationNote'},
  {key: 'claim', labelKey: 'accounts.dailyClaim', noteKey: 'accounts.dailyClaimNote'},
  {key: 'full', labelKey: 'accounts.dailyFull', noteKey: 'accounts.dailyFullWarn', warn: true},
];

function HourSelect({value, onChange, ariaLabel}: {
  value: number;
  onChange: (h: number) => void;
  ariaLabel: string;
}) {
  return (
    <Select value={String(value)} onValueChange={(v) => onChange(Number(v))}>
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
 * 计划任务卡片：**令牌刷新**（每 N 天全量续期一次）+ **每日任务**（签到 / 领奖 /
 * 做任务，每天到点自动跑）。
 *
 * 两块放一起是因为它们本质是同一件事——「不用我手动点」；分开成两张卡会让人
 * 以为有两套调度。它们都由管理端自己的巡检器执行（后端 `renew` / `dailyplan`），
 * 与**上游** config.json 自带的那套调度器相互独立（上游那套在「设置 → 上游」）。
 *
 * 每日任务里 `full`（做任务）会伪造活跃上报，上游脚本自己都标「慎用」——所以
 * 默认关闭、并在界面上带红色警告，不藏着。
 */
export function RefreshPlanCard({onRan}: {onRan?: () => void}) {
  const t = useT();
  return (
    <section className="space-y-3 rounded-[20px] bg-muted px-3.5 py-3">
      <div className="flex items-center gap-2">
        <CalendarClock className="h-4 w-4 shrink-0 text-muted-foreground" />
        <div className="text-sm font-medium">{t('tasks.planCardTitle')}</div>
      </div>
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

  // 有未保存改动才允许点保存：避免「点了没反应」，也让「已生效」可判。
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

/** 次日任务草稿：从 DAILY_TASKS 派生，别手抄任务名（漏一个就是 tsc 报错）。 */
function draftOf(p: DailyPlan): DailyPlan {
  const out = {} as DailyPlan;
  for (const {key} of DAILY_TASKS) out[key] = {...p[key]};
  return out;
}

/** 每日任务：签到 / 领取补偿 / 领奖 / 做任务，各自一个开关 + 整点。 */
function DailyTasksSection({onRan}: {onRan?: () => void}) {
  const t = useT();
  const [plan, setPlan] = useState<DailyPlan | null>(null);
  const [draft, setDraft] = useState<DailyPlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState<DailyTask | null>(null);
  const [sched, setSched] = useState<TaskSchedulerStatus | null>(null);
  const [applying, setApplying] = useState<'upstream' | 'panel' | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [p, s] = await Promise.all([
        accountApi.dailyPlan(),
        // 调度冲突是辅助信息；取不到时不能让整个每日计划面板变成错误态。
        accountApi.taskScheduler().catch(() => null),
      ]);
      setPlan(p);
      setDraft(draftOf(p));
      setSched(s);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const dirty =
    !!plan && !!draft &&
    DAILY_TASKS.some(({key}) =>
      plan[key].enabled !== draft[key].enabled || plan[key].hour !== draft[key].hour);

  const patch = useCallback((key: DailyTask, next: Partial<DailyTaskConfig>) => {
    setDraft((d) => (d ? {...d, [key]: {...d[key], ...next}} : d));
  }, []);

  const save = useCallback(async () => {
    if (!draft) return;
    setSaving(true);
    try {
      const p = await accountApi.saveDailyPlan(draft);
      setPlan(p);
      setDraft(draftOf(p));
      setSched(await accountApi.taskScheduler().catch(() => null));
      notify.ok(t('tasks.dailySaved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }, [draft, t]);

  const applyKeep = useCallback(async (keep: 'upstream' | 'panel') => {
    setApplying(keep);
    try {
      const r = await accountApi.keepTaskScheduler(keep);
      setPlan(r.plan);
      setDraft(draftOf(r.plan));
      setSched(r.scheduler);
      notify.ok(t(keep === 'upstream'
        ? 'tasks.schedulerKeptUpstream'
        : 'tasks.schedulerKeptPanel'));
      if (r.reload_hint) notify.warn(t('tasks.schedulerReloadPending'), r.reload_hint);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setApplying(null);
    }
  }, [t]);

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

  if (loading || !draft) {
    return (
      <div className="flex items-center gap-2 text-xs text-muted-foreground" aria-busy="true">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        {t('common.loading')}
      </div>
    );
  }

  // 「今天是否已经跑过」——last_run 是 `YYYY-MM-DD-HH`，比对日期部分即可
  const today = new Date().toLocaleDateString('sv-SE'); // sv-SE = YYYY-MM-DD

  return (
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

      <div className="mt-2 space-y-2">
        {DAILY_TASKS.map(({key, labelKey, noteKey, warn}) => {
          const cfg = draft[key];
          const ranToday = (plan?.[key].last_run || '').startsWith(today);
          return (
            <div key={key} className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
              <Switch checked={cfg.enabled}
                      onCheckedChange={(v) => patch(key, {enabled: v})}
                      aria-label={t(labelKey)} />
              <div className="min-w-[9rem] flex-1">
                <div className="flex items-center gap-1.5 text-xs">
                  {warn && <TriangleAlert className="h-3.5 w-3.5 text-amber-500" />}
                  <span className={warn ? 'text-amber-600 dark:text-amber-400' : ''}>{t(labelKey)}</span>
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
                  {t(noteKey)}
                </div>
              </div>
              <HourSelect value={cfg.hour} onChange={(h) => patch(key, {hour: h})}
                          ariaLabel={t(labelKey)} />
              <Button size="sm" variant="outline" className="rounded-full"
                      disabled={running !== null} onClick={() => runNow(key)}>
                {running === key ? <Loader2 className="animate-spin" /> : <Play />}
                {t('tasks.planRunNow')}
              </Button>
            </div>
          );
        })}
      </div>

      <div className="mt-2.5">
        <Button size="sm" variant="outline" className="rounded-full"
                disabled={!dirty || saving} onClick={save}>
          {saving ? <Loader2 className="animate-spin" /> : <Save />}
          {t('common.save')}
        </Button>
      </div>
    </div>
  );
}

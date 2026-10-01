'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  DownloadCloud,
  Loader2,
  PlugZap,
  Save,
  UploadCloud,
  XCircle,
} from 'lucide-react';
import {errText, pgSyncApi} from '@/lib/api';
import type {PgSyncConfig, PgSyncStatus} from '@/lib/types';
import {notify} from '@/lib/toast';
import {fmtAgo, fmtDateTime} from '@/lib/format';
import {useAuth} from '@/lib/auth-context';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useT} from '@/lib/i18n/provider';
import {RichText} from '@/lib/i18n/rich-text';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';

/** 表单初值：与后端 DEFAULT_CONFIG 一致，避免首帧渲染出 undefined 输入框 */
const BLANK: PgSyncConfig = {
  enabled: false,
  host: '',
  port: 5432,
  dbname: '',
  user: '',
  password: '',
  sslmode: 'prefer',
  interval_minutes: 0,
  keep_local_backup: true,
  last_export_at: 0,
  last_import_at: 0,
};

export function PgSyncPanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [form, setForm] = useState<PgSyncConfig>(BLANK);
  const [status, setStatus] = useState<PgSyncStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [busy, setBusy] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  /**
   * 整份拉配置（表单初值 + 状态）。
   *
   * **只在显式路径上调用**：首次挂载、加载失败后的「重试」。（保存后的刷新
   * 用的是 `save()` 自己的返回，不走这里。）它做的是 `setForm(r.config)`
   * 全量覆盖，所以**绝不能进轮询**——那正是 issue #122：空闲轮询每 15 秒
   * 覆盖一次表单，用户填到一半的字段被打回去。轮询走 `pollStatus()`。
   */
  const load = useCallback(async () => {
    try {
      const r = await pgSyncApi.config();
      setForm(r.config);
      setStatus(r.status);
      setLoadError(false);
    } catch {
      // 取不到配置时**不能**渲染那张默认值表单：那看起来像「我的配置就是空的」，
      // 用户会以为备份从没配过，然后重新填一遍。
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  /**
   * 轮询**只更新状态**，不整份拉配置、不碰用户正在填的字段（issue #122）。
   *
   * 这条路径的关键约束：**绝不能覆盖用户正在编辑的内容**。之前的写法是空闲时
   * 每 15 秒调一次 `load()`（整份配置回来**全量覆盖**表单），填写到一半的
   * 地址 / 端口会被服务器旧值打回去——输入框每隔 15 秒跳一次，基本没法填。
   *
   * 现在拉轻量的 `/status`，并只并入两个**服务器持有**的时刻字段
   * （last_export_at / last_import_at，供「上次迁移 / 上次恢复」在定时备份跑完后
   * 自动刷新）；其余字段一律保持用户手里的值。
   */
  const pollStatus = useCallback(async () => {
    try {
      const s = await pgSyncApi.status();
      setStatus(s);
      setForm((f) => ({
        ...f,
        last_export_at: typeof s.last_export_at === 'number'
          ? s.last_export_at : f.last_export_at,
        last_import_at: typeof s.last_import_at === 'number'
          ? s.last_import_at : f.last_import_at,
      }));
    } catch {
      /* 任务进行中接口短暂不可用属正常，忽略 */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const running = !!status?.running;
  // 轮询一律只拉 /status：运行中 1.5 秒（进度条 / 日志），空闲 15 秒
  // （等定时备份的结果与完成时刻）。空闲时**不**再整份拉配置——见 pollStatus。
  //
  // 走 useHeartbeat 而不是裸 setInterval：标签页不可见时跳过（浏览器本来就会
  // 把定时器节流到约 1 次/分钟），**切回来立即补一次**——否则切走再回来要等
  // 一个完整间隔才更新（换机器的场景里用户正盯着进度条）。
  useHeartbeat(() => void pollStatus(), running ? 1500 : 15000);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [status?.logs]);

  const patch = (next: Partial<PgSyncConfig>) => setForm((f) => ({...f, ...next}));

  async function save() {
    setSaving(true);
    try {
      const r = await pgSyncApi.save({
        enabled: form.enabled,
        host: form.host.trim(),
        port: Number(form.port) || 5432,
        dbname: form.dbname.trim(),
        user: form.user.trim(),
        password: form.password,
        sslmode: form.sslmode,
        interval_minutes: Number(form.interval_minutes) || 0,
        keep_local_backup: form.keep_local_backup,
      });
      setForm(r.config);
      notify.ok(t('pgSync.saved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  async function test() {
    setTesting(true);
    try {
      const r = await pgSyncApi.test({
        host: form.host.trim(),
        port: Number(form.port) || 5432,
        dbname: form.dbname.trim(),
        user: form.user.trim(),
        password: form.password,
        sslmode: form.sslmode,
      });
      if (r.ok) {
        notify.ok(t('pgSync.testOk'), r.server_version || r.message);
      } else {
        notify.err(r.message);
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setTesting(false);
    }
  }

  async function run(kind: 'export' | 'import') {
    setBusy(true);
    try {
      const r = kind === 'export' ? await pgSyncApi.exportData() : await pgSyncApi.importData();
      if (r.ok) {
        notify.ok(kind === 'export' ? t('pgSync.exportStarted') : t('pgSync.importStarted'));
        setStatus(r.status);
      } else {
        notify.warn(r.message);
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return (
      <div className="rounded-[20px] bg-muted p-5 text-sm text-muted-foreground">
        {t('pgSync.loading')}
      </div>
    );
  }
  if (loadError) {
    return (
      <div className="rounded-[20px] bg-muted p-5">
        <div className="text-sm text-muted-foreground">{t('pgSync.loadFailed')}</div>
        <Button variant="outline" size="sm" className="mt-3" onClick={() => void load()}>
          {t('state.retry')}
        </Button>
      </div>
    );
  }

  const configured = !!form.host.trim() && !!form.dbname.trim();
  const disabled = !isAdmin || busy || running;
  const logs = status?.logs ?? [];

  return (
    <div className="space-y-4">
      {/* ── 说明 ── */}
      <div className="rounded-[20px] bg-muted p-5">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium text-foreground">
          <Database className="h-4 w-4" />
          {t('pgSync.title')}
        </div>
        <RichText text={t('pgSync.desc')} className="text-xs leading-6 text-muted-foreground" />
      </div>

      {/* ── 连接配置 ── */}
      <div className="rounded-[20px] border border-border p-5">
        <div className="mb-4 text-sm font-medium">{t('pgSync.connTitle')}</div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Label htmlFor="pg-host">{t('pgSync.host')}</Label>
            <Input
              id="pg-host"
              value={form.host}
              disabled={!isAdmin}
              placeholder="127.0.0.1"
              onChange={(e) => patch({host: e.target.value})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('pgSync.hostHint')}</p>
          </div>
          <div>
            <Label htmlFor="pg-port">{t('pgSync.port')}</Label>
            <Input
              id="pg-port"
              type="number"
              value={String(form.port)}
              disabled={!isAdmin}
              onChange={(e) => patch({port: Number(e.target.value) || 0})}
            />
          </div>
          <div>
            <Label htmlFor="pg-db">{t('pgSync.dbname')}</Label>
            <Input
              id="pg-db"
              value={form.dbname}
              disabled={!isAdmin}
              placeholder="workbuddy"
              onChange={(e) => patch({dbname: e.target.value})}
            />
          </div>
          <div>
            <Label htmlFor="pg-user">{t('pgSync.user')}</Label>
            <Input
              id="pg-user"
              value={form.user}
              disabled={!isAdmin}
              placeholder="postgres"
              onChange={(e) => patch({user: e.target.value})}
            />
          </div>
          <div>
            <Label htmlFor="pg-pass">{t('pgSync.password')}</Label>
            <Input
              id="pg-pass"
              type="password"
              value={form.password}
              disabled={!isAdmin}
              onChange={(e) => patch({password: e.target.value})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('pgSync.passwordHint')}</p>
          </div>
          <div>
            <Label htmlFor="pg-ssl">{t('pgSync.sslmode')}</Label>
            <Input
              id="pg-ssl"
              value={form.sslmode}
              disabled={!isAdmin}
              placeholder="prefer"
              onChange={(e) => patch({sslmode: e.target.value})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('pgSync.sslmodeHint')}</p>
          </div>
        </div>

        {/* ── 自动备份 ── */}
        <div className="mt-5 space-y-3 border-t border-border pt-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-sm">{t('pgSync.autoEnable')}</div>
              <p className="text-xs text-muted-foreground">{t('pgSync.autoEnableDesc')}</p>
            </div>
            <Switch
              checked={form.enabled}
              disabled={!isAdmin}
              onCheckedChange={(v) => patch({enabled: v})}
            />
          </div>
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-sm">{t('pgSync.interval')}</div>
              <p className="text-xs text-muted-foreground">{t('pgSync.intervalHint')}</p>
            </div>
            <Input
              type="number"
              className="w-28"
              value={String(form.interval_minutes)}
              disabled={!isAdmin}
              onChange={(e) => patch({interval_minutes: Number(e.target.value) || 0})}
            />
          </div>
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-sm">{t('pgSync.keepBackup')}</div>
              <p className="text-xs text-muted-foreground">{t('pgSync.keepBackupDesc')}</p>
            </div>
            <Switch
              checked={form.keep_local_backup}
              disabled={!isAdmin}
              onCheckedChange={(v) => patch({keep_local_backup: v})}
            />
          </div>
        </div>

        <div className="mt-5 flex flex-wrap gap-2">
          <Button onClick={() => void save()} disabled={!isAdmin || saving}>
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            {t('pgSync.save')}
          </Button>
          <Button variant="outline" onClick={() => void test()} disabled={testing || !isAdmin}>
            {testing ? <Loader2 className="h-4 w-4 animate-spin" /> : <PlugZap className="h-4 w-4" />}
            {t('pgSync.test')}
          </Button>
        </div>
        {!isAdmin && (
          <p className="mt-3 text-xs text-muted-foreground">{t('pgSync.adminOnly')}</p>
        )}
      </div>

      {/* ── 迁移 / 恢复 ── */}
      <div className="rounded-[20px] border border-border p-5">
        <div className="mb-4 text-sm font-medium">{t('pgSync.actionTitle')}</div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="rounded-2xl bg-muted p-4">
            <div className="flex items-center gap-2 text-sm font-medium">
              <UploadCloud className="h-4 w-4" />
              {t('pgSync.exportTitle')}
            </div>
            <p className="mt-2 text-xs leading-6 text-muted-foreground">{t('pgSync.exportDesc')}</p>
            <p className="mt-2 text-xs text-muted-foreground">
              {t('pgSync.lastExport')}
              {form.last_export_at ? ` ${fmtAgo(form.last_export_at)}` : ` ${t('pgSync.never')}`}
            </p>
            <ConfirmDialog
              title={t('pgSync.exportConfirmTitle')}
              description={t('pgSync.exportConfirmDesc')}
              confirmText={t('pgSync.exportBtn')}
              trigger={
                <Button className="mt-3 w-full" disabled={disabled || !configured}>
                  <UploadCloud className="h-4 w-4" />
                  {t('pgSync.exportBtn')}
                </Button>
              }
              onConfirm={() => void run('export')}
            />
          </div>

          <div className="rounded-2xl bg-muted p-4">
            <div className="flex items-center gap-2 text-sm font-medium">
              <DownloadCloud className="h-4 w-4" />
              {t('pgSync.importTitle')}
            </div>
            <RichText text={t('pgSync.importDesc')}
                      className="mt-2 block text-xs leading-6 text-muted-foreground" />
            <p className="mt-2 text-xs text-muted-foreground">
              {t('pgSync.lastImport')}
              {form.last_import_at ? ` ${fmtAgo(form.last_import_at)}` : ` ${t('pgSync.never')}`}
            </p>
            <ConfirmDialog
              destructive
              title={t('pgSync.importConfirmTitle')}
              description={t('pgSync.importConfirmDesc')}
              confirmText={t('pgSync.importBtn')}
              trigger={
                <Button
                  variant="destructive"
                  className="mt-3 w-full"
                  disabled={disabled || !configured}
                >
                  <DownloadCloud className="h-4 w-4" />
                  {t('pgSync.importBtn')}
                </Button>
              }
              onConfirm={() => void run('import')}
            />
          </div>
        </div>
        {!configured && (
          <p className="mt-3 text-xs text-muted-foreground">{t('pgSync.needConfig')}</p>
        )}
      </div>

      {/* ── 进度与日志 ── */}
      {status && (status.running || status.ok !== null) && (
        <div className="rounded-[20px] border border-border p-5">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            {status.running ? (
              <Badge variant="secondary">
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                {t('pgSync.running')}
              </Badge>
            ) : status.ok ? (
              <Badge>
                <CheckCircle2 className="mr-1 h-3 w-3" />
                {t('pgSync.done')}
              </Badge>
            ) : (
              <Badge variant="destructive">
                <XCircle className="mr-1 h-3 w-3" />
                {t('pgSync.failed')}
              </Badge>
            )}
            <span className="text-xs text-muted-foreground">
              {status.kind === 'export' ? t('pgSync.kindExport') : t('pgSync.kindImport')}
            </span>
            {status.started_at ? (
              <span className="text-xs text-muted-foreground">
                {fmtDateTime(status.started_at)}
              </span>
            ) : null}
          </div>

          <div className="text-sm">{status.step}</div>

          {status.running && (
            <>
              <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-primary transition-all"
                  style={{width: `${Math.max(2, Math.min(100, status.percent))}%`}}
                />
              </div>
              <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
                <span>
                  {t('pgSync.tableProgress', {
                    done: status.tables_done,
                    total: status.tables_total,
                  })}
                </span>
                <span>{t('pgSync.rowCount', {n: status.rows})}</span>
              </div>
            </>
          )}

          {logs.length > 0 && (
            <div
              ref={logRef}
              className="mt-3 max-h-56 overflow-auto rounded-2xl bg-muted p-3 font-mono text-xs leading-5"
            >
              {logs.map((line, i) => (
                <div
                  key={`${line.ts}-${i}`}
                  className={
                    line.level === 'error'
                      ? 'text-destructive'
                      : line.level === 'warn'
                        ? 'text-amber-600 dark:text-amber-400'
                        : 'text-muted-foreground'
                  }
                >
                  {line.text}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 恢复是破坏性动作，界面上再留一句常驻提醒 */}
      <div className="flex items-start gap-2 rounded-[20px] bg-muted p-4 text-xs leading-6 text-muted-foreground">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <RichText text={t('pgSync.warn')} />
      </div>
    </div>
  );
}

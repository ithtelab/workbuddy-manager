'use client';

/**
 * 设置 → Qoder：qoder2api 管理面代理的面板（见 server/services/qoder.py）。
 *
 * 三块能力，对应三个数据源：
 *   1. **控制台连接**：地址 + 密码（write-only，保存后明文不再可见）；
 *   2. **运行状态 / 账号 / 额度 / 模型**：console 的只读端点原样代理；
 *   3. **轻写操作**：签到 / 切激活 / 桥启停（会话管理员 + 服务端审计）。
 *
 * 刻意**不做**的：OAuth 录号、账号增删、cleanup——那些涉及凭据录入与设备
 * 指纹，qoder2api 自己的控制台做得更好，面板代理只会多一份泄露面（R07-R09）。
 *
 * console 的响应是原样代理，字段随上游版本演进：界面只认「名字」这一类稳定键，
 * 其余整块 JSON 折叠展示——宁可诚实地说「原始数据」，也不猜上游没承诺的形状。
 */
import {useCallback, useEffect, useState} from 'react';
import {RefreshCw} from 'lucide-react';
import {errText, qoderApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {QoderAccount, QoderConsoleConfig, QoderStatus} from '@/lib/types';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';

/** console /api/accounts 兼容两种形状：裸数组或 {accounts: [...]}。 */
function asAccounts(raw: unknown): QoderAccount[] {
  if (Array.isArray(raw)) return raw as QoderAccount[];
  if (raw && typeof raw === 'object' && Array.isArray((raw as {accounts?: unknown}).accounts)) {
    return (raw as {accounts: QoderAccount[]}).accounts;
  }
  return [];
}

function SectionCard({title, desc, children}: {
  title: string; desc?: string; children: React.ReactNode;
}) {
  return (
    <div className="rounded-[20px] bg-muted p-4">
      <div className="mb-1 text-sm font-medium">{title}</div>
      {desc ? <div className="mb-3 text-[11px] text-muted-foreground">{desc}</div> : null}
      {children}
    </div>
  );
}

export function QoderPanel() {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const [config, setConfig] = useState<QoderConsoleConfig | null>(null);
  const [status, setStatus] = useState<QoderStatus | null>(null);
  const [accounts, setAccounts] = useState<QoderAccount[]>([]);
  const [quotaRaw, setQuotaRaw] = useState<unknown>(null);
  const [modelsRaw, setModelsRaw] = useState<unknown>(null);
  const [form, setForm] = useState<{url: string; password: string}>({url: '', password: ''});
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [acting, setActing] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const loadConfig = useCallback(async () => {
    try {
      const cfg = await qoderApi.settings();
      setConfig(cfg);
      setForm({url: cfg.url || '', password: ''});
      return cfg;
    } catch (e) {
      notify.err(errText(e));
      return null;
    }
  }, []);

  const loadData = useCallback(async () => {
    // 四个只读端点各自吞错：status 挂了不该连累账号列表显示（console 未配置
    // 时全部 400 是正常状态，交给每张卡片自己的「未配置」提示，不弹 toast）。
    const [s, a, q, m] = await Promise.allSettled([
      qoderApi.status(), qoderApi.accounts(), qoderApi.quota(), qoderApi.models(),
    ]);
    if (s.status === 'fulfilled') setStatus(s.value);
    else setStatus(null);
    if (a.status === 'fulfilled') setAccounts(asAccounts(a.value));
    else setAccounts([]);
    if (q.status === 'fulfilled') setQuotaRaw(q.value);
    if (m.status === 'fulfilled') setModelsRaw(m.value);
    setLoaded(true);
  }, []);

  useEffect(() => {
    void (async () => {
      const cfg = await loadConfig();
      if (cfg?.configured) await loadData();
      else setLoaded(true);
    })();
  }, [loadConfig, loadData]);

  async function save() {
    if (busy) return;
    setBusy(true);
    try {
      const out = await qoderApi.saveSettings({
        url: form.url,
        // 留空 = 不修改（PATCH 语义，见 server/services/qoder.py）
        password: form.password === '' ? null : form.password,
      });
      setConfig(out);
      setForm({url: out.url || form.url, password: ''});
      notify.ok(t('qoder.saved'));
      if (out.configured) await loadData();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    if (testing) return;
    setTesting(true);
    try {
      const r = await qoderApi.test();
      if (r.ok) notify.ok(r.message);
      else notify.err(r.message);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setTesting(false);
    }
  }

  async function act(key: string, fn: () => Promise<unknown>, doneMsg: string) {
    if (acting) return;
    setActing(key);
    try {
      await fn();
      notify.ok(doneMsg);
      await loadData();
    } catch (e) {
      // 失败也留了痕（服务端审计），这里只负责把原因弹出来
      notify.err(errText(e));
    } finally {
      setActing(null);
    }
  }

  const modelIds: string[] = (() => {
    if (modelsRaw && typeof modelsRaw === 'object'
        && Array.isArray((modelsRaw as {data?: unknown}).data)) {
      return ((modelsRaw as {data: Array<{id?: unknown}>}).data || [])
        .map((m) => String(m?.id ?? '')).filter(Boolean);
    }
    return [];
  })();

  const running = !!status?.running;

  return (
    <div className="space-y-4">
      {/* ── 控制台连接（write-only 凭据）── */}
      <SectionCard title={t('qoder.configTitle')} desc={t('qoder.desc')}>
        {!isAdmin ? (
          <div className="rounded-xl bg-background px-3 py-2 text-xs text-muted-foreground">
            {t('qoder.adminRequired')}
            {config?.url ? ` (${config.url})` : ''}
          </div>
        ) : (
          <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">{t('qoder.urlLabel')}</Label>
              <Input
                value={form.url}
                onChange={(e) => setForm({...form, url: e.target.value})}
                placeholder={t('qoder.urlPlaceholder')}
                maxLength={500}
                className="bg-background"
              />
              <p className="text-[11px] leading-4 text-muted-foreground">{t('qoder.urlHint')}</p>
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">{t('qoder.passwordLabel')}</Label>
              <Input
                type="password"
                value={form.password}
                onChange={(e) => setForm({...form, password: e.target.value})}
                placeholder={config?.password_set ? t('qoder.passwordKeep') : ''}
                maxLength={500}
                className="bg-background"
              />
              <p className="text-[11px] leading-4 text-muted-foreground">{t('qoder.passwordHint')}</p>
            </div>
            <div className="flex items-center gap-2">
              <Button className="rounded-full" disabled={busy} onClick={save}>
                {t('qoder.save')}
              </Button>
              <Button
                variant="outline"
                className="rounded-full"
                disabled={testing || !config?.configured}
                onClick={test}
              >
                {testing ? t('qoder.testing') : t('qoder.test')}
              </Button>
            </div>
          </div>
        )}
        {!loaded ? (
          <p className="mt-3 text-[11px] text-muted-foreground">…</p>
        ) : !config?.configured ? (
          <p className="mt-3 text-[11px] leading-4 text-muted-foreground">
            {t('qoder.notConfigured')}
          </p>
        ) : null}
      </SectionCard>

      {/* ── 运行状态 ── */}
      {config?.configured && (
        <SectionCard title={t('qoder.statusTitle')}>
          <div className="flex flex-wrap items-center gap-3 rounded-xl bg-background px-3 py-2">
            <Badge variant={running ? 'secondary' : 'destructive'} className="rounded-full text-[10px]">
              {running ? t('qoder.statusRunning') : t('qoder.statusStopped')}
            </Badge>
            <span className="text-xs text-muted-foreground">
              {t('qoder.activeAccount')}: <span className="font-mono">{status?.active_account || t('qoder.none')}</span>
            </span>
            <div className="ml-auto flex items-center gap-2">
              <Button
                variant="ghost"
                size="sm"
                className="rounded-full"
                onClick={() => void loadData()}
              >
                <RefreshCw className="h-3.5 w-3.5" />
                {t('qoder.refresh')}
              </Button>
              {isAdmin && (
                running ? (
                  <Button
                    variant="outline"
                    size="sm"
                    className="rounded-full"
                    disabled={acting === 'bridge'}
                    onClick={() => void act('bridge', () => qoderApi.bridgeStop(), t('qoder.bridgeDone'))}
                  >
                    {t('qoder.bridgeStop')}
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    className="rounded-full"
                    disabled={acting === 'bridge'}
                    onClick={() => void act('bridge', () => qoderApi.bridgeStart(), t('qoder.bridgeDone'))}
                  >
                    {t('qoder.bridgeStart')}
                  </Button>
                )
              )}
            </div>
          </div>
        </SectionCard>
      )}

      {/* ── 账号（只读 + 轻写：签到 / 切激活）── */}
      {config?.configured && (
        <SectionCard
          title={t('qoder.accountsTitle')}
          desc={isAdmin ? undefined : t('qoder.adminRequired')}
        >
          {accounts.length === 0 ? (
            <p className="rounded-xl bg-background px-3 py-4 text-center text-xs text-muted-foreground">
              {t('qoder.noAccounts')}
            </p>
          ) : (
            <div className="space-y-2">
              {accounts.map((acc, i) => (
                <div
                  key={String(acc.id ?? i)}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-background px-3 py-2"
                >
                  <div className="min-w-0 space-y-0.5">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-sm font-medium">{String(acc.name || acc.id || '—')}</span>
                      {acc.active && (
                        <Badge variant="secondary" className="rounded-full text-[10px]">
                          {t('qoder.activeTag')}
                        </Badge>
                      )}
                      {acc.region ? (
                        <span className="text-[11px] text-muted-foreground">{String(acc.region)}</span>
                      ) : null}
                    </div>
                    {acc.email ? (
                      <div className="truncate font-mono text-[11px] text-muted-foreground">
                        {String(acc.email)}
                      </div>
                    ) : null}
                  </div>
                  {isAdmin && !acc.active && (
                    <Button
                      variant="outline"
                      size="sm"
                      className="rounded-full"
                      disabled={acting === `activate-${acc.id}`}
                      onClick={() => void act(
                        `activate-${acc.id}`,
                        () => qoderApi.activate(String(acc.id ?? '')),
                        t('qoder.activated'),
                      )}
                    >
                      {acting === `activate-${acc.id}` ? t('qoder.activating') : t('qoder.activate')}
                    </Button>
                  )}
                </div>
              ))}
              {isAdmin && (
                <div>
                  <Button
                    size="sm"
                    className="rounded-full"
                    disabled={acting === 'checkin'}
                    onClick={() => void act('checkin', () => qoderApi.checkin(), t('qoder.checkinDone'))}
                  >
                    {acting === 'checkin' ? t('qoder.checking') : t('qoder.checkin')}
                  </Button>
                </div>
              )}
            </div>
          )}
        </SectionCard>
      )}

      {/* ── 额度 / 模型（console 原样代理，诚实展示原始数据）── */}
      {config?.configured && quotaRaw != null && (
        <SectionCard title={t('qoder.quotaTitle')}>
          <pre className="max-h-64 overflow-auto rounded-xl bg-background p-3 font-mono text-[11px] leading-4 text-muted-foreground">
            {JSON.stringify(quotaRaw, null, 2)}
          </pre>
        </SectionCard>
      )}
      {config?.configured && (
        <SectionCard title={t('qoder.modelsTitle')}>
          {modelIds.length === 0 ? (
            <p className="rounded-xl bg-background px-3 py-4 text-center text-xs text-muted-foreground">
              {t('qoder.noModels')}
            </p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {modelIds.map((id) => (
                <Badge key={id} variant="secondary" className="rounded-full font-mono text-[10px]">
                  {id}
                </Badge>
              ))}
            </div>
          )}
        </SectionCard>
      )}
    </div>
  );
}

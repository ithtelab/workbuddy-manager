'use client';

/**
 * 设置 → 系统 → 网关跨域（CORS）：issue #445。
 *
 * **为什么这是一个独立面板、而不是复用 `WB_CORS_ORIGINS`**：那个环境变量管的是
 * `/api/*` 管理面（Cookie 会话），误配成 `*` 等于把控制台交给任何网页，所以它
 * 只能留在部署文件里。数据面 `/v1`、`/v2` 用 Bearer 密钥，浏览器不会自动带上，
 * 因此可以安全地在面板上放开到任意来源 —— 恰好也是浏览器里直连网关的用户
 * 最需要的。
 *
 * 界面刻意把「管理面来源」作为**只读**信息列出来：用户填完这里发现 `/api` 还是
 * 跨域时，能立刻看到「那套是另一处配置」，而不是反复点保存。
 */

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {Globe2, Loader2, Plus, Save, ShieldAlert, Trash2} from 'lucide-react';
import {errText, systemApi} from '@/lib/api';
import type {GatewayCors} from '@/lib/types';
import {notify} from '@/lib/toast';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {RichText} from '@/lib/i18n/rich-text';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';

const BLANK: GatewayCors = {
  enabled: false,
  origins: [],
  max_age: 600,
  max_origins: 64,
  prefixes: ['/v1', '/v2'],
  admin_origins: [],
};

export function GatewayCorsPanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [form, setForm] = useState<GatewayCors>(BLANK);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  /**
   * 列表按「一行一条」编辑。
   *
   * 不直接编辑数组：输入过程中的空行必须能存在（否则用户敲下第一个字符之前
   * 那一行就被删掉了），所以这里保留原始文本行，保存时才过滤空行交给后端。
   */
  const [lines, setLines] = useState<string[]>(['']);
  /** 上次保存（或刚加载）时的开关与缓存秒数，用来判断「有未保存改动」 */
  const savedEnabled = useRef(false);
  const savedMaxAge = useRef(600);

  const load = useCallback(async () => {
    try {
      const r = await systemApi.gatewayCors();
      setForm(r);
      setLines(r.origins.length ? r.origins : ['']);
      savedEnabled.current = r.enabled;
      savedMaxAge.current = r.max_age;
      setLoadError(false);
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const effectiveLines = useMemo(
    () => lines.map((s) => s.trim()).filter(Boolean),
    [lines],
  );
  const wildcard = effectiveLines.includes('*');
  // 三项都要比：只看 origins 的话，只改开关或只改缓存秒数时界面不提示
  // 「未保存」，用户会以为已经生效了（保存按钮旁边那句提示就是为此存在）。
  const dirty = useMemo(() => (
    [...form.origins].join('\n') !== effectiveLines.join('\n')
    || form.max_age !== savedMaxAge.current
    || form.enabled !== savedEnabled.current
  ), [form.origins, form.max_age, form.enabled, effectiveLines]);

  function setLine(index: number, value: string) {
    setLines((cur) => cur.map((s, i) => (i === index ? value : s)));
  }

  function addLine() {
    setLines((cur) => [...cur, '']);
  }

  function removeLine(index: number) {
    setLines((cur) => (cur.length <= 1 ? [''] : cur.filter((_, i) => i !== index)));
  }

  async function save() {
    setSaving(true);
    try {
      const r = await systemApi.saveGatewayCors({
        enabled: form.enabled,
        origins: effectiveLines,
        max_age: form.max_age,
      });
      setForm(r);
      setLines(r.origins.length ? r.origins : ['']);
      savedEnabled.current = r.enabled;
      savedMaxAge.current = r.max_age;
      notify.ok(t('gatewayCors.saved'));
    } catch (e) {
      // 400 的 detail 已经写清了哪一条格式不对，直接透出比「保存失败」有用
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="rounded-[20px] bg-muted p-5 text-sm text-muted-foreground">
        {t('gatewayCors.loading')}
      </div>
    );
  }
  if (loadError) {
    return (
      <div className="rounded-[20px] bg-muted p-5">
        <div className="text-sm text-muted-foreground">{t('gatewayCors.loadFailed')}</div>
        <Button variant="outline" size="sm" className="mt-3" onClick={() => void load()}>
          {t('state.retry')}
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* ── 说明 ── */}
      <div className="rounded-[20px] bg-muted p-5">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium text-foreground">
          <Globe2 className="h-4 w-4" />
          {t('gatewayCors.title')}
        </div>
        <RichText text={t('gatewayCors.desc')} className="text-xs leading-6 text-muted-foreground" />
      </div>

      {/* ── 开关与来源 ── */}
      <div className="rounded-[20px] border border-border p-5">
        <div className="flex items-center justify-between gap-4">
          <div>
            <div className="text-sm">{t('gatewayCors.enable')}</div>
            <p className="text-xs text-muted-foreground">{t('gatewayCors.enableHint')}</p>
          </div>
          <Switch
            aria-label={t('gatewayCors.enable')}
            checked={form.enabled}
            disabled={!isAdmin}
            onCheckedChange={(v) => setForm((f) => ({...f, enabled: v}))}
          />
        </div>

        <div className="mt-5 border-t border-border pt-4">
          <Label>{t('gatewayCors.originsTitle')}</Label>
          <p className="mb-3 mt-1 text-xs text-muted-foreground">
            {t('gatewayCors.originsHint', {max: form.max_origins})}
          </p>
          <div className="space-y-2">
            {lines.map((value, index) => (
              <div key={index} className="flex items-center gap-2">
                <Input
                  value={value}
                  disabled={!isAdmin}
                  placeholder="https://app.example.com"
                  aria-label={t('gatewayCors.originPlaceholder')}
                  onChange={(e) => setLine(index, e.target.value)}
                />
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={t('common.delete')}
                  disabled={!isAdmin || lines.length <= 1}
                  onClick={() => removeLine(index)}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            ))}
          </div>
          <Button
            variant="outline"
            size="sm"
            className="mt-3"
            disabled={!isAdmin || lines.length >= form.max_origins}
            onClick={addLine}
          >
            <Plus className="h-4 w-4" />
            {t('gatewayCors.addOrigin')}
          </Button>
        </div>

        <div className="mt-5 flex items-center justify-between gap-4 border-t border-border pt-4">
          <div>
            <div className="text-sm">{t('gatewayCors.maxAge')}</div>
            <p className="text-xs text-muted-foreground">{t('gatewayCors.maxAgeHint')}</p>
          </div>
          <Input
            type="number"
            className="w-28"
            aria-label={t('gatewayCors.maxAge')}
            value={String(form.max_age)}
            disabled={!isAdmin}
            onChange={(e) => setForm((f) => ({...f, max_age: Number(e.target.value) || 0}))}
          />
        </div>

        {wildcard && (
          <div className="mt-4 flex items-start gap-2 rounded-xl bg-amber-500/10 p-3 text-xs leading-5 text-amber-700 dark:text-amber-400">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{t('gatewayCors.wildcardWarn')}</span>
          </div>
        )}

        <div className="mt-5 flex flex-wrap items-center gap-2">
          <Button onClick={() => void save()} disabled={!isAdmin || saving}>
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            {t('gatewayCors.save')}
          </Button>
          {dirty && (
            <span className="text-xs text-muted-foreground">{t('gatewayCors.unsaved')}</span>
          )}
          <span className="text-xs text-muted-foreground">
            {t('gatewayCors.scope', {prefixes: form.prefixes.join('、')})}
          </span>
        </div>
      </div>

      {/* ── 管理面来源（只读）── */}
      <div className="rounded-[20px] border border-border p-5">
        <div className="text-sm">{t('gatewayCors.adminTitle')}</div>
        <p className="mt-1 text-xs leading-6 text-muted-foreground">
          {t('gatewayCors.adminHint')}
        </p>
        {form.admin_origins.length ? (
          <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
            {form.admin_origins.map((o) => (
              <li key={o} className="font-mono">{o}</li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-xs text-muted-foreground">{t('gatewayCors.adminEmpty')}</p>
        )}
      </div>
    </div>
  );
}

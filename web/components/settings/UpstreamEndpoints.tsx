'use client';

/**
 * 多上游（账号池分组）管理面板。
 *
 * 为什么需要这个面板：本服务是转发型反代，账号由上游挑 —— 想让「不同下游密钥走
 * 不同账号池」，只能靠配置多个上游、再让密钥绑定其中之一（见 server/upstreamsvc.py）。
 * 没有这个面板，管理员只能手改数据库，而这个功能的价值恰恰在「日常顺手用」。
 *
 * 默认上游不可编辑也不可删除：它是环境变量 / 上游 config.json 的运行时映射，
 * 不是数据库里的一行（所以列表里只展示它，并单独标注）。
 *
 * 表单抽在 UpstreamFormDialog（账号页的「添加分组」用同一个）——这里的账号目录
 * 字段就是账号页分组的落点所在，两处必须是一份实现。
 */
import {useCallback, useEffect, useState} from 'react';

import {errText, upstreamsApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {UpstreamEndpoint} from '@/lib/types';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {UpstreamFormDialog} from '@/components/common/upstreams/UpstreamFormDialog';

export function UpstreamEndpoints() {
  const t = useT();
  // 这些操作后端要求会话管理员：对 viewer 显示出来只会得到 403。
  const {isAdmin} = useAuth();
  const [items, setItems] = useState<UpstreamEndpoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<UpstreamEndpoint | null>(null);
  const [busy, setBusy] = useState(false);
  const [probing, setProbing] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await upstreamsApi.list();
      setItems(r.items || []);
      return true;
    } catch (e) {
      notify.err(errText(e));
      return false;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function openCreate() {
    setEditing(null);
    setFormOpen(true);
  }

  function openEdit(item: UpstreamEndpoint) {
    setEditing(item);
    setFormOpen(true);
  }

  async function remove(item: UpstreamEndpoint) {
    if (item.is_default || item.id == null) return;
    setBusy(true);
    try {
      await upstreamsApi.remove(item.id);
      notify.ok(t('upstreams.deleted'));
      await load();
    } catch (e) {
      // 还有密钥绑着它时后端回 409 并说明把数 —— 原样弹出来，别让管理员猜
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function probe(item: UpstreamEndpoint) {
    if (item.is_default || item.id == null) return;
    setProbing(item.id);
    try {
      const r = await upstreamsApi.probe(item.id);
      if (r.ok) notify.ok(t('upstreams.probeOk'), r.message);
      else notify.err(t('upstreams.probeFailed'), r.message);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setProbing(null);
    }
  }

  return (
    <section className="space-y-3 rounded-[20px] bg-muted p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-1">
          <h2 className="text-sm font-semibold">{t('upstreams.title')}</h2>
          <p className="text-[11px] leading-4 text-muted-foreground">{t('upstreams.desc')}</p>
        </div>
        {isAdmin && (
          <Button className="rounded-full" onClick={openCreate}>
            {t('upstreams.add')}
          </Button>
        )}
      </div>

      {loading ? (
        <p className="text-xs text-muted-foreground">{t('common.loading')}</p>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <div
              key={item.is_default ? 'default' : item.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-background px-3 py-2"
            >
              <div className="min-w-0 space-y-0.5">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-sm font-medium">{item.name}</span>
                  {item.is_default && (
                    <Badge variant="secondary" className="rounded-full text-[10px]">
                      {t('upstreams.defaultTag')}
                    </Badge>
                  )}
                  {!item.enabled && (
                    <Badge variant="destructive" className="rounded-full text-[10px]">
                      {t('upstreams.disabledTag')}
                    </Badge>
                  )}
                  <span className="text-[11px] text-muted-foreground">
                    {t('upstreams.boundKeys', {n: item.bound_keys ?? 0})}
                  </span>
                </div>
                <div className="truncate font-mono text-[11px] text-muted-foreground">
                  {item.base_url}
                </div>
                {item.auth_dir ? (
                  <div
                    className="truncate font-mono text-[11px] text-muted-foreground"
                    title={t('upstreams.dirLine', {dir: item.auth_dir})}
                  >
                    {t('upstreams.dirLine', {dir: item.auth_dir})}
                  </div>
                ) : (
                  <div className="truncate text-[11px] text-muted-foreground">
                    {t('upstreams.noDirLine')}
                  </div>
                )}
                {item.note ? (
                  <div className="truncate text-[11px] text-muted-foreground">{item.note}</div>
                ) : null}
              </div>
              {isAdmin && !item.is_default && (
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    className="rounded-full"
                    disabled={probing === item.id}
                    onClick={() => probe(item)}
                  >
                    {probing === item.id ? t('upstreams.probing') : t('upstreams.probe')}
                  </Button>
                  <Button variant="outline" className="rounded-full" onClick={() => openEdit(item)}>
                    {t('upstreams.edit')}
                  </Button>
                  <ConfirmDialog
                    title={t('upstreams.deleteTitle')}
                    description={t('upstreams.deleteDesc', {name: item.name})}
                    confirmText={t('upstreams.remove')}
                    destructive
                    onConfirm={() => remove(item)}
                    trigger={
                      <Button variant="ghost" className="rounded-full text-destructive"
                              disabled={busy}>
                        {t('upstreams.remove')}
                      </Button>
                    }
                  />
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <p className="text-[11px] leading-4 text-muted-foreground">{t('upstreams.hint')}</p>

      <UpstreamFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        editing={editing}
        defaultUpstream={items.find((item) => item.is_default) ?? null}
        onSaved={() => void load()}
      />
    </section>
  );
}

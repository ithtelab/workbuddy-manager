'use client';

import {useMemo, useState} from 'react';
import {Check, ChevronsUpDown, Search, X} from 'lucide-react';
import {modelApi} from '@/lib/api';
import type {CatalogModel} from '@/lib/types';
import type {Realm} from '@/lib/realm-context';
import {useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Popover, PopoverContent, PopoverTrigger} from '@/components/ui/popover';
import {cn} from '@/lib/utils';

/**
 * 模型白名单输入（issue #141）：把「手打模型名」换成「从清单里勾」。
 *
 * 为什么保留输入框：清单里暂时没有的模型（新模型、私有部署、还没同步到的目录）仍然
 * 要能填 —— 候选只解决「记不住名字、写错了要等调用失败才发现」，不该变成新的门槛。
 *
 * 存储形态没变（仍是逗号/换行分隔的字符串），所以后端与既有密钥数据一行都不用动：
 * 勾选 = 往那个字符串里追加一个名字，取消 = 移除。
 *
 * 清单按**版本分组**展示（国内版 / 国际版）：白名单是在所选版本之内再收窄的，
 * 放在一起看不出哪个名字属于哪一版。
 *
 * 校验仍由父组件在输入框失焦时触发（与本组件引入前一致）；勾选进来的名字来自清单，
 * 本身不可能写错，因此不必再查一次。
 */
export function ModelWhitelistInput({
  value,
  onChange,
  onEdit,
  onBlur,
  realm,
  placeholder,
  disabled = false,
}: {
  value: string;
  onChange: (next: string) => void;
  /**
   * 正在编辑（新值还没提交到 `value`）。
   *
   * 父组件用它**作废上一次的校验结论**：输入框里的草稿只有失焦/回车才提交，若不作废，
   * 用户改了内容而提示还是旧的 —— 显示的「都对」可能对应的是改之前那一版，
   * 比不显示更误导（issue #46 那条不变量的原话）。
   */
  onEdit?: () => void;
  /** 失焦：参数是**提交后**的白名单字符串（父组件据此校验已知/未知模型） */
  onBlur?: (next: string) => void;
  /** 当前密钥的限定版本：清单里与它一致的那一组排在前面，便于先挑本版模型 */
  realm?: Realm | '';
  placeholder?: string;
  disabled?: boolean;
}) {
  const t = useT();
  const names = useMemo(() => splitNames(value), [value]);
  const [draft, setDraft] = useState('');
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [catalog, setCatalog] = useState<{cn: CatalogModel[]; global: CatalogModel[]} | null>(null);
  /** 只看上游标注为「限时免费」的模型（issue #208）；tags 缺失时按不在免费目录处理 */
  const [freeOnly, setFreeOnly] = useState(false);
  const [failed, setFailed] = useState(false);

  /** 勾选/取消：按当前顺序重建字符串（去重，保持用户看到的次序） */
  function emit(next: string[]) {
    onChange(Array.from(new Set(next)).join(', '));
  }

  function add(name: string) {
    const clean = name.trim().replace(/,+$/, '');
    if (!clean) return;
    emit([...names, clean]);
    setDraft('');
  }

  function remove(name: string) {
    emit(names.filter((n) => n !== name));
  }

  /** 打开时（只拉一次）取两个版本的模型清单；取不到就如实说，不挡住手输 */
  async function ensureCatalog() {
    if (catalog || failed) return;
    try {
      // 用模型目录而不是 /api/models：只有目录条目带 tags（含「限时免费」），
      // 白名单的免费筛选依赖它。目录有后端缓存，打开弹窗时拉一次即可。
      const [cn, global] = await Promise.all([
        modelApi.catalog('cn'),
        modelApi.catalog('global'),
      ]);
      setCatalog({cn: cn.models, global: global.models});
    } catch {
      setFailed(true);
    }
  }

  // 与密钥「限定版本」一致的那一组排前面：国内版密钥最可能选的是国内版模型。
  const isFree = (m: CatalogModel) => (m.tags || []).some((tag) => tag.includes('免费'));
  const groups: {key: Realm; label: string; models: CatalogModel[]}[] = [];
  if (catalog) {
    groups.push({key: 'cn', label: t('realm.cn'), models: catalog.cn});
    groups.push({key: 'global', label: t('realm.global'), models: catalog.global});
    // 排序要就地进行：写成 `[...].sort(...)` 会让 TS 把字面量的 key 推宽成 string，
    // 整个数组就不再是声明的那个类型（tsc 报过）。
    groups.sort((a, b) => (a.key === realm ? -1 : b.key === realm ? 1 : 0));
  }
  const needle = query.trim().toLowerCase();
  const filtered = groups
    .map((g) => ({
      ...g,
      models: g.models.filter((m) =>
        (!needle || m.id.toLowerCase().includes(needle)) && (!freeOnly || isFree(m))),
    }))
    .filter((g) => g.models.length > 0);

  return (
    <div className="space-y-1.5">
      {names.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {names.map((name) => (
            <span
              key={name}
              className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 font-mono text-[10px]"
            >
              {name}
              {!disabled && (
                <button
                  type="button"
                  aria-label={t('keys.modelRemove', {name})}
                  onClick={() => remove(name)}
                  className="text-muted-foreground hover:text-foreground"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </span>
          ))}
        </div>
      )}
      <div className="flex gap-1.5">
        <Input
          value={draft}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(e) => {
            onEdit?.();            // 值还没提交，先把上一次的校验结论作废
            // 逗号是既有的分隔写法：粘贴一串名字时按它就拆开
            if (e.target.value.includes(',')) {
              // 粘贴「a, b, c」时**一次算好再提交**：逐个调用 add() 会各自基于同一份
              // 旧 names 计算，最后只剩最后一个（复审时发现的真 bug）。
              const parts = e.target.value.split(',').map((s) => s.trim()).filter(Boolean);
              if (parts.length) emit([...names, ...parts]);
              setDraft('');
              return;
            }
            setDraft(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              add(draft);
            } else if (e.key === 'Backspace' && !draft && names.length) {
              remove(names[names.length - 1]);
            }
          }}
          onBlur={() => {
            // 把提交后的新值一起给出去：父组件拿它做「这些名字存在吗」的校验，
            // 而它自己的 state 此刻还没更新（读旧值会漏掉刚输入的这一个）。
            const merged = Array.from(new Set([...names, draft.trim()].filter(Boolean)));
            if (draft.trim()) onChange(merged.join(', '));
            setDraft('');
            onBlur?.(merged.join(', '));
          }}
        />
        <Popover
          open={open}
          onOpenChange={(next) => {
            setOpen(next);
            if (next) void ensureCatalog();
          }}
        >
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={disabled}
              className="shrink-0 rounded-full"
            >
              <ChevronsUpDown className="h-3.5 w-3.5" />
              {t('keys.modelPick')}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-[320px] p-2">
            <div className="mb-2 flex items-center gap-1.5 rounded-full bg-muted px-2.5 py-1">
              <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <input
                autoFocus
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t('keys.modelPickSearch')}
                className="w-full bg-transparent text-xs outline-none"
              />
            </div>
            <div className="mb-2 flex items-center gap-1.5">
              <button
                type="button"
                aria-pressed={freeOnly}
                onClick={() => setFreeOnly((v) => !v)}
                className={cn(
                  'rounded-full px-2 py-0.5 text-[11px] transition-colors',
                  freeOnly ? 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
                    : 'bg-muted text-muted-foreground hover:text-foreground',
                )}
              >
                {t('keys.freeOnly')}
              </button>
              {freeOnly && filtered.length > 0 && (
                <button
                  type="button"
                  onClick={() => emit([...names, ...filtered.flatMap((g) => g.models.map((m) => m.id))])}
                  className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
                >
                  {t('keys.freeSelectAll')}
                </button>
              )}
            </div>
            <div className="max-h-[260px] overflow-y-auto">
              {failed && (
                <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                  {t('keys.modelPickUnavailable')}
                </p>
              )}
              {!failed && !catalog && (
                <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                  {t('keys.modelPickLoading')}
                </p>
              )}
              {!failed && catalog && filtered.length === 0 && (
                <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                  {t('keys.modelPickEmpty')}
                </p>
              )}
              {filtered.map((group) => (
                <div key={group.key} className="mb-1">
                  <div className="px-2 py-1 text-[11px] text-muted-foreground">{group.label}</div>
                  {group.models.map((m) => {
                    const picked = names.includes(m.id);
                    return (
                      <button
                        key={m.id}
                        type="button"
                        role="checkbox"
                        aria-checked={picked}
                        onClick={() => (picked ? remove(m.id) : add(m.id))}
                        className={cn(
                          'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs',
                          'hover:bg-muted',
                          picked && 'bg-muted/70',
                        )}
                      >
                        <span
                          className={cn(
                            'flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border',
                            picked ? 'border-primary bg-primary text-primary-foreground' : 'border-border',
                          )}
                        >
                          {picked && <Check className="h-2.5 w-2.5" />}
                        </span>
                        <span className="min-w-0 flex-1 truncate font-mono">{m.id}</span>
                        {isFree(m) && (
                          <span className="shrink-0 rounded-full bg-emerald-500/15 px-1.5 text-[10px] text-emerald-700 dark:text-emerald-300">
                            {t('keys.freeBadge')}
                          </span>
                        )}
                        {typeof m.context_length === 'number' && (
                          <span className="shrink-0 text-[11px] text-muted-foreground">
                            {Math.round(m.context_length / 1000)}k
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              ))}
            </div>
          </PopoverContent>
        </Popover>
      </div>
    </div>
  );
}

function splitNames(value: string): string[] {
  return Array.from(new Set(
    value.split(/[\n,]/).map((s) => s.trim()).filter(Boolean),
  ));
}

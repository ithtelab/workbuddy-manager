'use client';

import {useEffect, useState} from 'react';
import {ChevronRight, FileText, Loader2, Package, TriangleAlert} from 'lucide-react';

import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {systemApi} from '@/lib/api';
import {RichText} from '@/lib/i18n/rich-text';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import type {Changelog, ChangelogItem} from '@/lib/types';

const REPO_URL = 'https://github.com/ithtelab/workbuddy-manager/releases';

/** 分类配色，让「安全」「修复」这类一眼可辨 */
const SECTION_STYLE: Record<string, string> = {
  安全: 'border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400',
  新增: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  修复: 'border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400',
  改进: 'border-sky-500/30 bg-sky-500/10 text-sky-600 dark:text-sky-400',
  说明: 'border-border bg-muted text-muted-foreground',
  计划中: 'border-violet-500/30 bg-violet-500/10 text-violet-600 dark:text-violet-400',
};

/**
 * 分类标题 → i18n 键。
 *
 * 分类名来自服务端解析 CHANGELOG.md 的小标题（固定的几个），属**数据**；
 * 按原值映射到译文。条目正文仍按仓库里写的中文原样展示——那是文档内容，
 * 翻译它属于改 CHANGELOG.md 本身，不在界面适配范围内。
 */
const SECTION_LABEL_KEYS: Record<string, string> = {
  安全: 'changelog.sectionSecurity',
  新增: 'changelog.sectionAdded',
  修复: 'changelog.sectionFixed',
  改进: 'changelog.sectionImproved',
  说明: 'changelog.sectionNotes',
  计划中: 'changelog.sectionPlanned',
};

function sectionLabel(title: string, t: (key: string) => string): string {
  const key = SECTION_LABEL_KEYS[title];
  return key ? t(key) : title;
}

/**
 * 轻量行内渲染：只处理 `**加粗**` 与 `` `代码` ``，不引入 markdown 依赖。
 * 更新日志格式固定，用不着完整解析器——少一个依赖，离线包也更小。
 */
function InlineText({text}: {text: string}) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return (
    <>
      {parts.map((part, i) => {
        if (part.startsWith('**') && part.endsWith('**')) {
          return (
            <strong key={i} className="font-medium text-foreground">
              {part.slice(2, -2)}
            </strong>
          );
        }
        if (part.startsWith('`') && part.endsWith('`')) {
          return (
            <code key={i} className="rounded bg-muted px-1 py-0.5 font-mono text-[11px]">
              {part.slice(1, -1)}
            </code>
          );
        }
        return <span key={i}>{part}</span>;
      })}
    </>
  );
}

function ItemList({items}: {items: ChangelogItem[]}) {
  const hasSub = items.some((it) => it.level > 0);
  return (
    <ul className={cn('space-y-1.5', hasSub && 'space-y-2')}>
      {items.map((it, i) => (
        <li
          key={i}
          className={cn(
            'flex gap-2 text-xs leading-5 text-muted-foreground',
            it.level > 0 && 'ml-3.5 border-l border-border pl-2.5',
          )}
        >
          <span
            className={cn(
              'mt-[7px] h-1 w-1 shrink-0 rounded-full',
              it.level > 0 ? 'bg-muted-foreground/40' : 'bg-muted-foreground/70',
            )}
          />
          <span className="min-w-0 flex-1 break-words">
            <InlineText text={it.text} />
          </span>
        </li>
      ))}
    </ul>
  );
}

function VersionBlock({
  index,
  version,
  date,
  unreleased,
  isCurrent,
  currentVersion,
  expanded,
  onToggle,
  sections,
}: {
  index: number;
  version: string;
  date: string;
  unreleased: boolean;
  isCurrent: boolean;
  currentVersion: string;
  expanded: boolean;
  onToggle: () => void;
  sections: Changelog['versions'][number]['sections'];
}) {
  const t = useT();
  const count = sections.reduce((n, s) => n + s.items.length, 0);
  // 当前版本置顶时也默认展开，方便一眼看到最新改了什么
  return (
    <div
      className={cn(
        'overflow-hidden rounded-[16px] border transition-colors',
        expanded ? 'border-border bg-background' : 'border-transparent bg-muted/50',
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left transition-colors hover:bg-muted/60"
      >
        <ChevronRight
          className={cn(
            'h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform',
            expanded && 'rotate-90',
          )}
        />
        <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-mono text-xs font-medium">
            {unreleased ? t('changelog.unreleased') : `v${version}`}
          </span>
          {isCurrent && !unreleased && (
            <Badge variant="secondary" className="h-5 px-1.5 text-[10px]">
              {t('changelog.currentVersion')}
            </Badge>
          )}
          {unreleased && (
            <Badge
              variant="outline"
              className="h-5 border-amber-500/40 bg-amber-500/10 px-1.5 text-[10px] text-amber-600 dark:text-amber-400"
            >
              {t('changelog.inProgress')}
            </Badge>
          )}
          {date && <span className="text-[11px] text-muted-foreground">{date}</span>}
        </span>
        {!expanded && (
          <span className="shrink-0 text-[11px] text-muted-foreground">
            {t('changelog.items', {count, n: count})}
          </span>
        )}
      </button>

      {expanded && (
        <div className="space-y-3.5 border-t border-border px-3.5 py-3.5">
          {sections.map((sec, si) => (
            <div key={si} className="space-y-2">
              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    'rounded-md border px-1.5 py-0.5 text-[10px] font-medium',
                    SECTION_STYLE[sec.title] ?? 'border-border bg-muted text-muted-foreground',
                  )}
                >
                  {sectionLabel(sec.title, t)}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  {t('changelog.items', {count: sec.items.length, n: sec.items.length})}
                </span>
              </div>
              <ItemList items={sec.items} />
            </div>
          ))}
          {isCurrent && currentVersion && !unreleased && (
            <div className="pt-0.5 text-[11px] text-muted-foreground">
              {t('changelog.thisIsCurrent')}
            </div>
          )}
          {index === 0 && !unreleased && (
            <div className="pt-0.5 text-[11px] text-muted-foreground">
              {t('changelog.seeAll')}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ChangelogPanel() {
  const t = useT();
  const [data, setData] = useState<Changelog | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    systemApi
      .changelog()
      .then((d) => {
        if (!alive) return;
        setData(d);
        if (!d.available) setError(d.error || t('changelog.unavailable'));
        else if (d.versions.length) setOpen(d.versions[0].version);
      })
      .catch(() => alive && setError(t('changelog.loadFailed')))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [t]);

  if (loading) {
    return (
      <div className="flex items-center justify-center gap-2 rounded-[20px] bg-muted py-16 text-xs text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t('changelog.loading')}
      </div>
    );
  }

  if (error || !data?.available) {
    return (
      <div className="flex items-start gap-2.5 rounded-[20px] border border-amber-500/30 bg-amber-500/10 p-4">
        <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
        <div className="space-y-1 text-xs">
          <div className="font-medium">{t('changelog.unavailable')}</div>
          <div className="text-muted-foreground">{error || data?.error}</div>
          <div className="text-muted-foreground">
            <RichText text={t('changelog.unavailableHint')} />
          </div>
        </div>
      </div>
    );
  }

  const currentVersion = (data.current || '').replace(/^v/i, '');

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium">
          <FileText className="h-4 w-4" />
          {t('changelog.title')}
          {currentVersion && (
            <span className="font-mono text-xs text-muted-foreground">
              {t('changelog.currentVersionLabel', {v: currentVersion})}
            </span>
          )}
        </div>
        <Button asChild variant="ghost" size="sm" className="h-7 gap-1.5 text-xs">
          <a href={REPO_URL} target="_blank" rel="noreferrer">
            <Package className="h-3.5 w-3.5" />
            Releases
          </a>
        </Button>
      </div>

      <div className="scroll-slim max-h-[calc(100dvh-260px)] min-h-[320px] space-y-2 overflow-y-auto pr-1">
        {data.versions.map((v, i) => (
          <VersionBlock
            key={v.version}
            index={i}
            version={v.version}
            date={v.date}
            unreleased={v.unreleased}
            isCurrent={!v.unreleased && v.version.replace(/^v/i, '') === currentVersion}
            currentVersion={currentVersion}
            expanded={open === v.version}
            onToggle={() => setOpen(open === v.version ? null : v.version)}
            sections={v.sections}
          />
        ))}
      </div>

      {data.truncated && (
        <p className="text-[11px] text-muted-foreground">
          {t('changelog.truncated', {n: data.versions.length, total: data.total ?? 0})}
        </p>
      )}
    </div>
  );
}

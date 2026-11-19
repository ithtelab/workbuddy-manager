'use client';

import {useEffect, useRef, useState} from 'react';
import {AnimatePresence, motion} from 'motion/react';
import {Brain, Check, ChevronUp, Send, Sparkles, Square} from 'lucide-react';

import {cn} from '@/lib/utils';
import {t} from '@/lib/i18n';
import {useT} from '@/lib/i18n/provider';

export interface ChatModelOption {
  id: string;
  name: string;
  efforts: string[];
  series: string;
}

/** 档位 → i18n 键；取值与上游 effortRank 对齐（off…max） */
const EFFORT_LABEL_KEYS: Record<string, string> = {
  off: 'effort.off',
  minimal: 'effort.minimal',
  low: 'effort.low',
  medium: 'effort.medium',
  high: 'effort.high',
  xhigh: 'effort.xhigh',
  max: 'effort.max',
};

/** 档位显示名（非组件环境也能用：读模块级当前语言）。 */
export function effortLabel(v: string): string {
  const key = EFFORT_LABEL_KEYS[v];
  return key ? t(key) : v;
}

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop?: () => void;
  streaming?: boolean;
  disabled?: boolean;
  /** 可选模型（含各自支持的推理档位） */
  models: ChatModelOption[];
  model: string;
  onModelChange: (id: string) => void;
  /** 推理档位；空串 = 不指定 */
  effort: string;
  onEffortChange: (v: string) => void;
  placeholders?: string[];
}

/**
 * 对话输入框。
 *
 * 视觉沿用 HextaUI 的 AIChatInput（失焦时循环淡入占位符、聚焦后展开、
 * 胶囊按钮），但做了两处必要改造：
 *   1. 配色改用本项目的主题 token —— 原组件写死白底黑字，暗色模式下会瞎
 *   2. 「Deep Search」换成**模型选择**（本项目没有联网检索能力），
 *      并把「Think」接到真实参数 reasoning_effort 上，而不是纯装饰
 */
export function AiChatInput({
  value,
  onChange,
  onSend,
  onStop,
  streaming = false,
  disabled = false,
  models,
  model,
  onModelChange,
  effort,
  onEffortChange,
  placeholders,
}: Props) {
  const t = useT();
  // 占位符跟随语言：默认值必须在组件内生成，否则模块加载时就固定成一种语言
  const hints = placeholders ?? ['chat.ph1', 'chat.ph2', 'chat.ph3', 'chat.ph4', 'chat.ph5'].map((k) => t(k));
  const [phIndex, setPhIndex] = useState(0);
  const [showPh, setShowPh] = useState(true);
  const [isActive, setIsActive] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [effortOpen, setEffortOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  const expanded = isActive || !!value || modelOpen || effortOpen;

  // 失焦且无内容时循环切换占位符
  useEffect(() => {
    if (expanded) return;
    const timer = setInterval(() => {
      setShowPh(false);
      setTimeout(() => {
        setPhIndex((p) => (p + 1) % hints.length);
        setShowPh(true);
      }, 400);
    }, 3200);
    return () => clearInterval(timer);
  }, [expanded, hints.length]);

  // 点击外部收起下拉（但不收起输入框本身，避免输入到一半被切走）
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setModelOpen(false);
        setEffortOpen(false);
        if (!value) setIsActive(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [value]);

  // 自适应高度：单行 44px 起，随内容长高，上限约 6 行
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 132) + 'px';
  }, [value]);

  const canSend = !!value.trim() && !disabled && !streaming;
  const current = models.find((m) => m.id === model);
  const supported = current?.efforts?.length ? current.efforts : [];

  function submit() {
    if (!canSend) return;
    onSend();
  }

  return (
    <div ref={wrapperRef} className="w-full">
      {/*
        外框不裁剪（原为 overflow-hidden）：模型/思考强度下拉是向上弹出的
        （bottom-full），若外框裁剪，面板会被整块切掉——实测面板 511~622
        而外框只到 669，可视区仅剩十几像素，点击后看起来"没有任何列表"。
        圆角改由内部两块各自处理，视觉不变。
      */}
      <motion.div
        layout
        className={cn(
          'w-full rounded-[28px] border bg-background transition-colors',
          expanded ? 'border-border shadow-lg' : 'border-border/60 shadow-sm',
        )}
        onClick={() => setIsActive(true)}
        transition={{type: 'spring', stiffness: 260, damping: 26}}
      >
        <div className="flex items-end gap-1.5 rounded-[28px] p-2 pl-3">
          {/* 文本输入 + 循环占位符 */}
          <div className="relative min-w-0 flex-1">
            <textarea
              ref={taRef}
              rows={1}
              value={value}
              disabled={disabled}
              onChange={(e) => onChange(e.target.value)}
              onFocus={() => setIsActive(true)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  submit();
                }
              }}
              className="scroll-slim block w-full resize-none border-0 bg-transparent py-2.5 text-sm leading-5 outline-none placeholder:text-transparent"
              style={{minHeight: 24}}
            />
            <div className="pointer-events-none absolute inset-0 flex items-center py-2.5">
              <AnimatePresence mode="wait">
                {showPh && !value && (
                  <motion.span
                    key={phIndex}
                    className="select-none whitespace-nowrap text-sm text-muted-foreground/70"
                    initial={{opacity: 0, filter: 'blur(10px)', y: 6}}
                    animate={{opacity: 1, filter: 'blur(0px)', y: 0}}
                    exit={{opacity: 0, filter: 'blur(10px)', y: -6}}
                    transition={{duration: 0.28}}
                  >
                    {hints[phIndex]}
                  </motion.span>
                )}
              </AnimatePresence>
            </div>
          </div>

          {/* 发送 / 停止 */}
          {streaming ? (
            <button
              type="button"
              onClick={onStop}
              title={t('chat.stop')}
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-foreground text-background transition-colors hover:opacity-90"
            >
              <Square className="h-3.5 w-3.5" fill="currentColor" />
            </button>
          ) : (
            <button
              type="button"
              onClick={submit}
              disabled={!canSend}
              title={t('chat.send')}
              className={cn(
                'flex h-9 w-9 shrink-0 items-center justify-center rounded-full transition-all',
                canSend
                  ? 'bg-foreground text-background hover:opacity-90'
                  : 'bg-muted text-muted-foreground/50',
              )}
            >
              <Send className="h-3.5 w-3.5" />
            </button>
          )}
        </div>

        {/* 展开区：模型选择 + 思考强度 */}
        <AnimatePresence initial={false}>
          {expanded && (
            <motion.div
              initial={{height: 0, opacity: 0}}
              animate={{height: 'auto', opacity: 1}}
              exit={{height: 0, opacity: 0}}
              transition={{duration: 0.2}}
              /*
                高度动画期间必须裁剪（否则内容会溢出外框）；
                但下拉是向上弹出的，若一直裁剪就会被切掉——所以下拉打开时
                切到 overflow-visible。此时高度动画早已结束，不会冲突。
              */
              className={modelOpen || effortOpen ? 'overflow-visible' : 'overflow-hidden'}
            >
              <div className="flex flex-wrap items-center gap-2 rounded-b-[28px] border-t border-border/50 px-3 py-2.5">
                {/* 模型选择 */}
                <div className="relative">
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setModelOpen((v) => !v);
                      setEffortOpen(false);
                    }}
                    className="flex items-center gap-1.5 rounded-full bg-muted px-3 py-1.5 text-[11px] font-medium transition-colors hover:bg-muted/70"
                  >
                    <Sparkles className="h-3.5 w-3.5 text-muted-foreground" />
                    <span className="max-w-[160px] truncate">
                      {current ? current.name || current.id : model || t('chat.selectModel')}
                    </span>
                    {effort && effort !== 'off' && (
                      <span className="text-muted-foreground">· {effortLabel(effort)}</span>
                    )}
                    <ChevronUp
                      className={cn('h-3 w-3 transition-transform', modelOpen && 'rotate-180')}
                    />
                  </button>

                  <AnimatePresence>
                    {modelOpen && (
                      <motion.div
                        initial={{opacity: 0, y: 6, scale: 0.98}}
                        animate={{opacity: 1, y: 0, scale: 1}}
                        exit={{opacity: 0, y: 6, scale: 0.98}}
                        transition={{duration: 0.15}}
                        className="scroll-slim absolute bottom-full left-0 z-50 mb-2 max-h-[280px] w-[320px] overflow-y-auto rounded-2xl border bg-popover p-1.5 shadow-xl"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {models.length === 0 && (
                          <div className="px-3 py-4 text-center text-[11px] text-muted-foreground">
                            {t('chat.noModels')}
                          </div>
                        )}
                        {models.map((m) => {
                          const on = m.id === model;
                          return (
                            <button
                              key={m.id}
                              type="button"
                              onClick={() => {
                                onModelChange(m.id);
                                // 换模型后若当前档位不被支持，收敛到最接近的
                                if (effort && m.efforts.length && !m.efforts.includes(effort)) {
                                  onEffortChange(m.efforts.includes('high') ? 'high' : m.efforts[m.efforts.length - 1]);
                                }
                                setModelOpen(false);
                              }}
                              className={cn(
                                'flex w-full items-start gap-2 rounded-xl px-2.5 py-2 text-left transition-colors',
                                on ? 'bg-muted' : 'hover:bg-muted/60',
                              )}
                            >
                              <span className="min-w-0 flex-1">
                                <span className="flex items-center gap-1.5">
                                  <span className="truncate text-xs font-medium">
                                    {m.name || m.id}
                                  </span>
                                  {m.series && (
                                    <span className="shrink-0 text-[11px] text-muted-foreground">
                                      {m.series}
                                    </span>
                                  )}
                                </span>
                                <span className="mt-0.5 block truncate font-mono text-[11px] text-muted-foreground">
                                  {m.id}
                                  {m.efforts.length > 0 && t('chat.reasoningSuffix', {efforts: m.efforts.join('/')})}
                                </span>
                              </span>
                              {on && <Check className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
                            </button>
                          );
                        })}
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>

                {/* 思考强度：只在该模型确实支持时才给出档位 */}
                <div className="relative">
                  <button
                    type="button"
                    disabled={supported.length === 0}
                    title={supported.length ? t('chat.effortTitle') : t('chat.effortUnsupported')}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (!supported.length) return;
                      setEffortOpen((v) => !v);
                      setModelOpen(false);
                    }}
                    className={cn(
                      'flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[11px] font-medium transition-colors',
                      supported.length
                        ? 'bg-muted hover:bg-muted/70'
                        : 'cursor-not-allowed bg-muted/50 text-muted-foreground/60',
                    )}
                  >
                    <Brain className="h-3.5 w-3.5 text-muted-foreground" />
                    {effort ? effortLabel(effort) : t('chat.effort')}
                    <ChevronUp
                      className={cn('h-3 w-3 transition-transform', effortOpen && 'rotate-180')}
                    />
                  </button>

                  <AnimatePresence>
                    {effortOpen && supported.length > 0 && (
                      <motion.div
                        initial={{opacity: 0, y: 6, scale: 0.98}}
                        animate={{opacity: 1, y: 0, scale: 1}}
                        exit={{opacity: 0, y: 6, scale: 0.98}}
                        transition={{duration: 0.15}}
                        className="absolute bottom-full left-0 z-50 mb-2 w-[150px] rounded-2xl border bg-popover p-1.5 shadow-xl"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <button
                          type="button"
                          onClick={() => {
                            onEffortChange('');
                            setEffortOpen(false);
                          }}
                          className={cn(
                            'flex w-full items-center justify-between rounded-xl px-2.5 py-1.5 text-left text-[11px] transition-colors',
                            !effort ? 'bg-muted' : 'hover:bg-muted/60',
                          )}
                        >
                          {t('chat.effortUnset')}
                          {!effort && <Check className="h-3 w-3" />}
                        </button>
                        {supported.map((e) => (
                          <button
                            key={e}
                            type="button"
                            onClick={() => {
                              onEffortChange(e);
                              setEffortOpen(false);
                            }}
                            className={cn(
                              'flex w-full items-center justify-between rounded-xl px-2.5 py-1.5 text-left text-[11px] transition-colors',
                              effort === e ? 'bg-muted' : 'hover:bg-muted/60',
                            )}
                          >
                            {effortLabel(e)}
                            {effort === e && <Check className="h-3 w-3" />}
                          </button>
                        ))}
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>

                <span className="ml-auto hidden text-[11px] text-muted-foreground sm:inline">
                  {t('chat.keyboardHint')}
                </span>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  );
}

export default AiChatInput;

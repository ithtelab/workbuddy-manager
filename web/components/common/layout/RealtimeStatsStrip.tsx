'use client';

/**
 * 用量统计页顶部的实时指标条：当前并发 + RPM + 近 3 分钟走势。
 *
 * 单独成组件而不是塞进页面里的原因：
 *
 *  - 刷新节奏与页面其余部分不同。汇总 / 趋势 / 分解表跟「时段选择器」走，60 秒
 *    心跳一次就够了；这两个数字是**此刻**的状态，5 秒才跟得上（上游在途数变化
 *    远快于分钟级聚合）。
 *  - 口径也与下面那排累计卡不同，见 `StatsRealtime` 的注释。
 *
 * 配色与同页 `StatCard` 的语义档对齐：并发 = warning（橙）、RPM = success（绿）。
 * 两条线共用一根纵轴并各自带峰值（`YAxis` 由两条曲线的最大值决定），所以数值
 * 更大的那条自然浮在上面，不需要第二条轴去「编」出一个高度关系。
 */
import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {Gauge, Zap} from 'lucide-react';
import {Area, ComposedChart, Line, ResponsiveContainer, Tooltip, YAxis} from 'recharts';

import {StatCard} from './StatCard';
import {statsApi} from '@/lib/api';
import type {StatsRealtime} from '@/lib/types';
import {fmtNumber} from '@/lib/format';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useT} from '@/lib/i18n/provider';

/**
 * 实时条的刷新间隔：1 秒。
 *
 * 这两个数是「此刻」的值：5 秒的间隔会让一条 0.4 秒的短请求整段落在两次采样之间，
 * 从头到尾看不见。1 秒足够跟上，单次请求也很轻（一条 /api/stats/realtime，库内三条
 * COUNT 加一次 4 分钟窗口扫描，毫秒级）。
 */
const POLL_MS = 1000;

/** 图区高度（px）。与 `StatCard` 的 96px 一起把卡片撑到与同行卡片等高 */
const CHART_H = 64;

interface ChartPoint {
  /** 桶起点的本地时间（HH:MM:SS），同时作为 tooltip 的标题 */
  label: string;
  conc: number;
  rpm: number;
}

function clockOf(epoch: number): string {
  const d = new Date(epoch * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function RealtimeStatsStrip({days}: {days: number}) {
  const t = useT();
  const [data, setData] = useState<StatsRealtime | null>(null);
  const [failed, setFailed] = useState(false);

  /**
   * 请求序号：只接受「最新一次」的响应。
   *
   * 换时段（`days` 变）会同时触发 `useEffect` 与心跳，两个请求在途时**先发的
   * 未必先回**；不带序号的话旧时段的数据会盖掉新时段的，界面上的峰值与下面的表
   * 就对不上，而且看起来像「数据在跳」。
   */
  const seq = useRef(0);

  /**
   * 同一时刻只允许一个请求在途（1 秒轮询 + 慢查询/慢网络时不堆积）。
   *
   * 心跳每 1 秒触发一次；如果某次 `/api/stats/realtime` 因为长时段扫描或网络
   * 抖动花了 3 秒，不加这道门就会叠出 3 个在途请求，越拖越糟。丢弃的这一次
   * 不影响正确性 —— 下一次心跳会再问。
   */
  const busy = useRef(false);

  const load = useCallback(() => {
    if (busy.current) return;
    busy.current = true;
    const ticket = ++seq.current;
    statsApi
      .realtime(days)
      .then((next) => {
        if (ticket !== seq.current) return;
        setData(next);
        setFailed(false);
      })
      .catch(() => {
        if (ticket !== seq.current) return;
        setFailed(true);
      })
      .finally(() => {
        busy.current = false;
      });
  }, [days]);

  // 换时段要立刻重取：留着上一个时段的峰值会和下面的表对不上
  useEffect(() => {
    load();
  }, [load]);
  useHeartbeat(load, POLL_MS);

  const series = data?.series;
  const points = useMemo<ChartPoint[]>(() => {
    if (!series) return [];
    const n = Math.min(series.rpm.length, series.concurrency.length);
    const out: ChartPoint[] = [];
    for (let i = 0; i < n; i += 1) {
      out.push({
        label: clockOf(series.start + i * series.step),
        conc: series.concurrency[i],
        rpm: series.rpm[i],
      });
    }
    return out;
  }, [series]);

  // 顶端留 6% 余量：峰值几乎触顶，小数值的起伏才看得出来
  const axisMax = useMemo(() => {
    const peak = points.reduce((m, p) => Math.max(m, p.conc, p.rpm), 0);
    return Math.max(peak, 1) * 1.06;
  }, [points]);

  const available = Boolean(data?.available);
  const peaks = data?.peaks;

  // 主数字是「上游槽位占用」，小字里带一个「客户端在途」：
  // 前者不含排队等槽的、也不含上游答完但尾巴还在写回客户端的；后者含。两者差得多
  // 就说明池子在排队或回传积压——那正是需要看见的时刻。
  const parts: string[] = [];
  if (data) {
    parts.push(t('stats.realtimeClientInflight', {n: fmtNumber(data.client_inflight)}));
    if (available) {
      parts.push(t('stats.realtimePeak', {n: fmtNumber(peaks?.concurrency)}));
      parts.push(t('stats.realtimeCapacity', {c: fmtNumber(data.capacity)}));
    } else if (data.error) {
      parts.push(data.error);
    }
  }
  const concHint = parts.join(' · ');

  const rpmHint = !data
    ? ''
    : `${t('stats.realtimePeak', {n: fmtNumber(peaks?.rpm)})} · ${t('stats.realtimeAverage', {
        v: fmtNumber(data.rpm_3m),
      })}`;

  return (
    <section className="grid grid-cols-2 gap-3 md:gap-4 lg:grid-cols-4">
      <StatCard
        label={t('stats.realtimeConcurrency')}
        value={available ? fmtNumber(data?.inflight) : '—'}
        hint={concHint}
        hintTone={data && !available ? 'danger' : undefined}
        icon={Zap}
        tone="warning"
        delay={0}
      />
      <StatCard
        label={t('stats.realtimeRpm')}
        value={data ? fmtNumber(data.rpm_60) : '—'}
        hint={rpmHint}
        hintTone={failed ? 'danger' : undefined}
        icon={Gauge}
        tone="success"
        delay={0.05}
      />
      <div className="wb-rt-strip col-span-2 min-h-[88px] sm:min-h-[96px] rounded-[20px] bg-muted px-3.5 py-3 sm:px-4">
        <div className="flex items-start justify-between gap-2">
          <div className="text-[11px] font-medium text-gray-500 dark:text-gray-400 truncate">
            {t('stats.realtimeTrend')}
          </div>
          <div className="wb-rt-conc h-6 w-6 shrink-0 rounded-full bg-white/70 dark:bg-white/[0.05] grid place-items-center">
            <Gauge className="h-3.5 w-3.5" />
          </div>
        </div>
        <div className="mt-1" style={{height: CHART_H}}>
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={points} margin={{top: 2, right: 0, bottom: 0, left: 0}}>
              <defs>
                <linearGradient id="wb-rt-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--wb-rt-conc)" stopOpacity={0.35} />
                  <stop offset="100%" stopColor="var(--wb-rt-conc)" stopOpacity={0} />
                </linearGradient>
              </defs>
              <YAxis hide domain={[0, axisMax]} />
              <Tooltip
                cursor={{stroke: 'var(--muted-foreground)', strokeOpacity: 0.25, strokeWidth: 1}}
                contentStyle={{
                  background: 'var(--popover)',
                  border: '1px solid var(--border)',
                  borderRadius: 10,
                  fontSize: 11,
                  padding: '4px 8px',
                  fontVariantNumeric: 'tabular-nums',
                }}
                labelStyle={{color: 'var(--muted-foreground)', fontSize: 11}}
                itemStyle={{padding: 0}}
                formatter={(value, name) => [
                  fmtNumber(Number(value)),
                  name === 'conc' ? t('stats.realtimeConcurrency') : t('stats.realtimeRpm'),
                ]}
              />
              <Area
                type="monotone"
                dataKey="conc"
                stroke="var(--wb-rt-conc)"
                strokeWidth={2}
                fill="url(#wb-rt-fill)"
                dot={false}
                activeDot={false}
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="rpm"
                stroke="var(--wb-rt-rpm)"
                strokeWidth={1.5}
                strokeDasharray="4 3"
                dot={false}
                activeDot={false}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </div>
    </section>
  );
}

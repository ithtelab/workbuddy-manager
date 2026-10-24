'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {
  Settings as SettingsIcon,
  RefreshCw,
  Plus,
  Trash2,
  Save,
  Users,
  Server,
  Shuffle,
  Info,
  TriangleAlert,
  ChevronDown,
  RotateCcw,
  PlugZap,
  Loader2,
  DownloadCloud,
  Database,
  FileText,
  KeyRound,
  Route,
} from 'lucide-react';
import {usePathname} from 'next/navigation';
import {notify} from '@/lib/toast';
import {getExpiryDailyGroup, setExpiryDailyGroup} from '@/lib/display-prefs';
import {useI18n} from '@/lib/i18n/provider';
import {t as tGlobal, tp as tpGlobal} from '@/lib/i18n';
import {RichText} from '@/lib/i18n/rich-text';
import {settingsApi, upstreamApi, errText} from '@/lib/api';
import {useAsyncAll} from '@/lib/use-async-data';
import {UpstreamEndpoints} from '@/components/settings/UpstreamEndpoints';
import {scheduleDelayError} from '@/lib/schedule-validation';
import {
  DEFAULT_SETTINGS_TAB,
  SETTINGS_TABS,
  SETTINGS_TAB_LABEL_KEYS,
  settingsTabFromPath,
  settingsTabHref,
  type SettingsTab,
} from '@/lib/settings-tabs';
import type {ModelInfo, ModelSource, UpstreamConfig, UserItem} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {SectionTabs, type SectionTabItem} from '@/components/common/layout/SectionTabs';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {ResetPasswordDialog} from '@/components/common/settings/ResetPasswordDialog';
import {useAuth} from '@/lib/auth-context';
import {UpdatePanel} from '@/components/common/settings/UpdatePanel';
import {PgSyncPanel} from '@/components/common/settings/PgSyncPanel';
import {ProxyPanel} from '@/components/common/settings/ProxyPanel';
import {TokensPanel} from '@/components/common/settings/TokensPanel';
import {ChangelogPanel} from '@/components/common/settings/ChangelogPanel';
import {UpstreamReloadNotice} from '@/components/common/settings/UpstreamReloadNotice';
import {CopyButton} from '@/components/ui/copy-button';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {Textarea} from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
// 只留 Tabs / TabsContent：导航那一行改成了 SectionTabs（每项是一条真实路由），
// 不再用 TabsList / TabsTrigger —— 它们是「同一页内的按钮」，地址栏不会跟着变。
import {Tabs, TabsContent} from '@/components/ui/tabs';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

/* ─────────────────────────────────────────────────────────
 * 可视化配置字段定义
 * 每个字段对应 workbuddy2api config.json 中的一个键，
 * 这里给出中文名称、白话说明与安全取值范围。
 * ───────────────────────────────────────────────────────── */

interface BoolField {
  key: string;
  kind: 'bool';
  label: string;
  desc: string;
  def: boolean;
}

interface NumField {
  key: string;
  kind: 'num';
  label: string;
  desc: string;
  unit?: string;
  min: number;
  max: number;
  /** 步进，缺省 1；小数参数用 0.1 */
  step?: number;
  def: number;
}

interface HoursField {
  key: string;
  /** 时刻数组：上游是 []int，如 [9, 21] 表示每天 9 点与 21 点执行 */
  kind: 'hours';
  label: string;
  desc: string;
  def: number[];
}

interface DurationField {
  key: string;
  /** 时长字符串：上游接受 30s / 10m / 2h 等 */
  kind: 'duration';
  label: string;
  desc: string;
  def: string;
  /**
   * 允许 `0` / 空串（上游语义：**关闭该特性**，不是格式错误）。
   *
   * 必须显式声明：`DURATION_RE` 只认「数字+单位」，`0` 与空串都过不了它。
   * 早先「留空或填 0 = 关闭」只写在 desc 里、没告诉校验器，于是用户按提示填 0
   * 想关掉优化，`pickValues` 又把它当成非法值**静默换回默认**（168h）——
   * 界面看不出任何异常，用户以为自己关掉了，其实一直开着。
   */
  offWhenZero?: boolean;
  /**
   * 允许留空（= 不启用该配置），但**不放行 `0`**。
   *
   * 与 `offWhenZero` 的差别就在 0 上：有的上游字段把 0 当「关闭」，有的把 0 当
   * **非法值**（`credit_exhausted` 就是——补丁里的 normalize 判 `d <= 0` 直接报错）。
   * 后者若照抄「留空或填 0」的说法，用户会得到「保存成功、上游起不来」。
   */
  allowEmpty?: boolean;
}

interface SelectField {
  key: string;
  /** 枚举字符串：上游只接受给定取值 */
  kind: 'select';
  label: string;
  desc: string;
  /** 危险项：填错会导致上游启动失败，界面上给显式警示 */
  caution?: string;
  options: {value: string; label: string}[];
  def: string;
}

interface TextField {
  key: string;
  /** 自由文本（文件路径之类），不做格式假设，仅禁止换行/控制字符 */
  kind: 'text';
  label: string;
  desc: string;
  caution?: string;
  placeholder?: string;
  def: string;
}

type Field =
  | BoolField
  | NumField
  | HoursField
  | DurationField
  | SelectField
  | TextField;

/** 时长格式校验：数字 + 单位（s/m/h，不含 d —— Go time.ParseDuration 不认 d） */
const DURATION_RE = /^\d+\s*(s|m|h)$/i;

/**
 * 一个时长字段的取值是否合法。
 *
 * `offWhenZero` 的字段额外接受 `0` 与空串（上游把两者都当「关闭」，见该字段注释）；
 * 其余字段仍要求严格的「数字+单位」。
 */
function durationOk(f: DurationField, raw: unknown): boolean {
  const text = String(raw ?? '').trim();
  if (f.offWhenZero && (text === '' || text === '0')) return true;
  // allowEmpty 只额外放行空串：`0` 仍走 DURATION_RE → 判非法（上游对它是报错）。
  if (f.allowEmpty && text === '') return true;
  return DURATION_RE.test(text);
}

/** 把时刻数组格式化为可读文本，如 [9,21] -> "9, 21" */
function hoursToText(v: unknown): string {
  if (Array.isArray(v)) {
    return v
      .filter((x): x is number => typeof x === 'number')
      .sort((a, b) => a - b)
      .join(', ');
  }
  return '';
}

/** 解析用户输入的时刻列表：返回 {ok, hours?, error?} */
function parseHours(text: string): {ok: boolean; hours: number[]; error?: string} {
  const parts = text.split(/[,，\s]+/).filter(Boolean);
  if (!parts.length) return {ok: false, hours: [], error: tGlobal('settings.errNeedOneHour')};
  const out: number[] = [];
  for (const p of parts) {
    const n = Number(p);
    if (!Number.isInteger(n) || n < 0 || n > 23) {
      return {ok: false, hours: [], error: tGlobal('settings.errHourRange', {v: p})};
    }
    if (!out.includes(n)) out.push(n);
  }
  return {ok: true, hours: out.sort((a, b) => a - b)};
}

const SCHEDULE_FIELDS: Field[] = [
  {
    key: 'account_delay_min_seconds',
    kind: 'num',
    label: '账号最短间隔',
    desc: '自动任务逐个处理账号，完成一个后再随机等待；建议最短 30 秒、最长 120 秒',
    unit: '秒',
    min: 0,
    max: 600,
    def: 0,
  },
  {
    key: 'account_delay_max_seconds',
    kind: 'num',
    label: '账号最长间隔',
    desc: '自动任务的账号随机间隔上限；两项填 0 保持原有间隔。开启后同一时刻的不同任务也依次运行，手动执行不受影响',
    unit: '秒',
    min: 0,
    max: 600,
    def: 0,
  },
  {
    key: 'random_delay_minutes',
    kind: 'num',
    label: '每日任务随机延迟',
    desc: '每天各类任务在原定整点后随机延迟 0～N 分钟；0 表示整点执行。同一天重启沿用原时刻，手动执行不受影响',
    unit: '分钟',
    min: 0,
    max: 60,
    def: 0,
  },
  {
    key: 'checkin_enabled',
    kind: 'bool',
    label: '自动签到',
    desc: '每天自动领取免费额度；签到时的余额查询还能解冻被冷却的账号',
    def: true,
  },
  {
    key: 'checkin_hours',
    kind: 'hours',
    label: '签到时刻',
    desc: '在哪些整点执行签到（0-23，可多个）。签到时同时刷新余额并解冻冷却账号',
    def: [9, 21],
  },
  {
    key: 'travel_enabled',
    kind: 'bool',
    label: '猫猫旅行',
    desc: '自动推进「猫猫旅行」：领养 / 派出 / 领取到站奖励，可获得积分',
    def: true,
  },
  {
    key: 'travel_hours',
    kind: 'hours',
    label: '旅行时刻',
    desc: '在哪些整点推进旅行（0-23，可多个）。默认两趟闭环：早上领奖并派出，晚上领当日奖励',
    def: [9, 21],
  },
  {
    key: 'activity_enabled',
    kind: 'bool',
    label: '活跃上报',
    desc: '每日上报一次对话活跃，点亮连续登录并解锁领养前置任务（领猫需要）',
    def: true,
  },
  {
    key: 'activity_hours',
    kind: 'hours',
    label: '上报时刻',
    desc: '在哪些整点上报（0-23，可多个）。每号每天一次即可，重复上报无额外收益',
    def: [10],
  },
  {
    key: 'keepalive_enabled',
    kind: 'bool',
    label: '自动保活',
    desc: '定期刷新登录令牌，避免账号因长期闲置掉线',
    def: true,
  },
  {
    key: 'keepalive_hours',
    kind: 'hours',
    label: '保活时刻',
    desc: '在哪些整点刷新令牌（0-23，可多个）',
    def: [22],
  },
  {
    key: 'school_enabled',
    kind: 'bool',
    label: '开学季任务',
    desc: '自动完成开学季任务中心的活动并抽空抽奖余额（仅国内版；国际版由脚本内部跳过）',
    def: true,
  },
  {
    key: 'school_hours',
    kind: 'hours',
    label: '开学季时刻',
    desc: '在哪些整点执行开学季任务（0-23，可多个）。活动未开启时会自动跳过，不算失败',
    def: [12],
  },
  {
    key: 'cat_enabled',
    kind: 'bool',
    label: '夜猫任务',
    desc: '在夜猫窗口（23:00–08:00）补做一次夜猫任务；窗口外会自动跳过',
    def: true,
  },
  {
    key: 'cat_hours',
    kind: 'hours',
    label: '夜猫时刻',
    desc: '在哪些整点尝试夜猫任务（0-23，可多个）。默认凌晨 1 点；窗口内每天最多补一次',
    def: [1],
  },
  {
    key: 'activity_report_count',
    kind: 'num',
    label: '每次上报条数',
    desc: '每个账号每次活跃上报发几条。领养猫需要 5 次对话，默认 5 条一次刷满；填 1 即旧行为',
    unit: '条',
    min: 1,
    max: 20,
    def: 5,
  },
];

const COOLDOWN_FIELDS: Field[] = [
  {
    key: 'soft_rate',
    kind: 'duration',
    label: '软限流冷却基数',
    desc: '被腾讯限流后，账号冷却多久。数值越大越保守（格式如 600s / 10m / 1h）',
    def: '600s',
  },
  {
    key: 'soft_rate_max',
    kind: 'duration',
    label: '冷却退避上限',
    desc: '连续触发限流会逐次延长冷却，这是延长后的封顶值（格式如 2h）',
    def: '2h',
  },
  {
    key: 'credit_exhausted',
    kind: 'duration',
    // 这里「留空」与「填 0」是两回事：空 = 默认行为，0 = 上游判非法。desc 必须把
    // 「不能填 0」写出来，否则用户按「留空或 0 关闭」的习惯填 0 会被判格式错误。
    label: '积分耗尽冷却',
    desc: '账号积分用完后的冷却时长。留空 = 冷却到次日 4 点、等签到恢复（默认）；'
      + '填了就只退出这一小段（如 10m / 1h），到期自动回到可用池。不能填 0',
    def: '',
    allowEmpty: true,
  },
];

const POOL_FIELDS: Field[] = [
  {
    key: 'max_in_flight',
    kind: 'num',
    label: '单账号最大并发',
    desc: '一个账号同时处理几个请求。调大能提高吞吐，但更容易触发腾讯限流（0 = 不限制）',
    unit: '个',
    min: 0,
    max: 32,
    def: 3,
  },
  {
    key: 'max_in_flight_global',
    kind: 'num',
    label: '国际版单账号最大并发',
    // 语义与 max_in_flight **不同**（上游如此）：那边 0 = 不限制，这边 0/负数
    // = 未设置、回落默认 2（上游 normalize 逻辑，见其 config.go:319）。
    // 写成「不填则跟随」会误导 —— 它并不跟随上面的值。
    desc: '国际版单独用这个上限。国际版的风控更严，官方默认压到 2——'
      + '并发越高越容易被判为异常流量。填 0 表示用默认值 2（注意与上面那个「0 = 不限制」不同）',
    unit: '个',
    min: 0,
    // 上限与后端 _INT_RANGES 的 max_in_flight_global 保持一致（0-64）：
    // 不一致会出现「JSON 里能设、表单编辑时被拒」的矛盾。
    max: 64,
    def: 2,
  },
  {
    key: 'breaker_threshold',
    kind: 'num',
    label: '连续失败熔断阈值',
    desc: '某账号连续失败多少次后，自动暂停使用它一段时间',
    unit: '次',
    min: 1,
    max: 100,
    def: 3,
  },
  {
    key: 'breaker_cooldown',
    kind: 'duration',
    label: '熔断基础冷却',
    desc: '被熔断的账号先等待多久（格式如 30m / 1h）',
    def: '30m',
  },
  {
    key: 'breaker_cooldown_max',
    kind: 'duration',
    label: '熔断冷却上限',
    desc: '反复熔断会指数退避延长，这是封顶值（格式如 6h）',
    def: '6h',
  },
  {
    key: 'idle_weight_per_hour',
    kind: 'num',
    label: '闲置补偿 / 小时',
    desc: '账号每闲置 1 小时增加一点调度权重，让久未使用的账号优先被选中',
    min: 0,
    max: 10,
    step: 0.1,
    def: 0.5,
  },
  {
    key: 'idle_weight_max',
    kind: 'num',
    label: '闲置补偿上限',
    desc: '闲置加成的封顶值，避免某个账号权重无限增大',
    min: 0,
    max: 100,
    step: 0.5,
    def: 5,
  },
  {
    key: 'expiring_soon',
    kind: 'duration',
    label: '快过期积分窗口',
    desc: '到期时间落在此窗口内的积分会被标记为「快过期」，选号时优先消耗掉，避免白白过期。留空或填 0 = 关闭该优化',
    offWhenZero: true,
    def: '168h',
  },
  {
    key: 'cost_explore_interval',
    kind: 'duration',
    label: '成本档位探索周期',
    // 语义偏「调优」而非「必需」，如实说清代价：探索期间会改走成本更低的档位，
    // 因此可能与平时选到的账号不同。默认 30m 相当于每模型每天最多 48 次。
    desc: '上游的选号优化：长时间只用高成本档位时，每隔这么久会改走一次低成本档位探测，'
      + '成功就留在低档（省钱）。留空 = 用上游默认 30m，填 0 = 关闭该优化',
    offWhenZero: true,
    def: '30m',
  },
];

const FEATURES_FIELDS: Field[] = [
  {
    key: 'sanitize_blacklist_fingerprints',
    kind: 'bool',
    label: '出站请求指纹脱敏',
    desc: '对发往上游的请求做轻量脱敏，降低被判定异常的概率。除非在排查问题，否则建议保持开启',
    def: true,
  },
  {
    key: 'reasoning_as_content',
    kind: 'bool',
    label: '思考内容兜底为正文',
    desc: '模型在思考模式下正文为空时，把推理原文同时作为正文下发，避免只看正文的客户端'
      + '显示空回复。代价：同时渲染推理与正文的客户端会把同一段话显示两遍'
      + '（表现为「推理变成了正文」）。客户端能显示推理时建议关闭',
    def: true,
  },
];

const SESSION_FIELDS: Field[] = [
  {
    key: 'enabled',
    kind: 'bool',
    label: '会话粘性',
    desc: '同一会话的连续请求尽量路由到同一账号，多轮对话更连贯（多实例部署时依赖 Redis）',
    def: true,
  },
  {
    key: 'ttl',
    kind: 'duration',
    label: '会话保持时长',
    desc: '一次会话多久没活动就解除绑定（格式如 30m / 1h）',
    def: '30m',
  },
  {
    key: 'gc_interval',
    kind: 'duration',
    label: '会话清理周期',
    desc: '后台多久清理一次过期会话（格式如 5m / 10m）',
    def: '5m',
  },
];

const PROMPT_FIELDS: Field[] = [
  {
    key: 'mode',
    kind: 'select',
    label: '系统提示词模式',
    desc: 'passthrough：原样透传客户端传来的 system 消息；custom：网关用自有提示词替换它；'
      + 'append：两者并用——在开头连续的 system/developer 块之后插入网关 system，既有消息逐字不动',
    caution:
      '默认 passthrough 会把下游的 system prompt 原样送给上游。若你依赖网关自己的提示词来稳定行为（或避免 system 指纹被判异常），请改为 custom；'
      + '若既要保留客户端原始 system、又要网关的提示词生效，用 append。',
    options: [
      {value: 'passthrough', label: 'passthrough（透传客户端 system，默认）'},
      {value: 'append', label: 'append（保留客户端 system，其后插入网关提示词）'},
      {value: 'custom', label: 'custom（替换为网关提示词）'},
    ],
    def: 'passthrough',
  },
  {
    key: 'file',
    kind: 'text',
    label: '自定义提示词文件',
    desc: '在 custom 与 append 模式下生效。留空使用内置默认提示词',
    caution: '路径必须存在于上游容器内且可读；写错会导致上游启动失败、反代不可用。不确定就留空。',
    placeholder: '留空 = 使用内置默认',
    def: '',
  },
];

/**
 * `server` 段（旧版上游的请求体上限）。
 *
 * 上游 9d1a21b **移除了** `server.max_body_mb`（其 chat handler 不再预拦截请求体）。
 * 这里保留字段是为了**兼容仍跑旧版上游的部署**：那些部署的这个键依然生效，界面上
 * 还能调。新建部署在新上游上会看到它「未生效」的提示。
 *
 * 本端自己的请求体上限是另一个东西（`WB_GATEWAY_MAX_BODY_MB`，默认 32 MB）——
 * 那是本网关读进内存前必须有的保护，与上游无关，因此不在这里暴露（要改改环境变量）。
 */
const SERVER_FIELDS: Field[] = [
  {
    key: 'max_body_mb',
    kind: 'num',
    label: '请求体上限（旧版上游）',
    desc: '仅对**旧版**上游生效：上游 9d1a21b 起已移除该配置、不再限制请求体大小。'
      + '本网关自身的入站上限由环境变量 WB_GATEWAY_MAX_BODY_MB 控制（默认 32 MB）',
    unit: 'MB',
    min: 1,
    max: 256,
    def: 8,
  },
];

const UPSTREAM_FIELDS: Field[] = [
  {
    key: 'user_agent',
    kind: 'text',
    label: '出站 User-Agent',
    desc: '网关向腾讯发起请求时使用的客户端标识，留空用内置默认（已对齐官方 WorkBuddy）',
    placeholder: '留空 = 内置默认',
    def: '',
  },
  {
    key: 'client_name',
    kind: 'text',
    label: '客户端名称',
    desc: '用量归属头（X-Product / X-IDE-Name / X-IDE-Type / X-IDE-Version）的取值，影响官网「使用端」显示。留空 = 对齐官方桌面端（WorkBuddy）；填 SaaS 可还原旧行为',
    placeholder: '留空 = WorkBuddy（对齐官方桌面端）',
    def: '',
  },
  {
    key: 'client_version',
    kind: 'text',
    label: '客户端版本',
    desc: '出站 UA 里 WorkBuddy/<版本> 这段，也用于 X-IDE-Version 头。留空 = 内置默认（对齐官方分发包）',
    placeholder: '留空 = 内置默认',
    def: '',
  },
  {
    key: 'cli_version',
    kind: 'text',
    label: 'CLI 版本',
    desc: '出站 UA 里 CLI/<版本> 这段。留空 = 内置默认',
    placeholder: '留空 = 内置默认',
    def: '',
  },
  {
    key: 'device_token_file',
    kind: 'text',
    label: '设备 Token 文件',
    desc: '宿主上存放 device token 的文件路径，留空则不读文件。上游每 5 分钟读一次，读失败自动忽略',
    placeholder: '留空 = 不读文件',
    def: '',
  },
  {
    key: 'passthrough_ip',
    kind: 'bool',
    label: '透传客户端 IP',
    desc: '是否把客户端 IP（X-Forwarded-For / X-Real-IP）透传给上游。缺省关闭——不把内网/代理 IP 暴露给上游',
    def: false,
  },
];

/**
 * 国际版（global）配置。
 *
 * 上游从 2026-09-14 起单实例同时支持国内版与国际版：账号按自身 realm 路由，
 * global.enabled=false 是「锁死纯 CN」的逃生门（关掉后国际版账号会被当 CN 打向
 * 国内端点，上游文档称之为配置错误）。两个 base 留空即用 https://www.workbuddy.ai。
 */
const GLOBAL_FIELDS: Field[] = [
  {
    key: 'enabled',
    kind: 'bool',
    label: '启用国际版路由',
    desc: '开启后，realm=global 的账号走 workbuddy.ai（国际版模型与端点）。关闭 = 锁死纯 CN：此时国际版账号会被当成国内版账号发往国内端点，属配置错误',
    def: true,
  },
  {
    key: 'chat_base',
    kind: 'text',
    label: '国际版 Chat 基址',
    desc: '国际版的聊天 / 登录 / 模型接口基址，留空用内置默认 https://www.workbuddy.ai',
    placeholder: '留空 = https://www.workbuddy.ai',
    def: '',
  },
  {
    key: 'billing_base',
    kind: 'text',
    label: '国际版 Billing 基址',
    desc: '国际版的积分 / trial 接口基址，留空用内置默认 https://www.workbuddy.ai',
    placeholder: '留空 = https://www.workbuddy.ai',
    def: '',
  },
];

/**
 * 账号管理接口（上游 `admin` 段）。
 *
 * 这个开关决定账号页「临时停用」走哪条路，所以文案必须写清两条路的差别——
 * 用户点停用时看到的提示会随它变化，不先说清楚会以为是 bug（实测有过反馈：
 * 升级后点停用弹出一句「账号将完全退出账号池，签到与保活也会一并停止」，
 * 用户不知道自己该不该开这个开关）。
 */
const ADMIN_FIELDS: Field[] = [
  {
    key: 'enabled',
    kind: 'bool',
    label: '启用账号管理接口',
    desc: '让「临时停用」只摘除转发流量（签到 / 保活照常、积分与凭证保持活跃）。'
      + '不开启时停用会回退为「改文件名」，账号会完全退出账号池、任务一并停止。'
      + '**需重启上游容器生效**；且上游要求 api_key 非空，否则拒绝启动',
    def: false,
  },
];

type Group = 'schedule' | 'prompt' | 'cooldown' | 'features' | 'session' | 'pool' | 'server' | 'upstream' | 'global' | 'admin';

/** 高级 JSON 编辑器里可直写的上游配置段（与后端 _EDITABLE_SECTIONS 一致） */
type WireSection =
  | 'schedule'
  | 'cooldown'
  | 'pool'
  | 'features'
  | 'session_sticky'
  | 'prompt'
  | 'server'
  | 'upstream'
  | 'global'
  | 'admin';

interface GroupDef {
  id: Group;
  /** 对应的上游 config.json 段名（UI 名与段名不一致时由此映射） */
  section: WireSection;
  title: string;
  desc: string;
  fields: Field[];
}

const GROUPS: GroupDef[] = [
  {
    id: 'schedule',
    section: 'schedule',
    title: '定时任务',
    desc: '六类任务各自独立排程：签到 / 猫猫旅行 / 活跃上报 / 保活 / 开学季 / 夜猫。可分别开关并设置执行时刻。签到、旅行、开学季、夜猫**只对国内版账号生效**——国际版没有这些体系；**活跃上报与保活两边都执行**，国际版没有签到，活跃上报是它唯一能刷新活跃状态的动作',
    fields: SCHEDULE_FIELDS,
  },
  {
    id: 'prompt',
    section: 'prompt',
    title: '系统提示词',
    desc: '网关如何对待客户端传来的 system / developer 消息',
    fields: PROMPT_FIELDS,
  },
  {
    id: 'cooldown',
    section: 'cooldown',
    title: '限流与冷却',
    desc: '被腾讯限流后的冷却策略',
    fields: COOLDOWN_FIELDS,
  },
  {
    id: 'features',
    section: 'features',
    title: '功能开关',
    desc: '上游的进阶行为开关',
    fields: FEATURES_FIELDS,
  },
  {
    id: 'session',
    section: 'session_sticky',
    title: '会话粘性',
    desc: '多轮对话的路由粘性与清理策略',
    fields: SESSION_FIELDS,
  },
  {
    id: 'pool',
    section: 'pool',
    title: '并发与熔断',
    desc: '控制账号池的并发能力与故障保护',
    fields: POOL_FIELDS,
  },
  {
    id: 'server',
    section: 'server',
    title: '请求上限（旧版上游）',
    desc: '仅当上游仍是 9d1a21b 之前的版本时生效',
    fields: SERVER_FIELDS,
  },
  {
    id: 'upstream',
    section: 'upstream',
    title: '出站标识',
    desc: '网关向腾讯发起请求时的客户端标识',
    fields: UPSTREAM_FIELDS,
  },
  {
    id: 'global',
    section: 'global',
    title: '国际版',
    desc: '国际版（workbuddy.ai）路由开关与基址。关闭即锁死纯 CN',
    fields: GLOBAL_FIELDS,
  },
  {
    id: 'admin',
    section: 'admin',
    title: '账号管理接口',
    desc: '账号页的「临时停用」走哪条路。**开启后**上游提供停用接口：停用只摘除转发流量，'
      + '签到与令牌保活照常执行（积分与凭证保持活跃）；不开启则回退为改文件名，'
      + '那会让账号完全退出账号池、任务也一并停止。'
      + '改动后**需重启上游容器**才生效（上游只在启动时读这个开关）。',
    fields: ADMIN_FIELDS,
  },
];

type FieldValue = boolean | number | string;

function defaultValues(fields: Field[]): Record<string, FieldValue> {
  const out: Record<string, FieldValue> = {};
  for (const f of fields) out[f.key] = f.kind === 'hours' ? hoursToText(f.def) : f.def;
  return out;
}

/** 从配置中取出某个分组的已知字段（缺失或类型不符时回退到默认值） */
function pickValues(fields: Field[], source: Record<string, unknown> | undefined): Record<string, FieldValue> {
  const out: Record<string, FieldValue> = {};
  for (const f of fields) {
    const raw = source?.[f.key];
    switch (f.kind) {
      case 'bool':
        out[f.key] = typeof raw === 'boolean' ? raw : f.def;
        break;
      case 'num': {
        const n = typeof raw === 'number' ? raw : Number(raw);
        out[f.key] = Number.isFinite(n) ? n : f.def;
        break;
      }
      case 'hours':
        // 上游为 []int；表单里用 "9, 21" 这样的字符串承载，保存时再解析回数组
        out[f.key] = Array.isArray(raw) ? hoursToText(raw) : hoursToText(f.def);
        break;
      case 'duration':
        out[f.key] = durationOk(f, raw) ? String(raw).trim() : f.def;
        break;
      case 'select':
        // 只接受枚举内的取值；配置里是别的值（上游改过枚举）时回退默认
        out[f.key] = typeof raw === 'string' && f.options.some((o) => o.value === raw) ? raw : f.def;
        break;
      case 'text':
        out[f.key] = typeof raw === 'string' ? raw : f.def;
        break;
    }
  }
  return out;
}

/** 文本类字段的即时校验（用于输入框下方提示，不阻塞输入） */
function fieldError(f: Field, raw: FieldValue): string | undefined {
  if (f.kind === 'num') {
    // 输入框的 min/max 只是浏览器属性，不参与提交校验，这里显式检查
    const n = Number(raw);
    if (!Number.isFinite(n)) return tGlobal('settings.errNumber');
    if (n < f.min || n > f.max) {
      return tGlobal('settings.errRange', {
        min: f.min ?? '',
        max: f.max ?? '',
        unit: f.unit ? tpGlobal(f.unit) : '',
      });
    }
    return undefined;
  }
  if (f.kind === 'hours') {
    const r = parseHours(String(raw));
    return r.ok ? undefined : r.error;
  }
  if (f.kind === 'duration') {
    return durationOk(f, raw) ? undefined : tGlobal('settings.errDuration');
  }
  if (f.kind === 'text' && /[\r\n\u0000-\u001f]/.test(String(raw))) {
    return tGlobal('settings.errNoNewline');
  }
  return undefined;
}

/** 把表单值转换成要写入上游 config.json 的值 */
function toWire(
  f: Field,
  raw: FieldValue,
): {ok: true; value: boolean | number | number[] | string} | {ok: false; error: string} {
  if (f.kind === 'hours') {
    const r = parseHours(String(raw));
    return r.ok ? {ok: true, value: r.hours} : {ok: false, error: r.error ?? tGlobal('settings.errHourFormat')};
  }
  if (f.kind === 'duration') {
    const t = String(raw).trim();
    return durationOk(f, t)
      ? {ok: true, value: t}
      : {ok: false, error: tGlobal('settings.errDuration')};
  }
  if (f.kind === 'num') {
    const n = Number(raw);
    if (!Number.isFinite(n)) return {ok: false, error: tGlobal('settings.errNumber')};
    if (n < f.min || n > f.max) {
      return {
        ok: false,
        error: tGlobal('settings.errRange', {
          min: f.min ?? '',
          max: f.max ?? '',
          unit: f.unit ? tpGlobal(f.unit) : '',
        }),
      };
    }
    return {ok: true, value: n};
  }
  if (f.kind === 'select') {
    const v = String(raw);
    return f.options.some((o) => o.value === v)
      ? {ok: true, value: v}
      : {ok: false, error: tGlobal('settings.errEnum')};
  }
  if (f.kind === 'text') {
    const t = String(raw).trim();
    if (/[\r\n\u0000-\u001f]/.test(t)) return {ok: false, error: tGlobal('settings.errNoNewline')};
    return {ok: true, value: t};
  }
  return {ok: true, value: raw};
}

const FIELD_BY_KEY: Record<string, Field> = {};
for (const g of GROUPS) for (const f of g.fields) FIELD_BY_KEY[f.key] = f;

/**
 * 界面归在某组、但在上游 config.json 里是**顶层**键的字段。
 *
 * 目前只有 reasoning_as_content（思考兜底）：它在上游是根级布尔，塞进
 * features 段就等于写到错误路径、改了不生效。提交时必须提到顶层。
 */
const TOP_LEVEL_KEYS = new Set(['reasoning_as_content']);

/**
 * 设置页的外壳（批次 4 的 P1-2：7 个 Tab 变成可寻址子路由）。
 *
 * **为什么外壳是 `layout.tsx` 而不是 `page.tsx`**：`/settings/upstream` 与
 * `/settings/models` 是两条不同的路由，App Router 在两者之间跳转时会**重挂载**
 * 子路由的 page 组件——而本页的配置表单、乐观值、`original` 基线全都是组件内部
 * 状态，重挂载就等于每切一次 Tab 都重新拉一次配置、闪一次骨架，还会把用户没保存
 * 的编辑丢掉。layout 在同一个段（`settings`）下**不重挂载**，所以取数与表单状态
 * 放在这里才活得下来。
 *
 * 代价是那 7 个子路由的 `page.tsx` 不渲染内容，只负责让 URL 存在（各自文件里有
 * 一行说明）。这不是疏忽：内容一旦交给子路由渲染，就必须把上面那几十项状态
 * 提到 context 里再发下去——那会把本仓库最复杂的一页整体拆开，风险远大于收益，
 * 而用户能看到的行为（可分享、可收藏、可后退、切 Tab 不重取数据）完全一样。
 */
export default function SettingsLayout({children}: {children: React.ReactNode}) {
  const {t, tp} = useI18n();
  const {isAdmin} = useAuth();
  /**
   * 当前 Tab 只由**路径**决定（`/settings/<tab>`）。
   *
   * 不再用 `Tabs` 的内部状态：那样地址栏不会变，链接分享出去只会落到第一个 Tab。
   * `settingsTabFromPath` 认不出时回落到默认 Tab —— `/settings` 这一层由同级的
   * `page.tsx` 把地址换成规范路径，所以这里只是「换地址那一帧」的兜底。
   */
  const tab: SettingsTab = settingsTabFromPath(usePathname()) ?? DEFAULT_SETTINGS_TAB;

  /**
   * 二级导航。顺序与图标取自原来的 `TabsList`（一处不差），只是每项从「一个
   * 组件内部的按钮」变成「一条真实路由」。
   *
   * 清单本身在 `@/lib/settings-tabs`：那边的 `SETTINGS_TABS` 同时被
   * `server/tests/test_settings_tabs.py` 用来断言「每一项都有真实存在的路由目录」。
   */
  // 只留图标：标签键搬到了 `@/lib/settings-tabs`（命令面板也要用同一份，
  // 留在外壳里就得在面板那边再抄一遍，漏抄一处不报错）。
  const TAB_META: Record<SettingsTab, {icon: React.ReactNode}> = {
    upstream: {icon: <Server className="h-3.5 w-3.5" />},
    proxy: {icon: <Route className="h-3.5 w-3.5" />},
    models: {icon: <Shuffle className="h-3.5 w-3.5" />},
    users: {icon: <Users className="h-3.5 w-3.5" />},
    tokens: {icon: <KeyRound className="h-3.5 w-3.5" />},
    system: {icon: <DownloadCloud className="h-3.5 w-3.5" />},
    backup: {icon: <Database className="h-3.5 w-3.5" />},
    changelog: {icon: <FileText className="h-3.5 w-3.5" />},
    about: {icon: <Info className="h-3.5 w-3.5" />},
  };
  const tabItems: SectionTabItem[] = SETTINGS_TABS.map((name) => ({
    href: settingsTabHref(name),
    label: t(SETTINGS_TAB_LABEL_KEYS[name]),
    icon: TAB_META[name].icon,
  }));

  /**
   * 三份数据一次并发取。
   *
   * 主数据是 `cfg`（上游配置）——页面主体（上游配置 Tab 的整张表单）就是它的副本，
   * 所以**它没到就不能渲染表单**：`form` 的初值是 `defaultValues(...)`，那等于把
   * 硬编码默认值当成「你现在的配置」显示出来。原先的写法是 `allSettled` 之后
   * `if (c.status === 'fulfilled')`，**失败分支什么都不做** —— 于是取数挂掉时页面
   * 照常渲染那张默认值表单，而写入路径又都被 `upstreamReady` 禁用，用户看到的是
   * 一张「能看不能改」的假配置（见下面首屏守卫）。
   *
   * `modelMap` / `users` 是配件（模型映射 Tab / 用户 Tab），与主数据同 hook 但
   * 各自独立成败：其中一个失败不该把另一个也判成失败。
   */
  const {values, errors, isRefreshing, reload} = useAsyncAll({
    cfg: () => settingsApi.upstream(),
    modelMap: () => settingsApi.modelMap(),
    users: () => settingsApi.users(),
  }, []);

  /** 配置：null = 还没取到（首屏加载中，或它自己失败了）。**不要**用假默认值兜底 */
  const cfg: UpstreamConfig | null = values.cfg ?? null;
  const users: UserItem[] = values.users ?? [];
  /**
   * 用户列表**从未**取到（首屏那次就失败了，所以 values 里始终没有它）。
   *
   * 用来把「没有用户」与「不知道有没有用户」分开——两者都会渲染成空表，含义却
   * 完全相反：「暂无管理用户」在这页等于告诉管理员「系统里一个账号都没有」，
   * 而实际可能只是这次没取到。宁可说「未取到」。
   */
  const usersFailed = 'users' in errors;
  /** 模型映射的乐观值：保存成功后、刷新落地前先显示新值（见 saveModelMap） */
  const [optimisticMap, setOptimisticMap] = useState<Record<string, string> | null>(null);
  const modelMap: Record<string, string> = optimisticMap ?? values.modelMap ?? {};

  const [models, setModels] = useState<ModelInfo[]>([]);
  /** 模型列表来源：dynamic = 上游实时拉取，static = 上游内置回退表 */
  const [modelSource, setModelSource] = useState<ModelSource>('unknown');
  const [modelsLoading, setModelsLoading] = useState(false);

  /**
   * 「到期积分按天模糊统计」——**界面偏好**，存在浏览器本地而不是上游配置里。
   *
   * 它决定仪表盘那个数字怎么算，不改变系统的任何行为，所以不进 GROUPS：
   * 那套分组每条都对应 config.json 的一个段，混进来会被当成上游配置去保存
   * （保存时还会因为段名对不上而被拒）。与语言、版本切换同类。
   *
   * 首屏后读 localStorage：直接读会让服务端渲染与客户端不一致。
   */
  const [expiryDaily, setExpiryDaily] = useState(false);
  useEffect(() => {
    setExpiryDaily(getExpiryDailyGroup());
  }, []);

  /** 可视化表单状态 */
  const [form, setForm] = useState<Record<Group, Record<string, FieldValue>>>({
    schedule: defaultValues(SCHEDULE_FIELDS),
    prompt: defaultValues(PROMPT_FIELDS),
    cooldown: defaultValues(COOLDOWN_FIELDS),
    pool: defaultValues(POOL_FIELDS),
    features: defaultValues(FEATURES_FIELDS),
    session: defaultValues(SESSION_FIELDS),
    server: defaultValues(SERVER_FIELDS),
    upstream: defaultValues(UPSTREAM_FIELDS),
    global: defaultValues(GLOBAL_FIELDS),
    admin: defaultValues(ADMIN_FIELDS),
  });
  /** 加载时的原始值，用于只提交改动过的项 */
  const original = useRef<Record<Group, Record<string, FieldValue>>>({
    schedule: defaultValues(SCHEDULE_FIELDS),
    prompt: defaultValues(PROMPT_FIELDS),
    cooldown: defaultValues(COOLDOWN_FIELDS),
    pool: defaultValues(POOL_FIELDS),
    features: defaultValues(FEATURES_FIELDS),
    session: defaultValues(SESSION_FIELDS),
    server: defaultValues(SERVER_FIELDS),
    upstream: defaultValues(UPSTREAM_FIELDS),
    global: defaultValues(GLOBAL_FIELDS),
    admin: defaultValues(ADMIN_FIELDS),
  });
  /** 高级模式（直接编辑 JSON） */
  const [advanced, setAdvanced] = useState(false);
  const [schedText, setSchedText] = useState('');
  const [coolText, setCoolText] = useState('');
  const [poolText, setPoolText] = useState('');
  const [featText, setFeatText] = useState('');
  const [sessText, setSessText] = useState('');
  const [promptText, setPromptText] = useState('');
  const [serverText, setServerText] = useState('');
  const [upText, setUpText] = useState('');
  const [globalText, setGlobalText] = useState('');

  const [mapAlias, setMapAlias] = useState('');
  const [mapTarget, setMapTarget] = useState('');
  const [newUser, setNewUser] = useState({username: '', password: '', role: 'viewer'});
  const [busy, setBusy] = useState(false);
  /** Upstash（Redis 持久化）表单 */
  const [upstashForm, setUpstashForm] = useState({url: '', token: ''});
  const [upstashBusy, setUpstashBusy] = useState(false);

  /**
   * 配置到达（首屏取到 / 手动刷新 / 保存后重取）时，把表单重置为服务端的值。
   *
   * 依赖 `cfg` 的**引用**而不是 `values`：刷新失败时 useAsyncAll 把上一次的值合并
   * 回来，`cfg` 仍是同一个对象，于是不会把用户正在编辑的内容冲掉。
   *
   * `available === false` 表示「配置读到了、但上游连不上」——那份配置本身仍是可读的，
   * 只是所有写入路径都被 `upstreamReady` 禁掉；此时不重置表单（与改造前一致）。
   */
  useEffect(() => {
    if (!cfg || cfg.available === false) return;
    const picked = {
      schedule: pickValues(SCHEDULE_FIELDS, cfg.schedule),
      prompt: pickValues(PROMPT_FIELDS, cfg.prompt),
      cooldown: pickValues(COOLDOWN_FIELDS, cfg.cooldown),
      pool: pickValues(POOL_FIELDS, cfg.pool),
      // reasoning_as_content 在上游是**顶层**键（不在 features 段里），
      // 但界面上跟其它功能开关放一组更自然。这里把顶层值并进视图，
      // 保存时再由 saveGroup 提到顶层（见 TOP_LEVEL_KEYS）。
      features: pickValues(FEATURES_FIELDS, {
        ...cfg.features,
        reasoning_as_content: cfg.reasoning_as_content,
      }),
      session: pickValues(SESSION_FIELDS, cfg.session_sticky),
      server: pickValues(SERVER_FIELDS, cfg.server),
      upstream: pickValues(UPSTREAM_FIELDS, cfg.upstream),
      global: pickValues(GLOBAL_FIELDS, cfg.global),
      admin: pickValues(ADMIN_FIELDS, cfg.admin),
    };
    setForm(picked);
    original.current = {
      schedule: {...picked.schedule},
      prompt: {...picked.prompt},
      cooldown: {...picked.cooldown},
      pool: {...picked.pool},
      features: {...picked.features},
      session: {...picked.session},
      server: {...picked.server},
      upstream: {...picked.upstream},
      global: {...picked.global},
      admin: {...picked.admin},
    };
    setSchedText(JSON.stringify(cfg.schedule ?? {}, null, 2));
    setCoolText(JSON.stringify(cfg.cooldown ?? {}, null, 2));
    setPoolText(JSON.stringify(cfg.pool ?? {}, null, 2));
    setFeatText(JSON.stringify(cfg.features ?? {}, null, 2));
    setSessText(JSON.stringify(cfg.session_sticky ?? {}, null, 2));
    setPromptText(JSON.stringify(cfg.prompt ?? {}, null, 2));
    setServerText(JSON.stringify(cfg.server ?? {}, null, 2));
    setUpText(JSON.stringify(cfg.upstream ?? {}, null, 2));
    setGlobalText(JSON.stringify(cfg.global ?? {}, null, 2));
    // url 可回显；token 不回显明文，留空表示不修改
    setUpstashForm({url: cfg.upstash?.url || '', token: ''});
  }, [cfg]);

  /**
   * 上游模型列表。
   *
   * 上游自己会缓存 1 小时（动态拉取成功时），失败则进入 5 分钟负缓存——
   * 所以「刷新页面」不一定能拿到新列表。这也是为什么提供手动重新拉取：
   * 上游刷新令牌/新增模型后，用户需要能立刻主动取一次，而不是干等缓存过期。
   *
   * 注：上游 2026-09-15（commit 1b7ce4a）起删除了静态兜底表，改为纯动态——
   * 取不到就是空列表。下面的 static 分支只为仍在跑旧版上游的部署保留。
   */
  const loadModels = useCallback(async (silent = true) => {
    if (!silent) setModelsLoading(true);
    try {
      const res = await upstreamApi.models();
      setModels(res.models || []);
      setModelSource(res.source || 'unknown');
    } catch {
      setModels([]);
      setModelSource('unknown');
    } finally {
      if (!silent) setModelsLoading(false);
    }
  }, []);

  // 配置 / 映射 / 用户由 useAsyncAll 在挂载时自己取（首屏一次），这里只管模型列表。
  useEffect(() => {
    loadModels();
  }, [loadModels]);

  const upstreamReady = !!cfg && cfg.available !== false;
  const upstreamError = cfg && cfg.available === false ? cfg.error : undefined;
  /** 配置文件中是否已保存 Upstash 地址 */
  const upstashConfigured = !!cfg?.upstash?.url;

  /** 保存 Upstash 配置（token 留空表示保持原值） */
  async function saveUpstash() {
    if (!upstashForm.url.trim()) {
      notify.err(t('settings.upstashUrlRequired'));
      return;
    }
    setUpstashBusy(true);
    try {
      await settingsApi.saveUpstream({
        upstash: {
          url: upstashForm.url.trim(),
          ...(upstashForm.token.trim() ? {token: upstashForm.token.trim()} : {}),
        },
      });
      notify.ok(t('settings.upstashSaved'), t('settings.applying'));
      // （Upstash 属于上游外部依赖，改完无需重启上游容器）
      setUpstashForm((f) => ({...f, token: ''}));
      await reload();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setUpstashBusy(false);
    }
  }

  function setField(group: Group, key: string, value: FieldValue) {
    setForm((prev) => ({...prev, [group]: {...prev[group], [key]: value}}));
  }

  /** 是否有未保存的改动 */
  function isDirty(group: Group): boolean {
    const cur = form[group];
    const org = original.current[group];
    return Object.keys(cur).some((k) => cur[k] !== org[k]);
  }

  /** 只提交改动过的字段，避免覆盖其他未展示的配置项 */
  async function saveGroup(group: Group) {
    const cur = form[group];
    if (group === 'schedule') {
      const error = scheduleDelayError(cur);
      if (error) {
        notify.err(tp(error));
        return;
      }
    }
    const org = original.current[group];
    const patch: Record<string, boolean | number | number[] | string> = {};
    // 顶层键单独收：它们不属于任何段，必须放在请求体根上
    const topPatch: Record<string, boolean | number | number[] | string> = {};
    for (const k of Object.keys(cur)) {
      if (cur[k] === org[k]) continue;
      const f = FIELD_BY_KEY[k];
      if (!f) continue;
      const w = toWire(f, cur[k]);
      if (!w.ok) {
        notify.err(t('settings.fieldInvalid', {label: tp(f.label)}), w.error);
        return;
      }
      (TOP_LEVEL_KEYS.has(k) ? topPatch : patch)[k] = w.value;
    }
    if (!Object.keys(patch).length && !Object.keys(topPatch).length) {
      notify.info(t('settings.noChanges'));
      return;
    }
    setBusy(true);
    try {
      const def = GROUPS.find((g) => g.id === group);
      const body: Record<string, unknown> = {...topPatch};
      // 段内有改动时才带该段：只改顶层键时不要顺手回写整段
      if (Object.keys(patch).length) body[def?.section ?? group] = patch;
      const saved = await settingsApi.saveUpstream(body);
      // 容器部署下无法自动重载上游（需 docker 命令，而容器刻意不挂 docker 套接字），
      // 后端会带回 reload_hint —— 如实转达，不让人以为改完就生效了
      if (saved?.reload_hint) {
        notify.warn(t('settings.savedManualStep'), saved.reload_hint);
      } else {
        notify.ok(t('settings.saved'), t('settings.applying'));
      }
      await reload();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  function resetGroup(group: Group) {
    setForm((prev) => ({...prev, [group]: {...original.current[group]}}));
  }

  /** 高级模式：直接保存 JSON（字段名即上游 config.json 的段名） */
  async function saveJson(field: WireSection, text: string) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      notify.err(t('settings.jsonInvalid'));
      return;
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      notify.err(t('settings.jsonNotObject'));
      return;
    }
    setBusy(true);
    try {
      await settingsApi.saveUpstream({[field]: parsed});
      notify.ok(t('settings.saved'), t('settings.applying'));
      await reload();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  /**
   * 保存模型映射。
   *
   * 乐观更新：先切到新值让表格立刻响应，再 `await reload()` 把服务端确认过的那份
   * 拉回来。**刷新成功才撤乐观值**——若这一次刷新失败，撤早了表格会跳回旧值，
   * 用户会以为没保存上（其实写操作是成功的）。与 `security` 页的 saveConfig 同一套。
   */
  async function saveModelMap(next: Record<string, string>) {
    setOptimisticMap(next);
    try {
      await settingsApi.saveModelMap(next);
      notify.ok(t('settings.modelMapSaved'));
      if (await reload()) setOptimisticMap(null);
    } catch (e) {
      setOptimisticMap(null);
      notify.err(errText(e));
    }
  }

  const header = (
    <>
      <PageHeader
        title={t('settings.title')}
        description={t('settings.description')}
        actions={
          <Button
            variant="outline"
            size="sm"
            className="rounded-full"
            title={t('settings.reloadTitle')}
            onClick={() => {
              reload();
              loadModels(false);
            }}
          >
            <RefreshCw />
            {t('settings.reload')}
          </Button>
        }
      />
      {/* 上游重载状态（批次 5）。放进 `header` 而不是两个 return 分支里各写一遍：
          下面「取不到配置」那条分支是**早返回**，漏一处就会出现「配置读不到时
          看不到重载失败」——而配置读不到往往正是重载失败的原因，那才是最需要
          看到它的时刻。导航/状态的有无应当取决于**路由**，不是**数据**。 */}
      <UpstreamReloadNotice />
    </>
  );

  /**
   * 首屏：配置还没取到（加载中，或它自己失败了）。
   *
   * **不能**渲染下面的表单——`form` 的初值是 `defaultValues(...)`，那等于把硬编码
   * 默认值当成「你现在的配置」显示出来。而写入路径又都被 `upstreamReady` 禁掉，
   * 用户看到的是一张「能看不能改」的假配置，会据此以为「我的配置就是这样」，
   * 或者以为「面板坏了」。取不到就说取不到，并给「重试」。
   */
  if (!cfg) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {'cfg' in errors ? <LoadError variant="page" onRetry={reload} /> : <SettingsSkeleton />}
        {/* children 在这个分支里也要渲染：`/settings` 的重定向挂在子路由的 page 上，
            少了它，「配置取不到」时地址就停在 `/settings` 不再规范化——于是用户
            刷新一次会再走一遍重定向，而「我现在在哪个 Tab」这件事变成**取决于
            数据取没取到**。重定向该是路由的性质，不是数据的性质。 */}
        {children}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* value 受路径控制；这里**不接** onValueChange —— 切换 Tab 是一次真正的
          导航（点的是 <Link>），不再由 Tabs 的内部状态说了算。 */}
      <Tabs value={tab}>
        {/* 标签较多，手机上会撑破容器，这里允许横向滚动 */}
        <div className="-mx-1 overflow-x-auto px-1 pb-1">
          <SectionTabs items={tabItems} active={settingsTabHref(tab)} />
        </div>

        {/* ═══ 上游配置 ═══ */}
        <TabsContent value="upstream" className="mt-3 space-y-3">
          {/* 多上游（账号池分组）：密钥绑定上游 = 请求只走那个池，见 server/upstreamsvc.py。
              放在 config.json 字段之前：它是「本端接了几个上游」的清单，
              而下面那批字段描述的是**默认上游自身**的配置。 */}
          <UpstreamEndpoints />
          {upstreamError && (
            <div className="flex items-start gap-2.5 rounded-[20px] border border-amber-500/30 bg-amber-500/10 p-4">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
              <div className="space-y-1">
                <div className="text-xs font-medium">{t('settings.cfgUnreadable')}</div>
                <div className="text-[11px] text-muted-foreground">{upstreamError}</div>
                <div className="text-[11px] text-muted-foreground">
                  <RichText text={t('settings.cfgUnreadableHint')} />
                </div>
              </div>
            </div>
          )}

          {/* 账号池概况 */}
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            <div className="rounded-[20px] bg-muted px-3.5 py-3">
              <div className="mb-2 text-sm font-medium">{t('settings.serviceInfo')}</div>
              <div className="space-y-1.5 text-xs">
                {([
                  // listen 可能是 ":7863"，也可能是 "127.0.0.1:9000" / "0.0.0.0:8080"：
                  // 只有以冒号开头时才需要补主机名，否则会出现
                  // "127.0.0.1127.0.0.1:9000" 这种拼接。
                  ['listen', t('settings.infoListen'),
                   cfg?.listen
                     ? (cfg.listen.startsWith(':') ? `127.0.0.1${cfg.listen}` : cfg.listen)
                     : '—'],
                  ['apiKey', t('settings.infoApiKey'), cfg?.api_key_masked ? t('settings.infoApiKeySet') : '—'],
                  ['authDir', t('settings.infoAuthDir'), cfg?.auth_dir || '—'],
                ] as [string, string, string][]).map(([id, k, v]) => (
                  <div key={id} className="flex items-center justify-between gap-3">
                    <span className="shrink-0 text-muted-foreground">{k}</span>
                    <span className="min-w-0 flex-1 truncate text-right font-mono" title={v}>{v}</span>
                    {id === 'authDir' && v !== '—' && (
                      <CopyButton value={v} title={t('settings.copyAuthDir')} className="h-6 w-6" />
                    )}
                  </div>
                ))}
                {cfg?.upstream_auth_dir && (
                  <div className="flex items-start justify-between gap-3">
                    <span className="shrink-0 text-muted-foreground">{t('settings.upstreamDeclaredDir')}</span>
                    <span
                      className="min-w-0 flex-1 truncate text-right font-mono text-amber-600 dark:text-amber-400"
                      title={cfg.upstream_auth_dir}
                    >
                      {cfg.upstream_auth_dir}
                    </span>
                  </div>
                )}
              </div>
              {cfg?.upstream_auth_dir && (
                <p className="mt-2 text-[11px] leading-4 text-amber-600 dark:text-amber-400">
                  {t('settings.authDirMismatch')}
                </p>
              )}
            </div>

            <div className="rounded-[20px] bg-muted px-3.5 py-3 lg:col-span-2">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <div className="text-sm font-medium">{t('settings.modelsTitle')}</div>
                  {modelSource === 'dynamic' && (
                    <span className="shrink-0 text-[11px] text-muted-foreground">
                      {t('settings.modelsDynamic', {count: models.length, n: models.length})}
                    </span>
                  )}
                  {modelSource === 'static' && (
                    <span
                      className="shrink-0 text-[11px] text-amber-600 dark:text-amber-400"
                      title={t('settings.modelsStaticTitle')}
                    >
                      {t('settings.modelsStatic', {count: models.length, n: models.length})}
                    </span>
                  )}
                  {modelSource === 'unknown' && models.length > 0 && (
                    <span className="shrink-0 text-[11px] text-muted-foreground">
                      {t('settings.modelsCount', {count: models.length, n: models.length})}
                    </span>
                  )}
                </div>
                {/*
                  只在请求进行中禁用，不绑定「配置文件是否可读」——这两件事无关：
                  模型列表是从上游 /v1/models 实时取的，config.json 读不到不代表
                  上游不可达。把按钮一起禁用会让用户在最需要重试时点不动。
                */}
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 shrink-0 gap-1.5 text-[11px]"
                  disabled={modelsLoading}
                  title={t('settings.refetchModelsTitle')}
                  onClick={() => loadModels(false)}
                >
                  {modelsLoading ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <RefreshCw className="h-3.5 w-3.5" />
                  )}
                  {t('models.refetch')}
                </Button>
              </div>
              <div className="flex flex-wrap gap-1">
                {models.length ? (
                  models.map((m) => (
                    <Badge key={m.id} variant="secondary" className="rounded-full font-mono text-[10px]">
                      {m.id}
                    </Badge>
                  ))
                ) : (
                  <span className="text-xs text-muted-foreground">
                    {upstreamReady ? t('settings.modelsNotFetched') : t('settings.cfgNotReady')}
                  </span>
                )}
              </div>

              {/* 关键说明：模型列表由上游**随机挑一个健康账号**去拉，取决于该账号的
                  授权，所以「同样的部署、不同账号看到的模型数量不同」是正常的——
                  并非本站少显示。上游自身还会缓存 1 小时。 */}
              {models.length > 0 && (
                <p className="mt-2 text-[11px] leading-4 text-muted-foreground">
                  {modelSource === 'static' ? (
                    <RichText text={t('settings.modelsStaticNote')} />
                  ) : (
                    <RichText text={t('settings.modelsNote')} />
                  )}
                </p>
              )}
            </div>
          </div>

          {/* 界面偏好：下面那些卡片都是**上游配置**（每条对应 config.json 的一个段、
              有保存按钮）；这一张只决定界面怎么摆，存在浏览器本地、改完立即生效，
              所以既没有保存按钮也不受「上游不可用」影响。 */}
          <div className="rounded-[20px] bg-muted px-3.5 py-3">
            <div className="mb-2.5">
              <div className="text-sm font-medium">{t('settings.displayTitle')}</div>
              <RichText className="text-[11px] text-muted-foreground" text={t('settings.displayDesc')} />
            </div>
            <div className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2">
              <div className="min-w-0">
                <div className="text-xs font-medium">{t('settings.expiryDailyLabel')}</div>
                <RichText
                  className="mt-0.5 block text-[11px] leading-4 text-muted-foreground"
                  text={t('settings.expiryDailyDesc')}
                />
              </div>
              <Switch
                aria-label={t('settings.expiryDailyLabel')}
                checked={expiryDaily}
                onCheckedChange={(v) => {
                  setExpiryDaily(v);
                  setExpiryDailyGroup(v);
                }}
              />
            </div>
          </div>

          {/* 可视化设置卡片 */}
          {GROUPS.map((g) => {
            const dirty = isDirty(g.id);
            return (
              <div key={g.id} className="rounded-[20px] bg-muted px-3.5 py-3">
                <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="text-sm font-medium">{tp(g.title)}</div>
                    {/* 分组说明里带加粗强调（如「只对国内版账号生效」），走 RichText 渲染 */}
                    <RichText className="text-[11px] text-muted-foreground" text={tp(g.desc)} />
                  </div>
                  <div className="flex items-center gap-2">
                    {dirty && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="rounded-full text-muted-foreground"
                        disabled={busy}
                        onClick={() => resetGroup(g.id)}
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                        {t('settings.revert')}
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant={dirty ? 'default' : 'outline'}
                      className="rounded-full"
                      disabled={!isAdmin || busy || !upstreamReady}
                      onClick={() => saveGroup(g.id)}
                    >
                      <Save className="h-3.5 w-3.5" />
                      {t('common.save')}
                    </Button>
                  </div>
                </div>

                {/* 宽屏两列：开关与它对应的时刻/数值字段天然成对，行数减半 */}
                <div className="grid grid-cols-1 gap-1.5 xl:grid-cols-2">
                  {g.fields.map((f) => {
                    const err = fieldError(f, form[g.id][f.key]);
                    const caution = 'caution' in f ? f.caution : undefined;
                    return (
                      <div
                        key={f.key}
                        className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2"
                      >
                        <div className="min-w-0">
                          <div className="text-xs font-medium">{tp(f.label)}</div>
                          <RichText
                            className="mt-0.5 block text-[11px] leading-4 text-muted-foreground"
                            text={tp(f.desc)}
                          />
                          {f.kind !== 'bool' && err && (
                            <div className="mt-0.5 text-[10px] leading-3 text-destructive">{err}</div>
                          )}
                        </div>

                        {f.kind === 'bool' ? (
                          <Switch
                            aria-label={tp(f.label)}
                            checked={!!form[g.id][f.key]}
                            disabled={!isAdmin || !upstreamReady}
                            onCheckedChange={(v) => setField(g.id, f.key, v)}
                          />
                        ) : f.kind === 'num' ? (
                          <div className="flex shrink-0 items-center gap-1.5">
                            <Input
                              type="number"
                              min={f.min}
                              max={f.max}
                              step={f.step ?? 1}
                              value={String(form[g.id][f.key] ?? f.def)}
                              disabled={!isAdmin || !upstreamReady}
                              onChange={(e) => {
                                const n = Number(e.target.value);
                                setField(g.id, f.key, Number.isFinite(n) ? n : f.def);
                              }}
                              className={
                                'h-8 w-20 bg-background text-right tabular-nums' +
                                (err ? ' border-destructive' : '')
                              }
                            />
                            {f.unit && (
                              <span className="w-8 text-[11px] text-muted-foreground">{tp(f.unit)}</span>
                            )}
                          </div>
                        ) : f.kind === 'select' ? (
                          <Select
                            value={String(form[g.id][f.key] ?? f.def)}
                            disabled={!isAdmin || !upstreamReady}
                            onValueChange={(v) => setField(g.id, f.key, v)}
                          >
                            <SelectTrigger className="h-8 w-[168px] shrink-0 bg-background text-xs" aria-label={tp(f.label)}>
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {f.options.map((o) => (
                                <SelectItem key={o.value} value={o.value} className="text-xs">
                                  {tp(o.label)}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        ) : f.kind === 'text' ? (
                          <Input
                            value={String(form[g.id][f.key] ?? '')}
                            disabled={!isAdmin || !upstreamReady}
                            placeholder={f.placeholder ? tp(f.placeholder) : undefined}
                            onChange={(e) => setField(g.id, f.key, e.target.value)}
                            className={
                              'h-8 shrink-0 bg-background text-xs ' +
                              (caution ? 'w-56' : 'w-40') +
                              (err ? ' border-destructive' : '')
                            }
                          />
                        ) : (
                          <Input
                            value={String(form[g.id][f.key] ?? '')}
                            disabled={!isAdmin || !upstreamReady}
                            placeholder={f.kind === 'hours' ? '9, 21' : '600s'}
                            onChange={(e) => setField(g.id, f.key, e.target.value)}
                            className={
                              'h-8 shrink-0 bg-background text-right tabular-nums ' +
                              (f.kind === 'hours' ? 'w-32' : 'w-24') +
                              (err ? ' border-destructive' : '')
                            }
                          />
                        )}
                      </div>
                    );
                  })}
                </div>

                {/* 危险项警示：填错会导致上游启动失败，单独占一行说明 */}
                {g.fields.some((f) => 'caution' in f && f.caution) && (
                  <div className="mt-1.5 space-y-1">
                    {g.fields.map((f) =>
                      'caution' in f && f.caution ? (
                        <div
                          key={f.key}
                          className="flex items-start gap-1.5 rounded-xl bg-amber-500/10 px-3 py-2 text-[11px] leading-4 text-amber-600 dark:text-amber-400"
                        >
                          <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" />
                          <RichText text={tp(f.caution)} />
                        </div>
                      ) : null,
                    )}
                  </div>
                )}

                {!isAdmin && (
                  <p className="mt-2 text-[11px] text-muted-foreground">{t('settings.readonlyNote')}</p>
                )}
              </div>
            );
          })}

          {/* Redis / Upstash 持久化 */}
          <div className="rounded-[20px] bg-muted p-4">
            <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="flex items-center gap-2 text-sm font-medium">
                  {t('settings.upstashTitle')}
                  {upstashConfigured ? (
                    <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
                      {t('settings.upstashConfigured')}
                    </Badge>
                  ) : (
                    <Badge variant="secondary" className="rounded-full text-muted-foreground">
                      {t('settings.upstashNotConfigured')}
                    </Badge>
                  )}
                </div>
                <div className="mt-0.5 text-[11px] leading-4 text-muted-foreground">
                  {upstashConfigured
                    ? t('settings.upstashDescOn')
                    : t('settings.upstashDescOff')}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  className="rounded-full"
                  disabled={!isAdmin || upstashBusy}
                  onClick={async () => {
                    setUpstashBusy(true);
                    try {
                      const r = await settingsApi.testUpstash(upstashForm.url, upstashForm.token || undefined);
                      (r.ok ? notify.ok : notify.err)(r.message, r.ok ? t('settings.upstashOk') : undefined);
                    } catch (e) {
                      notify.err(errText(e));
                    } finally {
                      setUpstashBusy(false);
                    }
                  }}
                >
                  {upstashBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <PlugZap className="h-3.5 w-3.5" />}
                  {t('settings.testConnection')}
                </Button>
                <Button
                  size="sm"
                  className="rounded-full"
                  disabled={!isAdmin || upstashBusy || !upstreamReady}
                  onClick={saveUpstash}
                >
                  <Save className="h-3.5 w-3.5" />
                  {t('common.save')}
                </Button>
              </div>
            </div>

            <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('settings.upstashUrl')}</Label>
                <Input
                  value={upstashForm.url}
                  disabled={!isAdmin || !upstreamReady}
                  onChange={(e) => setUpstashForm({...upstashForm, url: e.target.value})}
                  placeholder="https://xxx-12345.upstash.io"
                  className="bg-background font-mono text-xs"
                />
                <div className="text-[11px] text-muted-foreground">
                  {t('settings.upstashUrlHint')}
                </div>
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">Upstash Token</Label>
                <Input
                  type="password"
                  value={upstashForm.token}
                  disabled={!isAdmin || !upstreamReady}
                  onChange={(e) => setUpstashForm({...upstashForm, token: e.target.value})}
                  placeholder={
                    cfg?.upstash?.has_token
                      ? t('settings.upstashTokenSaved', {masked: cfg.upstash.token_masked})
                      : t('settings.upstashTokenPlaceholder')
                  }
                  className="bg-background font-mono text-xs"
                />
                <div className="text-[11px] text-muted-foreground">
                  {cfg?.upstash?.has_token
                    ? t('settings.upstashTokenHintSaved')
                    : t('settings.upstashTokenHint')}
                </div>
              </div>
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-2">
              {upstashConfigured && (
                <ConfirmDialog
                  title={t('settings.upstashClearTitle')}
                  description={t('settings.upstashClearDesc')}
                  confirmText={t('settings.clearConfig')}
                  destructive
                  onConfirm={async () => {
                    setUpstashBusy(true);
                    try {
                      await settingsApi.saveUpstream({upstash: {clear: true}});
                      setUpstashForm({url: '', token: ''});
                      notify.ok(t('settings.upstashCleared'), t('settings.applyingShort'));
                      await reload();
                    } catch (e) {
                      notify.err(errText(e));
                    } finally {
                      setUpstashBusy(false);
                    }
                  }}
                  trigger={
                    <Button size="sm" variant="ghost" className="rounded-full text-red-500" disabled={!isAdmin || upstashBusy}>
                      <Trash2 className="h-3.5 w-3.5" />
                      {t('settings.clearConfig')}
                    </Button>
                  }
                />
              )}
            </div>
            <div className="mt-2 text-[11px] leading-4 text-muted-foreground">
              {t('settings.upstashFooter')}
            </div>
          </div>

          {/* 高级模式：直接编辑 JSON */}
          <div className="rounded-[20px] bg-muted p-4">
            <button
              type="button"
              onClick={() => setAdvanced((v) => !v)}
              className="flex w-full items-center justify-between gap-2 text-left"
            >
              <div>
                <div className="text-sm font-medium">{t('settings.advanced')}</div>
                <div className="text-[11px] text-muted-foreground">
                  {t('settings.advancedDesc')}
                </div>
              </div>
              <ChevronDown
                className={'h-4 w-4 shrink-0 text-muted-foreground transition-transform ' + (advanced ? 'rotate-180' : '')}
              />
            </button>

            {advanced && (
              <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="font-mono text-[11px] text-muted-foreground">schedule</div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 rounded-full text-[11px]"
                      disabled={!isAdmin || busy || !upstreamReady}
                      onClick={() => saveJson('schedule', schedText)}
                    >
                      {t('common.save')}
                    </Button>
                  </div>
                  <Textarea
                    rows={8}
                    spellCheck={false}
                    disabled={!isAdmin || !upstreamReady}
                    value={upstreamReady ? schedText : ''}
                    placeholder={upstreamReady ? undefined : t('settings.cfgNotLoaded')}
                    onChange={(e) => setSchedText(e.target.value)}
                    className="bg-background font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="font-mono text-[11px] text-muted-foreground">cooldown</div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 rounded-full text-[11px]"
                      disabled={!isAdmin || busy || !upstreamReady}
                      onClick={() => saveJson('cooldown', coolText)}
                    >
                      {t('common.save')}
                    </Button>
                  </div>
                  <Textarea
                    rows={8}
                    spellCheck={false}
                    disabled={!isAdmin || !upstreamReady}
                    value={upstreamReady ? coolText : ''}
                    placeholder={upstreamReady ? undefined : t('settings.cfgNotLoaded')}
                    onChange={(e) => setCoolText(e.target.value)}
                    className="bg-background font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="font-mono text-[11px] text-muted-foreground">features</div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 rounded-full text-[11px]"
                      disabled={!isAdmin || busy || !upstreamReady}
                      onClick={() => saveJson('features', featText)}
                    >
                      {t('common.save')}
                    </Button>
                  </div>
                  <Textarea
                    rows={8}
                    spellCheck={false}
                    disabled={!isAdmin || !upstreamReady}
                    value={upstreamReady ? featText : ''}
                    placeholder={upstreamReady ? undefined : t('settings.cfgNotLoaded')}
                    onChange={(e) => setFeatText(e.target.value)}
                    className="bg-background font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="font-mono text-[11px] text-muted-foreground">session_sticky</div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 rounded-full text-[11px]"
                      disabled={!isAdmin || busy || !upstreamReady}
                      onClick={() => saveJson('session_sticky', sessText)}
                    >
                      {t('common.save')}
                    </Button>
                  </div>
                  <Textarea
                    rows={8}
                    spellCheck={false}
                    disabled={!isAdmin || !upstreamReady}
                    value={upstreamReady ? sessText : ''}
                    placeholder={upstreamReady ? undefined : t('settings.cfgNotLoaded')}
                    onChange={(e) => setSessText(e.target.value)}
                    className="bg-background font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="font-mono text-[11px] text-muted-foreground">pool</div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 rounded-full text-[11px]"
                      disabled={!isAdmin || busy || !upstreamReady}
                      onClick={() => saveJson('pool', poolText)}
                    >
                      {t('common.save')}
                    </Button>
                  </div>
                  <Textarea
                    rows={8}
                    spellCheck={false}
                    disabled={!isAdmin || !upstreamReady}
                    value={upstreamReady ? poolText : ''}
                    placeholder={upstreamReady ? undefined : t('settings.cfgNotLoaded')}
                    onChange={(e) => setPoolText(e.target.value)}
                    className="bg-background font-mono text-xs"
                  />
                </div>
                {([
                  ['prompt', promptText, setPromptText],
                  ['server', serverText, setServerText],
                  ['upstream', upText, setUpText],
                  ['global', globalText, setGlobalText],
                ] as const).map(([name, val, setter]) => (
                  <div key={name} className="space-y-2">
                    <div className="flex items-center justify-between">
                      <div className="font-mono text-[11px] text-muted-foreground">{name}</div>
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 rounded-full text-[11px]"
                        disabled={!isAdmin || busy || !upstreamReady}
                        onClick={() => saveJson(name, val)}
                      >
                        {t('common.save')}
                      </Button>
                    </div>
                    <Textarea
                      rows={8}
                      spellCheck={false}
                      disabled={!isAdmin || !upstreamReady}
                      value={upstreamReady ? val : ''}
                      placeholder={upstreamReady ? undefined : t('settings.cfgNotLoaded')}
                      onChange={(e) => setter(e.target.value)}
                      className="bg-background font-mono text-xs"
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        </TabsContent>

        {/* ═══ 模型映射 ═══ */}
        <TabsContent value="models" className="mt-4 space-y-4">
          <div className="rounded-[20px] bg-muted p-4">
            <div className="mb-1 text-sm font-medium">{t('settings.mapNewAlias')}</div>
            <div className="mb-3 text-[11px] text-muted-foreground">
              <RichText text={t('settings.mapNewAliasDesc')} />
            </div>
            <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-4">
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('settings.mapAlias')}</Label>
                <Input value={mapAlias} onChange={(e) => setMapAlias(e.target.value)} placeholder="gpt-4o-mini" className="bg-background" disabled={!isAdmin} />
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('settings.mapTarget')}</Label>
                <Select value={mapTarget} onValueChange={setMapTarget} disabled={!isAdmin}>
                  <SelectTrigger className="bg-background" aria-label={t('settings.mapTarget')}><SelectValue placeholder={t('chat.selectModel')} /></SelectTrigger>
                  <SelectContent>
                    {models.map((m) => (
                      <SelectItem key={m.id} value={m.id}>{m.id}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="sm:col-span-2">
                <Button
                  className="rounded-full"
                  disabled={!isAdmin || !mapAlias.trim() || !mapTarget}
                  onClick={() => {
                    saveModelMap({...modelMap, [mapAlias.trim()]: mapTarget});
                    setMapAlias('');
                    setMapTarget('');
                  }}
                >
                  <Plus />
                  {t('settings.mapAdd')}
                </Button>
              </div>
            </div>
          </div>

          <div className="overflow-hidden rounded-[20px] bg-muted">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('settings.mapAlias')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('settings.mapTarget')}</TableHead>
                  {isAdmin && <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">{t('accounts.colActions')}</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {Object.entries(modelMap).map(([alias, target]) => (
                  <TableRow key={alias} className="border-b border-border/40">
                    <TableCell className="pl-4 font-mono text-xs">
                      {/* 兜底那条（键为 `*`）直接显示成「兜底模型」，别让用户去记星号 */}
                      {alias === '*' ? (
                        <span className="flex items-center gap-1.5">
                          <span className="font-sans text-[11px] font-medium">
                            {t('settings.mapFallbackRow')}
                          </span>
                          <code className="text-muted-foreground">*</code>
                        </span>
                      ) : (
                        alias
                      )}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">{target}</TableCell>
                    {isAdmin && (
                      <TableCell className="pr-4 text-right">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 rounded-md text-red-500 hover:text-red-600"
                          onClick={() => {
                            const next = {...modelMap};
                            delete next[alias];
                            saveModelMap(next);
                          }}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {!Object.keys(modelMap).length && (
              <div className="py-10 text-center text-xs text-muted-foreground">
                {t('settings.mapEmpty')}
              </div>
            )}
          </div>
        </TabsContent>

        {/* ═══ 管理用户 ═══ */}
        <TabsContent value="users" className="mt-4 space-y-4">
          {isAdmin && (
            <div className="rounded-[20px] bg-muted p-4">
              <div className="mb-1 text-sm font-medium">{t('settings.usersNew')}</div>
              <div className="mb-3 text-[11px] text-muted-foreground">
                {t('settings.usersNewDesc')}
              </div>
              <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-4">
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('login.username')}</Label>
                  <Input value={newUser.username} onChange={(e) => setNewUser({...newUser, username: e.target.value})} className="bg-background" />
                </div>
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('login.password')}</Label>
                  <Input type="password" value={newUser.password} onChange={(e) => setNewUser({...newUser, password: e.target.value})} className="bg-background" />
                </div>
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('settings.usersRole')}</Label>
                  <Select value={newUser.role} onValueChange={(v) => setNewUser({...newUser, role: v})}>
                    <SelectTrigger className="bg-background" aria-label={t('settings.usersRole')}><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="admin">{t('settings.roleAdmin')}</SelectItem>
                      <SelectItem value="viewer">{t('settings.roleViewer')}</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <Button
                  className="rounded-full"
                  disabled={busy}
                  onClick={async () => {
                    if (!newUser.username.trim() || !newUser.password) {
                      notify.err(t('settings.usersRequired'));
                      return;
                    }
                    setBusy(true);
                    try {
                      await settingsApi.addUser(newUser);
                      notify.ok(t('settings.usersCreated'));
                      setNewUser({username: '', password: '', role: 'viewer'});
                      reload();
                    } catch (e) {
                      notify.err(errText(e));
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  <Plus />
                  {t('keys.create')}
                </Button>
              </div>
            </div>
          )}

          <div className="overflow-hidden rounded-[20px] bg-muted">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('login.username')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('settings.usersRole')}</TableHead>
                  {isAdmin && <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">{t('accounts.colActions')}</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {users.map((u) => (
                  <TableRow key={u.username} className="border-b border-border/40">
                    <TableCell className="pl-4 text-sm font-medium">{u.username}</TableCell>
                    <TableCell>
                      <Badge variant="secondary" className="rounded-full">
                        {u.role === 'admin' ? t('profile.roleAdmin') : t('profile.roleViewer')}
                      </Badge>
                    </TableCell>
                    {isAdmin && (
                      <TableCell className="pr-4 text-right">
                        <div className="flex justify-end gap-1">
                          {/* 重置密码走与「删除用户」同一套对话框（原来这里是
                              window.prompt，同一行里两种风格）。校验、清空、
                              改完自己要不要重新登录等逻辑都在组件里。 */}
                          <ResetPasswordDialog
                            username={u.username}
                            trigger={
                              <Button variant="ghost" size="sm" className="h-7 rounded-full text-xs">
                                {t('settings.resetPassword')}
                              </Button>
                            }
                          />
                          <ConfirmDialog
                            title={t('settings.deleteUserTitle', {name: u.username})}
                            description={t('settings.deleteUserDesc')}
                            confirmText={t('keys.delete')}
                            destructive
                            onConfirm={async () => {
                              try {
                                await settingsApi.removeUser(u.username);
                                notify.ok(t('keys.deleted'));
                                reload();
                              } catch (e) {
                                notify.err(errText(e));
                              }
                            }}
                            trigger={
                              <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md text-red-500 hover:text-red-600">
                                <Trash2 className="h-3.5 w-3.5" />
                              </Button>
                            }
                          />
                        </div>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {/* 取不到用户列表时**不能**说「暂无管理用户」——那等于断言「这个系统里
                一个账号都没有」，而实际是没取到。两者含义完全相反（前者会让人以为
                管理员账号被清空了），所以失败时如实说「未取到」并给「重试」。 */}
            {!users.length && (
              usersFailed ? (
                <LoadError message={t('state.partialFailed')} onRetry={reload} />
              ) : (
                <EmptyState
                  icon={Users}
                  title={t('settings.usersEmpty')}
                  description={t('settings.usersEmptyDesc')}
                  className="flex flex-col items-center justify-center py-12 text-center"
                />
              )
            )}
          </div>
        </TabsContent>

        {/* ═══ 访问令牌 ═══ */}
        <TabsContent value="proxy" className="mt-4 space-y-4">
          <ProxyPanel />
        </TabsContent>

        <TabsContent value="tokens" className="mt-4 space-y-4">
          <TokensPanel />
        </TabsContent>

        {/* ═══ 系统更新 ═══ */}
        <TabsContent value="system" className="mt-4 space-y-4">
          <UpdatePanel />
        </TabsContent>

        {/* ═══ 数据备份 ═══ */}
        <TabsContent value="backup" className="mt-4 space-y-4">
          <PgSyncPanel />
        </TabsContent>

        {/* ═══ 更新日志 ═══ */}
        <TabsContent value="changelog" className="mt-4">
          <ChangelogPanel />
        </TabsContent>

        {/* ═══ 关于 ═══ */}
        <TabsContent value="about" className="mt-4">
          <div className="rounded-[20px] bg-muted p-5 text-xs leading-6 text-muted-foreground">
            <div className="mb-2 flex items-center gap-2 text-sm font-medium text-foreground">
              <SettingsIcon className="h-4 w-4" />
              WorkBuddy Manager
            </div>
            <p>
              {t('settings.aboutDesc')}
            </p>
            <p className="mt-3">
              {t('settings.aboutCredits')}
            </p>
            <p className="mt-3">
              <RichText text={t('settings.aboutUsage')} />
            </p>
          </div>
        </TabsContent>
      </Tabs>

      {/* 子路由的 page 不渲染内容（只让 URL 存在，理由见本组件上方）。这里照常
          渲染 children 是为了不违背 layout 的约定：拿到的 children 就该被渲染，
          只是那 7 个 page 都返回 null，所以渲染出来是空的。 */}
      {children}
    </div>
  );
}

/**
 * 首屏骨架。
 *
 * 形状照着真实版面走（一条 Tab 栏 + 两张卡片），数据到位时不会整版跳一下。
 * 用 `SkeletonBar` 而不是 `Skeleton`：卡片是 `bg-muted`，而 `Skeleton` 自带的
 * `bg-accent` 与它在两套主题下是同一个字面量，放上去等于画了看不见的条
 * （见 components/common/states/SkeletonBar.tsx）。
 */
function SettingsSkeleton() {
  return (
    <>
      <SkeletonBar className="h-9 w-72 max-w-full rounded-full" />

      <div className="rounded-[20px] bg-muted p-4">
        <SkeletonBar className="mb-4 h-3.5 w-24" />
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {Array.from({length: 8}, (_, i) => (
            <SkeletonBar key={i} className="h-9 w-full rounded-xl" />
          ))}
        </div>
      </div>

      <div className="rounded-[20px] bg-muted p-4">
        <SkeletonBar className="mb-4 h-3.5 w-32" />
        <div className="space-y-3">
          {Array.from({length: 6}, (_, i) => (
            <SkeletonBar key={i} className="h-9 w-full rounded-xl" />
          ))}
        </div>
      </div>
    </>
  );
}

import axios, {AxiosError} from 'axios';
import {BASE_PATH} from './base-path';
import {t, tp} from './i18n';
import type {Realm} from './realm-context';
import type {
  Account,
  AccountsResponse,
  ApiKey,
  ApiToken,
  AuditLogPage,
  Changelog,
  CheckinLogPage,
  ClaimInfo,
  CreatedApiToken,
  CreatedRedPacket,
  CreditExpiry,
  CreditsMeta,
  DrawResult,
  IpAccessLog,
  IpRule,
  KeyExportResult,
  KeyImportDetectResult,
  KeyImportResult,
  KeyImportStatus,
  Me,
  ModelCatalog,
  ModelListResponse,
  Page,
  PgSyncConfig,
  PgSyncConfigResponse,
  PgSyncStatus,
  PgSyncTestResult,
  PlaygroundModels,
  RedPacket,
  RedPacketDetail,
  RedPacketKind,
  RedPacketMode,
  ReloadState,
  RequestLog,
  SecurityConfig,
  StatsSummary,
  TaskLogResponse,
  TaskRunStatus,
  UpdateCheck,
  UpdateStatus,
  UpstreamConfig,
  UpstreamEndpoint,
  UpstreamStats,
  UpstreamStatus,
  UsageBreakdown,
  UsageHourPoint,
  UsagePoint,
  UserItem,
  Versions,
} from './types';

export const http = axios.create({
  // 子路径部署时接口也挂在前缀下（反向代理剥掉前缀再转发给本服务）。
  // 这里必须用 basePath，否则所有请求都会打到域名根路径的 /api 上。
  baseURL: BASE_PATH,
  withCredentials: true,
  timeout: 60000,
});

/**
 * 统一抽取后端错误信息。
 *
 * 后端的报错是中文（服务端不做多语言，见 README 的多语言说明），这里过一遍
 * 短语表：命中已收录的后端文案就换成当前语言，没收录的原样展示——既不需要
 * 后端改造，也不会因为漏收录而显示成键名或空白。
 *
 * 403 **不做统一改写**。这个状态码在本项目里有四种互不相干的含义：角色不够
 * （`require_admin`）、接口只收会话不收 API Token（`require_session_admin`）、
 * 本机导入的开关没开、以及调用方不是面板所在的机器。后两种的原文是**可照做的
 * 操作说明**（去哪儿开开关、改用「导出配置」），一律改写成「权限不足」会把
 * 用户唯一能照着做的那句话抹掉。所以只要后端给了文案，就永远优先用它。
 *
 * 兜底只在后端**没给**文案时生效。那种情况下原本会露出 axios 自己的英文
 * message（"Request failed with status code 403"）——界面明明是多语言的，
 * 偏偏在这条路径上漏出英文，而且对用户没有任何指导意义。
 */
export function errText(e: unknown): string {
  const ax = e as AxiosError<{detail?: string; error?: string}>;
  const d = ax?.response?.data;
  const raw = (typeof d === 'string' ? d : d?.detail || d?.error) || '';
  if (raw) return tp(raw);
  if (ax?.response?.status === 403) return t('error.forbidden');
  const fallback = ax?.message || '';
  return fallback ? tp(fallback) : tp('请求失败');
}

http.interceptors.response.use(
  (r) => r,
  (error: AxiosError) => {
    if (error.response?.status === 401 && typeof window !== 'undefined') {
      // 会话失效：清掉缓存的登录态，避免仍显示管理员入口
      window.sessionStorage.removeItem('wb-me');
      const path = window.location.pathname;
      // **公开页不跳登录页**：红包抽奖页就是给没账号的人看的（同事朋友收到链接
      // 直接打开），而它自己也会加载 /api/me（根 layout 的 AuthProvider 一挂载
      // 就校验一次会话）—— 未登录必然 401，于是被这个拦截器立刻踢去登录页，
      // 用户根本没机会点「开启」。那正是「不注册也能领」的反面。
      // 这不改变服务端的鉴权（那些端点本来就是公开的），只是别在前端自己拦自己。
      // 将来再加公开页，往这个清单里加一条即可。
      // 路径必须带 basePath：basePath 只自动作用于 next/router 的跳转，裸的
      // window.location.href 会跳到域名根（通常 404）—— 登录页与公开页都一样。
      const loginPath = `${BASE_PATH}/login`;
      const publicPaths = [loginPath, `${BASE_PATH}/claim`];
      if (!publicPaths.some((p) => path.startsWith(p))) {
        window.location.href = loginPath;
      }
    }
    return Promise.reject(error);
  },
);

const get = async <T>(url: string, params?: Record<string, unknown>): Promise<T> =>
  (await http.get<T>(url, {params})).data;
const post = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.post<T>(url, body)).data;
const patch = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.patch<T>(url, body)).data;
const put = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.put<T>(url, body)).data;
const del = async <T>(url: string): Promise<T> => (await http.delete<T>(url)).data;

/* ── 鉴权 ───────────────────────────────────────────── */
export const authApi = {
  me: () => get<Me>('/api/me'),
  login: (username: string, password: string) =>
    post<{ok: boolean; username: string; role: string}>('/api/login', {username, password}),
  logout: () => post<{ok: boolean}>('/api/logout'),
  /** 吊销当前用户的**全部**会话（含本机）——服务端递增会话版本，所有 cookie 立即失效。 */
  revokeSessions: () => post<{ok: boolean; relogin_required: boolean}>('/api/sessions/revoke'),
};

/* ── 账号 ───────────────────────────────────────────── */
/**
 * 账号接口的分组参数（`?upstream_id=`）。默认分组（null / undefined）= 不带
 * 参数，与升级前的请求逐字一致——「默认分组的定义」只有后端一份（upstreamsvc）。
 */
const groupQs = (upstreamId?: number | null): string =>
  upstreamId == null ? '' : `?upstream_id=${encodeURIComponent(String(upstreamId))}`;

export const accountApi = {
  proxies: () => get<{routes: string[]; default: string}>('/api/proxies'),
  setProxy: (filename: string, proxy: string, upstreamId?: number | null) =>
    put<{ok: boolean; proxy: string; reload_triggered: boolean}>(
      `/api/accounts/${encodeURIComponent(filename)}/proxy` + groupQs(upstreamId), {proxy}),
  /**
   * 某分组的账号列表。upstreamId 省略 / null = 默认分组。
   *
   * 「分组」= 一套上游实例（账号池）：账号文件读该分组的目录、运行状态问该
   * 分组的上游。两者必须成对——只换其中一个会把别的组的账号标错状态。
   */
  list: (upstreamId?: number | null) =>
    get<AccountsResponse>('/api/accounts',
                          upstreamId == null ? undefined : {upstream_id: upstreamId}),
  upload: (files: File[], upstreamId?: number | null) => {
    const body = new FormData();
    files.forEach((file) => body.append('files', file, file.name));
    return http.post<{
      ok: boolean;
      uploaded: Array<{file: string; uid: string; updated: boolean}>;
      failed: Array<{file: string; message: string}>;
    }>('/api/accounts/upload' + groupQs(upstreamId), body).then((r) => r.data);
  },
  /**
   * 发起扫码登录；realm 决定国内版 / 国际版端点。
   *
   * region 也一并带上：服务端会替我们盯着这张码（前端被节流也不影响），
   * 而地区登记必须在落盘**前**完成 —— 后台那条路径拿不到 region 就会漏掉它
   * （国际版新号聊天报 14017）。用户在弹窗里改地区会重新发码，所以这里带的
   * 总是当前这张码对应的地区。
   */
  start: (realm: Realm = 'cn', upstreamId?: number | null, region?: string, proxy?: string) =>
    post<{state: string; authUrl: string; realm: Realm}>(
      '/api/auth/start' + groupQs(upstreamId), {realm, region, proxy}),
  /** 轮询扫码结果。region 仅国际版需要（新号必须先做地区注册） */
  poll: (state: string, realm?: Realm, region?: string, upstreamId?: number | null) =>
    get<{
      status: 'waiting' | 'success' | 'expired' | 'invalid' | 'realm_mismatch';
      uid?: string;
      nickname?: string;
      updated?: boolean;
      realm?: Realm;
      region_note?: string;
      expected?: Realm;
      got?: Realm;
    }>('/api/auth/poll', {state, realm, region, upstream_id: upstreamId ?? undefined}),
  remove: (file: string, upstreamId?: number | null) =>
    del<{success: boolean}>(`/api/accounts/${encodeURIComponent(file)}${groupQs(upstreamId)}`),
  /**
   * 把账号**移动**到另一个分组（账号页「移动到分组…」）。
   *
   * 后端移动的是账号文件本身（凭证不改一个字节）；`toUpstreamId = 0` 表示
   * 移回默认分组（见 routers/accounts.py 的 move 端点）。upstreamId = 源分组。
   */
  move: (file: string, toUpstreamId: number, upstreamId?: number | null) =>
    post<{ok: boolean; file: string; uid: string;
          from: {id: number | null; name: string};
          to: {id: number | null; name: string}}>(
      `/api/accounts/${encodeURIComponent(file)}/move${groupQs(upstreamId)}`,
      {to_upstream_id: toUpstreamId}),
  /** 临时禁用 / 启用账号（issue #21）：改文件名 + 触发上游重载。 */
  setDisabled: (file: string, disabled: boolean, upstreamId?: number | null) =>
    post<{ok: boolean; file: string; disabled: boolean; changed: boolean;
          reload_triggered: boolean; message: string}>(
      `/api/accounts/${encodeURIComponent(file)}/disabled${groupQs(upstreamId)}`, {disabled}),
  checkin: (file: string, upstreamId?: number | null) =>
    post<{
      code: number;
      message: string;
      /** 今天已经签过：后端没打上游请求，直接就地返回。提示语要与「刚签上」分开 */
      already?: boolean;
      credits?: number | null;
    }>(`/api/accounts/${encodeURIComponent(file)}/checkin${groupQs(upstreamId)}`),
  /** 单个账号的实时积分（直接向腾讯查询） */
  credits: (file: string, upstreamId?: number | null) =>
    get<{
      ok: boolean;
      credits: number | null;
      message: string;
      cached: boolean;
      cache_age: number | null;
      expiries?: CreditExpiry[];
    }>(`/api/accounts/${encodeURIComponent(file)}/credits`,
       upstreamId == null ? undefined : {upstream_id: upstreamId}),
  /** 并发刷新所有账号的实时积分 */
  /** 查询全部账号积分；force=false 时 60 秒内命中服务端缓存 */
  refreshCredits: (force = true, upstreamId?: number | null) =>
    post<{
      total: number;
      succeeded: number;
      credits: Record<string, number | null>;
      meta: Record<string, CreditsMeta>;
      failed: string[];
    }>('/api/accounts/refresh-credits?force=' + (force ? 'true' : 'false')
       + (upstreamId == null ? '' : `&upstream_id=${encodeURIComponent(String(upstreamId))}`)),
  checkinAll: (upstreamId?: number | null) =>
    post<{
      /**
       * 只统计**本次真正发起签到**的账号：国际版（不适用）与今天已签到的都不进
       * 分母，分别见 skipped / already。原先 total 里混着「今天已签过」的账号，
       * 界面会把「无需重复」说成「刚签成功」。
       */
      total: number;
      succeeded: number;
      /** 今天已签到、本次被跳过的账号数（没打上游请求） */
      already: number;
      /** 不适用的账号数（国际版没有签到体系）。它既不算成功也不算失败 */
      skipped: number;
      results: {
        nickname: string; ok: boolean; message: string;
        code?: number; skipped?: boolean; already?: boolean;
      }[];
    }>('/api/accounts/checkin-all' + groupQs(upstreamId)),
  /** 签到记录（分页）。days 用于时间范围筛选 */
  checkinLogs: (limit = 20, offset = 0, uid?: string, days?: number, realm?: Realm) =>
    get<CheckinLogPage>('/api/checkin-logs', {limit, offset, uid, days, realm}),
  clearCheckinLogs: () => post<{ok: boolean}>('/api/checkin-logs/clear'),
  /** 自动任务记录（分页）。kind / days 为筛选条件 */
  taskLogs: (limit = 20, offset = 0, kind?: string, uid?: string, days?: number, realm?: Realm) =>
    get<TaskLogResponse>('/api/task-logs', {limit, offset, kind, uid, days, realm}),
  collectTaskLogs: () => post<{ok: boolean; parsed: number; added: number}>('/api/task-logs/collect'),
  clearTaskLogs: () => post<{ok: boolean}>('/api/task-logs/clear'),
  upstreamLogs: (limit = 200) =>
    get<{available: boolean; lines: string[]; total: number}>('/api/upstream/logs', {limit}),
  test: (file: string, upstreamId?: number | null) =>
    post<{ok: boolean; message: string}>(
      `/api/accounts/${encodeURIComponent(file)}/test${groupQs(upstreamId)}`),
  refresh: (file: string, upstreamId?: number | null) =>
    post<{ok: boolean; message: string}>(
      `/api/accounts/${encodeURIComponent(file)}/refresh${groupQs(upstreamId)}`),
  /** 强制清除账号级冷却、熔断/降权与模型级限流（会重启一次上游）。 */
  clearCooling: (file: string, upstreamId?: number | null) =>
    post<{ok: boolean; message: string; uid?: string; backup?: string}>(
      `/api/accounts/${encodeURIComponent(file)}/clear-cooling${groupQs(upstreamId)}`,
    ),
  /**
   * 给账号写备注（issue #67）。传空串 = 清除备注。
   * 存的是本端库、按 uid 关联——临时停用（改文件名）不会丢。
   */
  setNote: (file: string, note: string, upstreamId?: number | null) =>
    put<{ok: boolean; uid: string; note: string}>(
      `/api/accounts/${encodeURIComponent(file)}/note${groupQs(upstreamId)}`,
      {note},
    ),
  restart: (upstreamId?: number | null) =>
    post<{ok: boolean; message: string}>('/api/restart' + groupQs(upstreamId)),

  /* ── 成长任务一键执行（issue #19）─────────────────────
   * 调用上游自带的 scripts/task_runner.py。full（点亮）会伪造活跃上报，
   * 因此单独要求 confirm，与幂等的 claim 区分开。 */
  taskRunStatus: () => get<TaskRunStatus>('/api/task-run'),
  taskRunStart: (mode: 'preview' | 'claim' | 'full', target = 'ALL', confirm = false) =>
    post<{ok: boolean; message: string}>('/api/task-run', {mode, target, confirm}),
  taskRunStop: () => post<{ok: boolean; message: string}>('/api/task-run/stop'),

  /* ── 定时领奖配置：**后端有、前端有意不做 UI** ───────────────
   * 这两个封装是**故意留着但没人调**的，不要当成「漏接线的死代码」删掉，
   * 也不要顺手补一个入口——这是一次明确的决策，不是疏忽：
   *
   *   1. 定时领奖本身只跑幂等认领（不伪造行为），但它是**无人值守**的写操作：
   *      用户设完就忘了，出了问题（上游限流、账号失效）没有任何人在场看到。
   *   2. 设置页的「定时任务」区块走的是**上游配置**（`SCHEDULE_FIELDS`），
   *      而这里走的是**管理端自己的调度器**——两套独立机制。界面只暴露前者时，
   *      这层困惑不会被放大；**一旦补了入口，用户会看到两个都叫「定时」的东西**。
   *      所以补入口之前必须先决定这两套是合并还是分层展示。
   *
   * 也就是说：入口的问题不是「没做」，是「还差一个前置决策」。详见
   * `server/services/taskrun.py` 的 `get_schedule()` 与 UI-UX-ROADMAP 的 P1-8 / P1-9。 */
  taskClaimSchedule: () => get<{enabled: boolean; hours: number[]}>('/api/task-claim-schedule'),
  saveTaskClaimSchedule: (enabled: boolean, hours: number[]) =>
    put<{enabled: boolean; hours: number[]}>('/api/task-claim-schedule', {enabled, hours}),
};

/* ── 上游状态 ───────────────────────────────────────── */
export const upstreamApi = {
  /** 上游运行状态。upstreamId 非空时查**该分组**的实例（多账号池）。 */
  status: (upstreamId?: number | null) =>
    get<UpstreamStatus>('/api/status',
                        upstreamId == null ? undefined : {upstream_id: upstreamId}),
  /** 上游模型简表；realm 非空时只返回该版本的条目 */
  models: (realm?: Realm) => get<ModelListResponse>('/api/models', {realm}),
};

/* ── 模型中心 ───────────────────────────────────────── */
export const modelApi = {
  /** 指定版本的模型目录；force=true 绕过 5 分钟缓存 */
  catalog: (realm: Realm = 'cn', force = false) =>
    get<ModelCatalog>('/api/model-catalog', {realm, force}),
};

/* ── 聊天测试台 ─────────────────────────────────────── */
export const playgroundApi = {
  /** 指定版本的可选模型 + 各自支持的推理档位（与模型中心同源） */
  models: (realm: Realm = 'cn') =>
    get<PlaygroundModels>('/api/playground/models', {realm}),
};

/* ── API 密钥 ───────────────────────────────────────── */
/* ── 多上游（账号池分组，见 server/upstreamsvc.py）────
 * 密钥绑定上游后，它的请求只走那个上游的账号池；未绑定 = 默认上游。
 * 写接口是**会话管理员**专属（带着上游 api_key，属配置级凭据）。
 *
 * 注意 `UpstreamWrite` 与 `UpstreamEndpoint` 是**两套形状**：`api_key` 只在请求里
 * 出现（响应只回脱敏值与 has_key，见 routers/upstreams.py 的 _serialize）。 */
export type UpstreamWrite = {
  name: string;
  base_url: string;
  api_key?: string;
  note?: string;
  enabled?: boolean;
  /**
   * 该分组的本地账号目录（绝对路径）。账号页的「分组」视图按它列账号 /
   * 添号 / 移动账号；空串 = 该分组只用于密钥转发。
   */
  auth_dir?: string;
  /** 该分组上游实例的容器名（可选）：「重启该分组」按它 docker restart。 */
  container?: string;
};

export const upstreamsApi = {
  list: () => get<{items: UpstreamEndpoint[]}>('/api/upstreams'),
  create: (body: UpstreamWrite) => post<UpstreamEndpoint>('/api/upstreams', body),
  update: (id: number, body: Partial<UpstreamWrite>) =>
    patch<UpstreamEndpoint>(`/api/upstreams/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/upstreams/${id}`),
  /** 探测该上游是否可达（走它的 /healthz）；失败原因原样返回给界面。 */
  probe: (id: number) => post<{ok: boolean; message: string}>(`/api/upstreams/${id}/probe`),
};

export const keyApi = {
  list: () => get<ApiKey[]>('/api/keys'),
  create: (body: Partial<ApiKey>) => post<ApiKey>('/api/keys', body),
  update: (id: number, body: Partial<ApiKey>) => patch<ApiKey>(`/api/keys/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/keys/${id}`),
  resetUsage: (id: number) => post<{ok: boolean}>(`/api/keys/${id}/reset-usage`),
  /**
   * 检查模型白名单里哪些名字匹配不到已知模型（issue #46）。
   *
   * 判据在后端（与调用侧同一份），前端只负责显示——不然就是第二份事实来源。
   * `checked: false` 表示拿不到模型清单（缓存未就绪），此时**不能**把空
   * `unknown` 当成「全部正确」展示。
   */
  checkModels: (models: string[], realm: string) =>
    post<{checked: boolean; unknown: string[]; reason?: string}>(
      '/api/keys/check-models', {models, realm}),
  /**
   * 把一把**刚创建**的密钥导出为客户端配置片段（cc-switch / ZCode）。
   *
   * 必须传明文 `token`：面板只存哈希，库里拿不回明文——这个端点的存在前提
   * 就是「调用方此刻手里有明文」。因此它只在一次性展示弹窗里被调用，
   * 密钥列表那行（只有 prefix）导不出来。
   */
  exportConfig: (body: {
    client: 'ccswitch' | 'zcode';
    token: string;
    app?: 'claude' | 'codex';
    baseUrl?: string;
    providerName?: string;
    models?: string[];
    defaultModel?: string;
  }) => post<KeyExportResult>('/api/keys/export', body),
  /**
   * 本机导入的可用状态（**只读探测**，不写任何东西）。
   *
   * 要看三件事：面板侧开关是否打开、这次请求是否来自面板所在机器、目标
   * 客户端是否已安装且未在运行。三者齐了才谈得上「一键导入」——界面据此
   * 提前把不能用的原因说清楚，用户就不会点完才知道不行。
   *
   * 开关关着时返回 200 + `enabled: false`（报错会被当成故障，而这里只是
   * 一个默认关闭的可选特性）。
   */
  importLocalStatus: () => get<KeyImportStatus>('/api/keys/import-local/status'),
  /**
   * 把刚创建的密钥**直接写进本机**的 cc-switch / ZCode 配置（真一键）。
   *
   * 与 `exportConfig` 同一份参数、同一份配置生成逻辑，区别只在去向：导出把
   * 片段交给用户，导入替用户落盘。返回体里**没有密钥**。
   *
   * 可预期的失败都有明确状态码，`errText` 能直接取到可照做的说明：
   * 403 = 开关没开或不是本机访问；404 = 本机没装该客户端；
   * 409 = 客户端正在运行（或探测不到），退出客户端后可重试。
   */
  importLocal: (body: {
    client: 'ccswitch' | 'zcode';
    token: string;
    app?: 'claude' | 'codex';
    baseUrl?: string;
    providerName?: string;
    models?: string[];
    defaultModel?: string;
    /** 是否把导入的供应商设为当前（cc-switch 置 is_current；ZCode 移到最前） */
    setCurrent?: boolean;
    /**
     * 导入方式。`auto`（默认）= 能走客户端官方深链就走深链；
     * `deeplink` / `direct` 是给它兜底和排障用的强制值。
     */
    mode?: 'auto' | 'deeplink' | 'direct';
    /**
     * 直写方式下，客户端正在运行就替用户关掉、写完再拉起来。
     * 关掉前会先确认定位得到它的可执行文件——关掉却拉不起来比不改更糟。
     */
    closeRunning?: boolean;
  }) => post<KeyImportResult>('/api/keys/import-local', body),
  /**
   * 自动检测客户端装在哪（「自动检测」按钮）。
   *
   * 为什么需要：安装路径因机器而异——绿色版可能解压在 `E:\cc swich\`，安装版在
   * `%LOCALAPPDATA%\Programs\…`。检测顺序是"可信度从高到低"：环境变量 →
   * 运行中的进程 → 注册表里的协议处理器 → 上次结果 → 常见安装位 → 受限扫描。
   * 命中后会缓存在面板数据目录，之后不用再扫。**只读**，不写用户配置。
   */
  importLocalDetect: (client: 'ccswitch' | 'zcode' = 'ccswitch') =>
    get<KeyImportDetectResult>('/api/keys/import-local/detect', {client}),
};

/* ── 红包：批量发放带额度的密钥（见 server/redpacket.py）────
 * create 返回**明文 key**，且仅此一次（库里只存哈希）。 */
export const redPacketApi = {
  list: () => get<RedPacket[]>('/api/red-packets'),
  detail: (id: number) => get<RedPacketDetail>(`/api/red-packets/${id}`),
  create: (body: {
    title: string;
    quota_kind: RedPacketKind;
    total_amount: number;
    shares: number;
    mode: RedPacketMode;
    /** 有效期（天）。null = 用后端默认值（7 天） */
    ttl_days: number | null;
    /** 模型白名单：**token 红包必填、积分红包必须为空**（见 server/redpacket.py） */
    models: string[];
  }) => post<CreatedRedPacket>('/api/red-packets', body),
  /** 收回整批（停用这批密钥，可逆）。返回停用的数量。 */
  revoke: (id: number) => post<{revoked: number}>(`/api/red-packets/${id}/revoke`),
};

/* ── 抽奖（**公开**，不需要登录）─────────────────────────
 * 收到链接的是同事朋友，不该要求他们注册账号。防滥用靠「每 IP 一次」
 * + 128 位抽奖码 + 有效期。 */
export const claimApi = {
  info: (code: string) => get<ClaimInfo>(`/api/claim/${encodeURIComponent(code)}`),
  /** 抽一份。同一 IP 第二次会 409（提示「你已经抽过了」）。 */
  draw: (code: string) => post<DrawResult>(`/api/claim/${encodeURIComponent(code)}`, {}),
};

/* ── 访问令牌（管理面作用域化 API Token）──────────────────
 * 与 keyApi 是两套：那个是给下游调模型的网关密钥，这个授权管理接口。
 * 明文只在创建时返回一次。 */
export const tokenApi = {
  list: () => get<ApiToken[]>('/api/tokens'),
  create: (body: {name: string; scope: 'readonly' | 'admin'; expires_at: number | null}) =>
    post<CreatedApiToken>('/api/tokens', body),
  update: (id: number, body: {
    name?: string;
    scope?: 'readonly' | 'admin';
    enabled?: boolean;
    expires_at?: number | null;
  }) => patch<ApiToken>(`/api/tokens/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/tokens/${id}`),
};

/* ── 日志 ───────────────────────────────────────────── */
export const logApi = {
  list: (params: Record<string, unknown>) => get<Page<RequestLog>>('/api/logs', params),
  clear: () => post<{ok: boolean}>('/api/logs/clear'),
};

/* ── 用量统计 ───────────────────────────────────────── */
export const statsApi = {
  /** realm 非空时只统计该版本（界面按版本切换时传） */
  summary: (realm?: Realm) => get<StatsSummary>('/api/stats/summary', {realm}),
  daily: (days = 30, realm?: Realm) => get<UsagePoint[]>('/api/stats/daily', {days, realm}),
  /**
   * 某天**按小时**的用量（默认今天）。「今日」趋势图用它 —— 范围只有一天时
   * 按天聚合只有一根柱子。后端固定返回 24 个桶（补零），前端不必再补。
   */
  hourly: (day?: string, realm?: Realm) =>
    get<UsageHourPoint[]>('/api/stats/hourly', {day, realm}),
  byModel: (days = 30, realm?: Realm) =>
    get<UsageBreakdown[]>('/api/stats/by-model', {days, realm}),
  byKey: (days = 30, realm?: Realm) =>
    get<UsageBreakdown[]>('/api/stats/by-key', {days, realm}),
  /**
   * 上游自己那份统计（issue #59）。口径与本页其它数字不同：含**直连上游**的调用，
   * 且是自上游进程启动以来的累计（没有时段概念）。
   */
  upstream: () => get<UpstreamStats>('/api/stats/upstream'),
  /** 按请求日志回填用量缺口（幂等） */
  rebuildUsage: () =>
    post<{rows_before: number; rows_after: number; requests_delta: number; tokens_delta: number}>(
      '/api/stats/rebuild-usage',
    ),
  repairUsage: () =>
    post<{repaired: number; requests: number; tokens: number}>('/api/stats/repair-usage'),
};

/* ── 安全 / IP ──────────────────────────────────────── */
export const securityApi = {
  config: () => get<SecurityConfig>('/api/security/config'),
  saveConfig: (body: SecurityConfig) => post<SecurityConfig>('/api/security/config', body),
  rules: () => get<IpRule[]>('/api/security/rules'),
  addRule: (body: {kind: 'allow' | 'deny'; cidr: string; note?: string}) =>
    post<IpRule>('/api/security/rules', body),
  removeRule: (id: number) => del<{ok: boolean}>(`/api/security/rules/${id}`),
  logs: (limit = 200) => get<IpAccessLog[]>('/api/security/logs', {limit}),
  logsClear: () => post<{ok: boolean}>('/api/security/logs/clear'),
};

/* ── 上游配置 / 用户 ───────────────────────────────── */
export const settingsApi = {
  upstream: () => get<UpstreamConfig>('/api/settings/upstream'),
  saveUpstream: (body: Record<string, unknown>) =>
    post<UpstreamConfig>('/api/settings/upstream', body),
  /** 测试 Upstash 连通性；token 留空表示使用已保存的值 */
  testUpstash: (url: string, token?: string) =>
    post<{ok: boolean; message: string}>('/api/settings/upstash/test', {url, token}),
  /** 上游重载状态（保存配置后自动重启） */
  reloadState: () => get<ReloadState>('/api/upstream/reload-state'),
  /** 立即重启上游（一般无需手动调用，保存配置会自动重载） */
  reloadUpstream: () =>
    post<{ok: boolean; message: string}>('/api/settings/upstash/reload'),
  modelMap: () => get<Record<string, string>>('/api/settings/model-map'),
  saveModelMap: (body: Record<string, string>) =>
    post<Record<string, string>>('/api/settings/model-map', body),
  users: () => get<UserItem[]>('/api/users'),
  /** 管理端审计日志（登录/改密码/增删用户等，仅管理员可读） */
  auditLogs: (limit = 200) => get<AuditLogPage>('/api/audit-logs', {limit}),
  addUser: (body: {username: string; password: string; role: string}) =>
    post<UserItem>('/api/users', body),
  /** 改密码 / 改角色；会吊销该用户的既有会话（改自己则需重新登录） */
  updateUser: (username: string, body: {password?: string; role?: string}) =>
    patch<UserItem & {sessions_revoked?: boolean; relogin_required?: boolean}>(
      `/api/users/${encodeURIComponent(username)}`, body),
  removeUser: (username: string) => del<{ok: boolean}>(`/api/users/${encodeURIComponent(username)}`),
};

export type {Account};

/* ── 系统维护（一键更新）──────────────────────────────── */
export const systemApi = {
  updateStatus: () => get<UpdateStatus>('/api/system/update-status'),
  /** 检测新版本；force=true 绕过 6 小时缓存 */
  checkUpdate: (force = false) => get<UpdateCheck>('/api/system/check-update', {force}),
  versions: () => get<Versions>('/api/system/versions'),
  /** 启动一键更新；target: manager | upstream | both */
  startUpdate: (target: 'manager' | 'upstream' | 'both') =>
    post<{ok: boolean; message: string}>('/api/system/update', {target}),
  /** 固定上游版本（空串 = 取消固定，恢复跟随分支） */
  setUpstreamRef: (ref: string) =>
    post<{ok: boolean; upstream_ref: string}>('/api/system/upstream-ref', {ref}),
  /**
   * 清除上次更新的结果与日志（issue #105）。
   *
   * 此前失败记录只能靠「下次发起更新」覆盖：状态文件与 update.log 都留着，
   * 界面上那条「更新未完成」与日志永远擦不掉，用户得进容器手删文件。
   * 更新进行中后端会拒绝（409）——那会把「正在更新」看丢。
   */
  clearUpdateStatus: () =>
    del<{ok: boolean; message: string}>('/api/system/update-status'),
  /** 更新日志（解析仓库根目录 CHANGELOG.md，离线可用） */
  changelog: () => get<Changelog>('/api/system/changelog'),
};

/* ── PostgreSQL 异地备份（设置 → 数据备份）────────────── */
export const pgSyncApi = {
  config: () => get<PgSyncConfigResponse>('/api/settings/pg-sync'),
  /** 保存连接配置；password 传掩码或空串表示沿用已保存的值 */
  save: (body: Partial<PgSyncConfig>) =>
    post<{config: PgSyncConfig}>('/api/settings/pg-sync', body),
  /** 探测连通性。表单里刚填的值优先，没填的字段回落到已保存的配置 */
  test: (body: Partial<PgSyncConfig>) =>
    post<PgSyncTestResult>('/api/settings/pg-sync/test', body),
  /** 把本地数据全量推到 PostgreSQL（只读本地，不会改动它） */
  exportData: () =>
    post<{ok: boolean; message: string; status: PgSyncStatus}>('/api/settings/pg-sync/export'),
  /** 从 PostgreSQL 拉回数据。**会覆盖本地数据**，前端必须先二次确认 */
  importData: () =>
    post<{ok: boolean; message: string; status: PgSyncStatus}>('/api/settings/pg-sync/import'),
  status: () => get<PgSyncStatus>('/api/settings/pg-sync/status'),
};

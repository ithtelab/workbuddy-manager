export type Role = 'admin' | 'viewer';

export interface Me {
  username: string;
  role: Role;
}

export interface Account {
  proxy?: string;
  /** 该令牌签发的总时长（秒）；后端从 JWT 解出，解不出为 null */
  ttl_seconds?: number | null;
  /** 令牌签发时间（秒）≈ 最近一次刷新时间；后端从 JWT iat 解出，解不出为 null */
  issued_at?: number | null;
  file: string;
  uid: string;
  nickname: string;
  enterprise_id: string;
  expires_at: number;
  is_expired: boolean;
  remain_seconds: number;
  /** 当前可花费积分余额（上游聚合套餐剩余额度），null 表示尚未同步 */
  credits?: number | null;
  /** 来自 workbuddy2api /status 的运行时字段，可能为空 */
  healthy?: boolean | null;
  disabled?: boolean | null;
  /** 被上游禁用时的原因（如 11140 request illegal 需重新登录）；空串表示未给出 */
  disabled_reason?: string | null;
  in_flight?: number | null;
  cooling?: boolean | null;
  /** 冷却剩余秒数（上游权威值：可能是上游明说的重置时刻，也可能是有界退避）；null = 未在冷却 */
  cool_remaining_sec?: number | null;
  /**
   * 被限流的模型清单（上游限额台账）。多模型限流时各模型的恢复时刻**不同**，
   * 账号级冷却时间不足以表达，故逐条列出。
   * 字段名照上游 /status 的 JSON：model / until / reset_at / reason。
   */
  /**
   * 本端给这个账号写的备注（issue #67）。按 uid 存在本端库里，不在上游账号文件里，
   * 所以临时停用（改文件名）不会丢。没有备注时是空串（不是缺字段）。
   */
  note?: string;
  /**
   * 今天成功签到的时刻（epoch 秒）；null / 缺省 = 今天还没签。
   *
   * 判定依据是本端签到记录（腾讯对「今天已签过」回 10001，我们照记成功），
   * 所以它同时代表「本面板签过」与「今天已签到」。界面据此把签到按钮变成
   * 已签到态，避免重复点击（详见 server/routers/accounts.py 的 `_today_start`）。
   */
  checkin_today?: number | null;
  rate_limited_models?: {
    model: string;
    /** 该模型的冷却截止（已被 soft_rate_max 截断） */
    until?: string;
    /** 上游明说的重置时刻（未截断）；无时间文案时缺省 */
    reset_at?: string;
    reason?: string;
  }[];
  success_count?: number | null;
  err_total?: number | null;
  breaker_fails?: number | null;
  /**
   * 连败降权截止（上游 issue #114，RFC3339 字符串）。在未来 = 正被降权。
   *
   * 为什么要有：上游把降权**计入 cooling**，所以「冷却中」里混着两类原因完全不同
   * 的情况——限流退避（等一会儿就好）与连败降权（这个号在持续失败）。不区分时
   * 用户看到「冷却中」无从判断该等还是该处理。
   */
  degrade_until?: string | null;
  /** 连续失败计数（降权进度：达阈值即降权，成功后清零） */
  consecutive_fails?: number | null;
  last_success?: string | null;
  last_used?: number | null;
  source: 'file' | 'pool';
  /**
   * 该账号是否出现在上游账号池里（`/status.accounts`）。
   *
   * 我们读的是 auths/ 目录下的文件，上游读的才是池——两者不总一致：上游
   * `LoadDir` 对解析失败的文件静默跳过（`Parse` 在 accessToken 为空时报错）。
   * 那种文件不在池里、永远选不中；若不标出来，界面会显示成「在线」，
   * 出现「面板全绿但调用报没有健康账号」的矛盾。
   */
  in_pool?: boolean;
  /**
   * 本次没取到上游状态（`/status` 里 `connected: false`）——运行时字段全部未知。
   *
   * 与 `in_pool: false` 是**两回事**：前者是「看不到上游」，后者是「上游明确没
   * 加载它」。混为一谈会把正常账号误报成文件损坏（实测确认过这个误报）。
   */
  poolUnknown?: boolean;
  /** 已知上游不会加载该文件时的原因（空 = 未发现明显问题） */
  invalid_reason?: string;
  /**
   * 本面板**主动临时停用**（issue #21）——文件名带 `.disabled` 后缀，
   * 上游的 `workbuddy*.json` glob 因此不再匹配它、不加载该账号。
   *
   * 与 `disabled` 是两回事：那个是上游按错误分类自动禁的（需重新登录），
   * 这个是运维手动停用的，在面板上再点一次「启用」即可恢复。
   *
   * **注意**：新版上游走 `manual_disabled` 状态位停用（见下），那时本字段为
   * false 而 `manual_disabled` 为 true。两种停用都要当「已停用」看。
   */
  disabled_by_panel?: boolean;
  /**
   * 上游的**手动停用**状态位（上游 issue #138/#118，本面板 issue #45）。
   *
   * 语义是「对话流量摘除」而非「账号冻结」：账号仍在池里、**签到 / token 保活 /
   * 猫猫旅行照常执行**，凭证与积分都是活的，只是不被选中转发。与 `disabled`
   * 并列独立（上游对叠加态分别透出），两位都清才真正回池。
   *
   * 有它的时候优先用它：副作用比改名（`disabled_by_panel`）小得多。
   *
   * `null` = 上游这份快照里没这个字段（账号不在池里，或旧版上游没有该状态位）。
   * 与 `false`（明确未停用）区分，同 `disabled` 的口径。
   */
  manual_disabled?: boolean | null;
  /** 手动停用的原因文案（仅 `manual_disabled` 为真时有值） */
  manual_reason?: string;
  /** 账号所属版本（cn / global）；存量账号按域名回退，无该字段时视为 cn */
  realm?: 'cn' | 'global';
  /** 该版本是否支持签到体系（国际版没有） */
  checkin_supported?: boolean;
  /** auth 文件里的 domain，便于确认归属 */
  domain?: string;
}

export interface AccountsResponse {
  total: number;
  accounts: Account[];
  /** 已从上游同步到运行时状态的账号数 */
  pool_synced?: number;
  /** 上游是否可达 */
  pool_available?: boolean;
  /**
   * 分组上下文（多账号池）：这次列表来自哪个分组。默认分组也带这个字段
   * （is_default = true）——界面靠它标注「本列表属于哪一组」。
   */
  upstream?: {id: number | null; name: string; is_default: boolean};
  /** 该分组的本地账号目录；空串 = 未配置（该分组在面板里只能看、不能管理账号） */
  auth_dir?: string;
  /** 该分组能否在面板管理账号（= 配置了本地账号目录） */
  manageable?: boolean;
}

/** 上游为一组账号给出的计数（`/status` 的顶层汇总与 realm_totals 同构） */
export interface PoolCounts {
  total: number;
  healthy: number;
  cooling: number;
  disabled: number;
  in_flight_full: number;
}

export interface UpstreamStatus {
  connected: boolean;
  accounts?: Record<string, unknown>[];
  cooling?: number;
  disabled?: number;
  healthy?: number;
  total?: number;
  in_flight_full?: number;
  /**
   * 上游**按版本分好组**的计数（cn / global）。
   * 界面切到某个版本时要的是这一份，而不是顶层那份全局汇总——
   * 后者的 healthy/cooling 含两个版本，直接用会在国际版视图下显示国内版的数。
   * 注意 healthy 是**汇总层**字段，账号明细里没有它（曾因此在界面上恒显示 0）。
   */
  realm_totals?: Record<string, PoolCounts>;
  redis_mode?: string;
  sticky_sessions?: number;
  error?: string;
}

export interface ModelInfo {
  id: string;
  owned_by?: string;
  /** 上下文窗口（上游动态拉取给出；静态回退表为固定值） */
  context_length?: number;
  context_window?: number;
  /**
   * 最大输出 token。**只有上游动态拉取的条目才带这个键**，内置静态回退表没有，
   * 前端据此判断列表来源（见 ModelListResponse.source）。
   */
  max_output_tokens?: number | null;
}

/** 模型列表来源：dynamic = 上游实时动态拉取；static = 上游内置静态回退表 */
export type ModelSource = 'dynamic' | 'static' | 'unknown';

export interface ModelListResponse {
  models: ModelInfo[];
  source: ModelSource;
  count: number;
}

/* ── 模型中心（模型目录）────────────────────────────────── */

/** 模型目录里的一条：比 /v1/models 多显示名与推理档位 */
export interface CatalogModel {
  id: string;
  /** 显示名，来自腾讯模型接口；上游回退数据为空串 */
  name: string;
  /** 上下文窗口（token，0 = 未知） */
  context_length: number;
  /** 最大输出（token，0 = 未知） */
  max_output_tokens: number;
  /** 支持的推理档位，如 ['low','high','max']；空数组 = 非推理模型或未提供 */
  efforts: string[];
  /** 模型描述（腾讯的 descriptionZh，中文）；空串 = 未提供 */
  description?: string;
  /**
   * 积分倍率原文（如 "x0.05"）：同一 prompt 在不同模型上的扣费倍率。
   * **仅展示**，不参与选号（与上游口径一致）。空串 = 未提供。
   */
  credits?: string;
  /** 厂商标识（如 volc / deepseek）；空串 = 未提供 */
  vendor?: string;
  /** 模型标签（含 badge:限时免费 等） */
  tags?: string[];
  /** 是否默认模型 */
  is_default?: boolean;
  supports_reasoning?: boolean;
  supports_tool_call?: boolean;
  /** 纯推理模型（不产出正文，只出思维链） */
  only_reasoning?: boolean;
  /** 推理摘要模式（如 "auto"） */
  reasoning_summary?: string;
  /** 默认推理档位；空串 = 上游未声明（由上游自行回退到硬编码默认） */
  default_effort: string;
  /** 平台声明的图片输入能力；null 表示未知或冲突，不代表模型原生多模态 */
  supports_images: boolean | null;
  native_modality?: 'text' | 'multimodal' | 'router' | 'unknown';
  native_modality_source?: string;
  native_modality_verified_at?: string;
  image_input_conflict?: boolean;
  image_input_sources?: Record<string, boolean | null>;
  /** 系列归属（按 id 前缀推导，仅用于分组浏览） */
  series: string;
}

export interface CatalogSummary {
  total: number;
  /** 带推理档位的模型数 */
  reasoning: number;
  /** 上下文 ≥128K 的模型数 */
  large_context: number;
  /** 最大上下文（token） */
  max_context: number;
  /** 涉及系列 */
  series: string[];
  unique_ids: number;
}

/**
 * 数据来源：
 * - `tencent`：直连腾讯模型接口，字段最全（含显示名与推理档位）
 * - `upstream`：回退到上游 /v1/models，字段有限
 * - `none`：两者都拿不到
 */
export type CatalogSourceKind = 'tencent' | 'upstream' | 'none';

export interface ModelCatalog {
  models: CatalogModel[];
  source: CatalogSourceKind;
  /** 来源的中文说明，直接用于界面标注 */
  source_label: string;
  /** 本次数据取自哪个账号（回退时为 workbuddy2api） */
  via: string;
  /** 逐次尝试的失败原因，便于排查（不展示给普通用户也要留着） */
  errors: string[];
  /** 命中缓存时为 true */
  cached: boolean;
  /** 缓存已存在多少秒 */
  cache_age: number;
  summary: CatalogSummary;
}

/**
 * 上游接入点（多上游 / 账号池分组）。
 *
 * `id === null && is_default` 那条是**默认上游**：来自环境变量 / 上游 config.json，
 * 不是数据库记录，所以它没有 id、也不可改不可删。
 */
export interface UpstreamEndpoint {
  id: number | null;
  name: string;
  base_url: string;
  /**
   * 上游凭据**不明文回传**（与 api_key / upstash.token 同规矩）：接口只给
   * 「有没有配」与脱敏值。编辑时留空即不修改。
   */
  has_key: boolean;
  api_key_masked: string;
  note: string;
  enabled: boolean;
  is_default: boolean;
  /**
   * 该分组的**本地账号目录**（绝对路径）：面板按它列账号 / 添号 / 移动账号。
   * 空串 = 该分组只用于密钥转发，不管账号。默认分组 = 部署时的 WB_AUTH_DIR。
   */
  auth_dir: string;
  /**
   * 该分组上游实例的容器名（可选）：面板「重启该分组」按它 docker restart。
   * 空串 = 不能从面板重启（账号文件的增删改由上游热加载自动收录）。
   */
  container: string;
  /** 有多少把密钥绑定在它上面（默认上游那行 = 未绑定上游的密钥数） */
  bound_keys: number;
  created_at?: number | null;
  updated_at?: number | null;
}

export interface ApiKey {
  id: number;
  name: string;
  prefix: string;
  enabled: boolean;
  expires_at: number | null;
  max_ips: number;
  ip_allowlist: string[];
  models: string[];
  /**
   * 版本归属：'cn' | 'global'，空串 = 不限制（两版都能调）。
   *
   * 空串是**存量密钥**的形态（该字段引入前创建的），保持其原有行为不变；
   * 新建密钥会跟随当前所在版本写入。
   */
  realm: 'cn' | 'global' | '';
  quota: number | null;
  used_tokens: number;
  /**
   * 积分额度与已用量（issue #27）：按上游返回的**真实扣费**（usage.credit）累计。
   *
   * 与 token 额度各自独立，任一超限即拒绝调用；0 = 不限。
   * 为什么不只按 token 限额：同样 1M token，便宜模型与贵模型的扣费能差几十倍，
   * 按 token 估不出实际花了多少积分（提需求的人遇到的正是这个问题）。
   */
  quota_credit: number;
  used_credit: number;
  /**
   * 绑定的上游接入点（多上游 / 分组隔离）；null = 默认上游。
   *
   * 为什么是可空 id 而不是「上游名字」：默认上游不是数据库里的一行
   * （见 server/upstreamsvc.py），空值本身就代表「走默认」这个合法状态。
   */
  upstream_id?: number | null;
  /**
   * 来源红包的 id；null = 手工建的。
   *
   * 密钥列表按它分成两个 tab：红包一次生成一批、额度零碎，与手工建的混在
   * 一起很难看（也从没人会去逐把编辑红包发出去的密钥）。
   */
  packet_id: number | null;
  created_at: number;
  last_used_at: number | null;
  /** 仅在创建时返回一次 */
  key?: string;
}

/**
 * 密钥导出结果（`POST /api/keys/export`）。
 *
 * 两个客户端的载荷载体不同，故用可选字段而不是联合类型：调用方按 `client`
 * 取值即可，避免为两个分支各写一份收窄逻辑。
 *   · ccswitch → `app_type` + `settings_config`
 *   · zcode    → `provider`（片段，含 providerRule 与 providerModelRules）
 */
export interface KeyExportResult {
  client: 'ccswitch' | 'zcode';
  /** 面板对外地址 + /v1，客户端实际要填的 base URL */
  base_url: string;
  realm: 'cn' | 'global';
  /** **网关口径**的模型 id（带 cn: / global: 前缀） */
  models: string[];
  name: string;
  app_type?: 'claude' | 'codex';
  settings_config?: Record<string, unknown>;
  provider?: Record<string, unknown>;
}

/**
 * 本机一键导入：单个客户端的可写状态（见 server/services/keyimport.py）。
 *
 * `running` 是**三态**：true / false / null，null = 探测不到。后端把 null 也
 * 当作"不能写"（fail-closed）：漏判的代价是写坏用户本机配置，误判的代价只是
 * 让用户改用导出，两者不对称。
 */
export interface KeyImportClientStatus {
  client: 'ccswitch' | 'zcode';
  /** 该客户端的配置文件是否存在 */
  installed: boolean;
  /** 是否正在运行；null = 探测不到 */
  running: boolean | null;
  /** 综合结论：现在能不能*直接写*（客户端没在跑时才为 true） */
  importable: boolean;
  /** 不能直写的原因；可写时为空串 */
  reason: '' | 'not_installed' | 'client_running' | 'cannot_detect';
  /** 会被写入的文件路径 */
  target: string;
  /**
   * 面板认得的可执行文件；null = 还没定位到（界面提示可点「自动检测」）。
   * 直写模式下要在写完后把客户端拉起来，靠的就是它。
   */
  exe: string | null;
  /** 上面那个路径是从哪认出来的：env / process / protocol / cache / registry / known-dir / scan */
  exe_source: string;
  /** 正在运行、但可以自动关闭（定位到了 exe）——界面要提前说清"会先替你关掉它" */
  needs_close: boolean;
  /** 本机支持客户端官方的 ccswitch:// 深链导入（首选路径：不用关客户端、不用改它的库） */
  deeplink: boolean;
}

export interface KeyImportStatus {
  /** 面板侧开关（WB_LOCAL_IMPORT），默认关 */
  enabled: boolean;
  /** 这次请求是否来自面板所在机器；false 时只能导出后手动导入 */
  local_caller: boolean;
  clients: KeyImportClientStatus[];
}

/**
 * 导入结果。**不含密钥**——服务端刻意不回显：配置已经落盘，没必要再把明文
 * 放进响应体（明文只该在「创建密钥」那一次出现）。
 */
export interface KeyImportResult {
  client: 'ccswitch' | 'zcode';
  app: '' | 'claude' | 'codex';
  realm: 'cn' | 'global';
  base_url: string;
  model_count: number;
  /**
   * 走的是哪条路：
   *  · `deeplink` = 把配置拼成客户端官方的 ccswitch:// 链接交给系统，
   *    由客户端自己弹确认框、自己入库（此时 `action` 是 `handed-off`）；
   *  · `direct`   = 面板直接改它的配置/数据库。
   */
  method: 'deeplink' | 'direct';
  /** created = 新增；updated = 更新了已有项；handed-off = 已交给客户端去导入 */
  action: 'created' | 'updated' | 'handed-off';
  provider_id: string;
  /** 实际写入的文件（深链模式下为空：面板没碰文件） */
  target: string;
  /** 写入前的备份（可据此回滚）；深链模式下为空 */
  backup: string;
  /** 直写模式才有的进程生命周期：是否被面板关掉/重新拉起 */
  lifecycle: {
    stopped: boolean;
    forced: boolean;
    /** null = 没关过（谈不上重开）；false = 关了但没拉起来 */
    restarted: boolean | null;
    exe: string;
  } | null;
  name?: string;
  app_type?: 'claude' | 'codex';
  /** 深链模式：真正接收这条链接的客户端可执行文件 */
  handler?: string;
}

/** 自动检测客户端位置的结果（面板的「自动检测」按钮）。 */
export interface KeyImportDetectResult {
  client: 'ccswitch' | 'zcode';
  found: boolean;
  exe: string | null;
  /** 从哪找到的；没找到时为空串。界面据此说明"是从注册表/扫描里认出来的" */
  source: '' | 'env' | 'process' | 'protocol' | 'cache' | 'registry'
    | 'known-dir' | 'scan';
}

/**
 * 管理面**作用域化 API Token**（见 docs/api-tokens.md）。
 *
 * 与上面那把网关密钥（`ApiKey`）是**两套东西**：`ApiKey` 只授权模型调用；
 * 本类型授权的是管理接口 `/api/*`，供脚本 / CI 免登录调用。
 */
export interface ApiToken {
  id: number;
  name: string;
  /** 明文前 12 字符，用于展示与识别（明文本身不会再回传） */
  prefix: string;
  /** 权限：只读 / 管理员（角色由它决定） */
  scope: 'readonly' | 'admin';
  enabled: boolean;
  /** 到期时刻（epoch 秒）；null = 永不过期 */
  expires_at: number | null;
  created_at: number;
  /** 创建者用户名（审计用） */
  created_by: string;
  last_used_at: number | null;
  last_used_ip: string | null;
}

/** 创建令牌的返回：比 ApiToken 多一个**仅此一次**的明文字段。 */
export interface CreatedApiToken extends ApiToken {
  token: string;
}

export interface RequestLog {
  id: number;
  ts: number;
  key_id: number | null;
  key_name: string | null;
  ip: string;
  model: string | null;
  mapped_model: string | null;
  status: number;
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
  /**
   * 首字延迟（毫秒）：从发起上游请求到收到第一个含正文的 delta。
   * null = 未采集（非流式请求，或升级前的历史记录）。
   * 与 latency_ms 的区别：latency_ms 含模型生成全部内容的耗时，回答越长越大，
   * 反映不出上游响应快慢；首字延迟才是「上游多久开始回话」。
   */
  first_token_ms: number | null;
  /**
   * 提示词缓存的三段 token（issue #69）：上游（腾讯）在流式末帧 usage 里给。
   * **null = 上游没给这三个字段**（老上游），与「给了 0」不是一回事——
   * 后者代表这次请求确实没命中缓存。界面据此显示「—」而不是 0%。
   *
   * 前缀缓存是**按账号**存的，所以「换了号」与「没命中」常常一起出现，
   * 与下面那个 account 列对着看才有意义。
   */
  cache_hit_tokens: number | null;
  cache_miss_tokens: number | null;
  cache_write_tokens: number | null;
  ua: string | null;
  error: string | null;
  stream: boolean;
  /** 本次调用的真实扣费（上游 usage.credit）；null = 上游未返回，不是 0 */
  credit: number | null;
  /**
   * 本次实际用了哪个上游账号，形如 `昵称(uid8)`。
   *
   * 账号由上游决定、不在响应里回传，本端是**采集上游容器日志后按时间对回来的**，
   * 所以比请求本身晚几秒——刚打完的请求这一列可能还是 null（界面显示「—」，
   * 稍后刷新即有）。null 也可能是「日志已滚掉」或「上游没在跑容器」。
   */
  account: string | null;
}

export interface UsagePoint {
  day: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** 当日实际扣费合计 */
  credit: number;
  /**
   * 当日失败请求数（4xx + 5xx 合计）。
   *
   * 单独来自 `request_logs` 而非用量汇总——后者只含有 token 或扣费的请求，
   * 被拒绝的调用与全池不可用（503，零 token）在里面根本不存在。所以这一项
   * 补上了「用量表天然看不到失败」的盲区。
   *
   * 可选：老版本后端不返回该字段，界面按 0 处理（不显示失败）。
   */
  failed?: number;
}

/**
 * 某一天**按小时**的用量（`/api/stats/hourly`）。
 *
 * 「今日」趋势图用：范围只有一天时按天聚合只会得到一根柱子（那正是用户反馈的
 * 「今日还是柱状图，不太对劲」）。与 `UsagePoint` **同源同口径** —— 小时表与
 * 按天表由同一处写入累计，所以图与页头卡片永远一致，清日志也不会打架。
 */
export interface UsageHourPoint {
  /** `YYYY-MM-DD` */
  day: string;
  /** 0-23（本地时区） */
  hour: number;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  credit: number;
  /** 该小时的失败请求数（来自请求日志，与 `UsagePoint.failed` 同一口径） */
  failed: number;
}

export interface UsageBreakdown {
  name: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** 该维度实际扣费合计 */
  credit: number;
}

export interface StatsSummary {
  today_requests: number;
  today_tokens: number;
  /** 实际扣费（上游 usage.credit 合计）；上游未返回该字段时恒为 0 */
  today_credit: number;
  week_credit: number;
  total_credit: number;
  week_requests: number;
  week_tokens: number;
  total_requests: number;
  total_tokens: number;
  active_keys: number;
  top_model: string | null;
  /**
   * 统计写入的健康状态。`ok: false` 时详情说明为什么可疑。
   *
   * 存在意义：统计是旁路写入（失败不影响转发），坏了以后界面看不出异常——
   * 数字只是停着不动，页面照常轮询。所以要把「今天有请求但统计为 0」这种
   * 组合显式报出来，而不是等用户自己发现。
   */
  /**
   * `logs_today` 是**本该累计用量**的今日调用数（服务端按与 `bump_usage`
   * 相同的条件统计）。界面用它拼译文，不要去正则解析 `detail` 那句中文——
   * 那样改文案会让译文静默失效（曾经如此）。
   */
  usage_health?: {ok: boolean; detail: string; logs_today?: number};
  /**
   * 失败请求数（4xx / 5xx 分档，今天与近 7 天）。
   *
   * 为什么不在 `today_requests` 里体现：那个数来自用量汇总，只含**成功**
   * 请求（有 token 或有扣费）。失败请求在汇总里完全不存在，于是界面上的
   * 「请求数」与趋势图都只反映成功量——全池中断那天看起来像「没有请求」。
   * 这一项把失败显式补出来。
   *
   * 可选：老版本后端不返回，界面按全 0 处理（不显示失败相关提示）。
   */
  failures?: {
    today_4xx: number;
    today_5xx: number;
    week_4xx: number;
    week_5xx: number;
  };
}

export interface IpRule {
  id: number;
  kind: 'allow' | 'deny';
  cidr: string;
  note: string;
  created_at: number;
}

export interface IpAccessLog {
  id: number;
  ts: number;
  ip: string;
  path: string;
  blocked: boolean;
  ua: string | null;
  /**
   * 拦截原因短码（issue #33）：missing_key / invalid_key / ip_blocked /
   * key_disabled / key_expired / quota_exhausted / credit_quota_exhausted /
   * ip_not_allowed / too_many_ips / realm_mismatch / model_not_allowed /
   * rate_limited 等。
   *
   * 稳定短码而非散文：展示侧按语言翻译，改文案不必迁移历史数据。
   * 存量记录为 null（那时没记原因），界面显示「未记录」。
   */
  reason?: string | null;
}

export interface SecurityConfig {
  enabled: boolean;
  mode: 'whitelist' | 'blacklist';
}

export interface UserItem {
  username: string;
  role: Role;
}

export interface UpstashConfig {
  /** Upstash 地址，支持 https://xxx.upstash.io 或 xxx.upstash.io */
  url: string;
  /** 是否已配置 token（内容不回传） */
  has_token: boolean;
  /** token 掩码，仅用于展示 */
  token_masked: string;
}

export interface UpstreamConfig {
  /** 是否成功读取到上游配置文件；false 时前端应提示并禁止保存 */
  available?: boolean;
  /**
   * 保存后若需要用户手动做一步（容器部署下无法自动重启上游），后端带回此提示。
   * 有值时界面用醒目提示转达，而不是显示「正在自动应用」。
   */
  reload_hint?: string;
  /** 上游配置文件路径 */
  config_path?: string;
  /** 读取失败原因 */
  error?: string;
  listen?: string;
  api_key_masked?: string;
  auth_dir?: string;
  /** 上游 config.json 中声明的 auth_dir，与管理端不一致时出现 */
  upstream_auth_dir?: string;
  schedule?: Record<string, unknown>;
  pool?: Record<string, unknown>;
  cooldown?: Record<string, unknown>;
  features?: Record<string, unknown>;
  session_sticky?: Record<string, unknown>;
  prompt?: Record<string, unknown>;
  server?: Record<string, unknown>;
  upstream?: Record<string, unknown>;
  /** 国际版（workbuddy.ai）路由：enabled / chat_base / billing_base */
  global?: Record<string, unknown>;
  /**
   * 上游的运维管理端点开关：enabled。
   *
   * 决定账号页「临时停用」走哪条路——开启后走上游的停用接口（只摘转发流量，
   * 签到与保活照常），不开启则回退为改文件名（账号完全退出账号池）。
   * 上游默认关闭，所以面板提供一个开关，否则用户只能手改 config.json。
   */
  admin?: Record<string, unknown>;
  upstash?: UpstashConfig;
}

/** 一个套餐的到期时刻与它名下的可用额度 */
export interface CreditExpiry {
  /** 到期时刻（epoch 秒）。腾讯下发的是 UTC+8 墙钟，后端已换算成绝对时刻 */
  at: number;
  /** 该套餐当前的可用额度 */
  amount: number;
}

/** 积分查询来源：实时查询 or 命中 60 秒缓存 */
export interface CreditsMeta {
  cached: boolean;
  cache_age: number | null;
  message?: string;
  /** 各套餐到期时间，按到期时刻升序；只含仍有余额的套餐 */
  expiries?: CreditExpiry[];
}

export interface CheckinLog {
  id: number;
  ts: number;
  uid: string;
  nickname: string;
  /** 触发来源：manual 手动 / manual-batch 批量 / add 添加账号 / auto 上游自动 */
  source: string;
  /** 类型：checkin 签到 / keepalive 保活 */
  kind: string;
  success: boolean;
  code: number | null;
  message: string;
  /** true = 来自上游自动签到（采集器落库），false = 本端触发 */
  auto?: boolean;
}

/** 上游自动任务留痕（猫猫旅行 / 活跃上报 / 自动签到 / 保活） */
export interface TaskLog {
  id: number;
  ts: number;
  uid: string;
  /** travel / activity / checkin / keepalive / user-resource / credit */
  kind: string;
  /** credit 有积分收益 / ok 成功 / info 跳过 / warn 警告 / error 失败 */
  level: string;
  credits: number;
  /** 上游英文原文（排查用，界面上作为悬浮提示） */
  message: string;
  /** 中文展示文案 */
  message_cn?: string;
  /** 账号昵称（后端按 uid/前缀解析；上游日志只带 uid 前 8 位） */
  nickname?: string;
}

export interface TaskLogResponse {
  logs: TaskLog[];
  /** 当前筛选下的总条数（分页用；与 stats.total 在未筛选时一致） */
  total: number;
  stats: {
    by_kind: Record<string, {count: number; credits: number}>;
    total: number;
    total_credits: number;
  };
  kinds: Record<string, string>;
  collector: {at?: number; parsed?: number; added?: number; error?: string};
}

/**
 * 成长任务一键执行的状态（调用上游自带的 scripts/task_runner.py）。
 *
 * 三种模式按**风险分级**，界面必须让用户看清区别：
 *   preview 只查询（dry-run，不发写请求）
 *   claim   只领已完成任务的奖励（幂等，不伪造行为）
 *   full    点亮 + 领奖（会伪造活跃上报，有风控风险，需二次确认）
 */
export interface TaskRunStatus {
  running: boolean;
  mode: string;
  target: string;
  started_at: number;
  finished_at: number;
  exit_code: number | null;
  timed_out: boolean;
  error: string;
  /** 输出尾部（脚本按行打印，后端只保留最近若干行） */
  lines: string[];
  /** 上游脚本与账号目录是否就位；false 时 unavailable_reason 说明原因与做法 */
  available: boolean;
  unavailable_reason: string;
  /** 定时领奖配置（只有幂等的认领，不含点亮） */
  schedule: {enabled?: boolean; hours?: number[]};
}


/** 签到记录分页返回 */
export interface CheckinLogPage {
  items: CheckinLog[];
  /** 当前筛选下的总条数（本端触发 + 上游自动） */
  total: number;
  /** 其中本端触发的条数 */
  local_total?: number;
  /** 其中上游自动签到的条数 */
  auto_total?: number;
}

export interface UpdateLogLine {
  ts: number;
  level: string;
  text: string;
}

export interface UpdateStatus {
  available: boolean;
  /** 是否运行在容器里（决定能力边界，见 can_update_upstream） */
  in_container?: boolean;
  /**
   * 是否支持「更新上游」。容器部署为 false —— 重建上游容器需要 docker CLI，
   * 而挂载 docker.sock 会把宿主 root 权限交给容器内进程，是本项目刻意不做的事。
   */
  can_update_upstream?: boolean;
  /** 是否正在更新 */
  running: boolean;
  /** 上次更新是否成功（null = 未运行过） */
  ok: boolean | null;
  step: string;
  logs: UpdateLogLine[];
  /** 当前部署版本 */
  version: string;
  updater_found: boolean;
  upstream_dir: string;
  /** 当前固定的上游版本（空 = 跟随分支） */
  upstream_ref?: string;
  started_at?: number;
  finished_at?: number | null;
  duration?: number;
  pid?: number;
  /** 日志原文（便于复制反馈） */
  log_tail?: string;
  /**
   * 发布包签名校验结果（供应链防护）。
   * verified = 已验签通过；skipped = 走了 WB_SKIP_SIGNATURE 绕过开关（有风险）；
   * none 或缺失 = 未执行验签（如旧版本更新器）。
   */
  signature?: {status: 'verified' | 'skipped' | 'none'; detail?: string};
}

export interface VersionSide {
  /** 当前版本 */
  current: string;
  /** 远端最新 */
  latest: string;
  /** 是否有更新可用 */
  has_update: boolean;
  /** 检测失败原因 */
  error: string;
}

export interface ManagerVersion extends VersionSide {
  /** Release 页面地址 */
  url: string;
  repo: string;
}

/** 上游两个版本之间的单条提交 */
export interface UpstreamChange {
  sha: string;
  subject: string;
  date: string;
}

export interface UpstreamVersion extends VersionSide {
  /** 最新提交时间 */
  date: string;
  /** 最新提交说明 */
  subject: string;
  /** 本地落后远端多少个提交（0 = 未知或不落后） */
  ahead?: number;
  /** 两版本之间的提交总数 */
  total?: number;
  /** 变更说明列表（最新在前，最多 20 条） */
  changes?: UpstreamChange[];
  /** 提交数超过展示上限，列表被截断 */
  truncated?: boolean;
  repo: string;
}

export interface UpdateCheck {
  /** 检测时间（秒） */
  checked_at: number;
  /** 是否为缓存结果 */
  cached: boolean;
  manager: ManagerVersion;
  upstream: UpstreamVersion;
  /** 任一组件有更新 */
  has_any: boolean;
}

export interface Versions {
  manager: string;
  upstream_connected: boolean;
  upstream_accounts: number | null;
  upstream_dir: string;
}

/** 更新日志中的一条（level 1 = 二级缩进，作为子项展示） */
export interface ChangelogItem {
  level: number;
  text: string;
}

export interface ChangelogSection {
  /** 中文分类：安全 / 新增 / 修复 / 改进 / 说明 / 计划中 */
  title: string;
  items: ChangelogItem[];
}

export interface ChangelogVersion {
  /** 版本号，未发布的开发内容为「未发布」 */
  version: string;
  /** 发布日期，未发布时为空串 */
  date: string;
  /** 是否为尚未发布的开发内容 */
  unreleased: boolean;
  sections: ChangelogSection[];
}

export interface Changelog {
  /** CHANGELOG.md 是否可读 */
  available: boolean;
  /** 不可读时的原因 */
  error?: string;
  /** 文件路径（便于排查） */
  path?: string;
  /** 版本总数（未被截断时） */
  total?: number;
  versions: ChangelogVersion[];
  /** 版本过多，列表被截断 */
  truncated?: boolean;
  /** 当前部署版本，用于高亮 */
  current?: string;
}

export interface ReloadState {
  /** 正在执行重启 */
  running: boolean;
  /** 有重启在排队（短时间内多次改动的合并） */
  pending: boolean;
  /** 上次重启完成时间（秒） */
  last_at: number;
  last_ok: boolean | null;
  last_message: string;
  restart_count: number;
}

export interface Page<T> {
  total: number;
  items: T[];
}

/* ── 聊天测试台 ─────────────────────────────────────── */

export interface PlaygroundModel {
  id: string;
  name: string;
  /** 支持的推理档位；空数组 = 该模型不支持调整 */
  efforts: string[];
  series: string;
}

export interface PlaygroundModels {
  models: PlaygroundModel[];
  source: CatalogSourceKind;
  source_label: string;
}

/* ── 管理端审计日志 ─────────────────────────────────── */

export interface AuditLog {
  id: number;
  ts: number;
  /** 操作者用户名（anonymous 表示未认证） */
  actor: string;
  /** login / login_failed / update_user / delete_user ... */
  action: string;
  target: string;
  detail: string;
  ip: string;
}

export interface AuditLogPage {
  items: AuditLog[];
  total: number;
}

/**
 * 上游自己那份统计（`/v1/stats`，issue #59）。
 *
 * 字段名照上游 JSON。**口径与面板的用量统计不同**：这份含直连上游的调用，
 * 且自上游进程启动累计——界面必须标注清楚，别与按时段统计的数字混着看。
 */
export interface UpstreamStatRow {
  model?: string;
  requests?: number;
  success?: number;
  failed?: number;
  total_tokens?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  credit?: number;
  cache_hit_rate?: number;
}

export interface UpstreamStats {
  /** 取不到时为 false，此时只有 error */
  available: boolean;
  error?: string;
  /** 上游可关闭统计采集；关闭时 enabled=false 且 message 说明原因 */
  enabled?: boolean;
  message?: string;
  since?: string;
  uptime_sec?: number;
  total?: UpstreamStatRow;
  models?: UpstreamStatRow[];
}

/* ── 红包：一次建一批带额度的密钥，管理员自己分发 ──────────
 * 见 server/redpacket.py 的模块说明（为什么不做领取页/分享码）。 */

/**
 * 额度类别。两者**限制的对象不同**，不只是单位不同：
 * credit 限制上游返回的真实扣费（口径准）；token 限制 token 总数（直观）。
 */
export type RedPacketKind = 'credit' | 'token';

/** 分配方式：lucky = 拼手气（有人多有人少），even = 均分 */
export type RedPacketMode = 'lucky' | 'even';

export interface RedPacket {
  id: number;
  title: string;
  quota_kind: RedPacketKind;
  total_amount: number;
  shares: number;
  mode: RedPacketMode;
  /**
   * 限定的模型范围。**token 红包非空、积分红包恒为空**（两类规则相反，见
   * server/redpacket.py 的 validate）：token 是「量」与模型强相关，
   * 积分是「钱」任何模型都能用。
   */
  models: string[];
  /**
   * 抽奖码 —— 拼出分享链接用。**它是凭据**：拿到就能抽走一份，
   * 所以只在管理端接口下发，不要贴到公开场合。
   */
  code: string;
  created_by: string;
  created_at: number;
  expires_at: number;
  /** 已被抽走的份数（抽奖式红包看的就是这个进度） */
  claimed: number;
  /** 整批都已停用 = 已收回（部分停用不算，那种情况去密钥页看单把） */
  revoked: boolean;
}

/** 红包里的一份（= 一个密钥）。**不含明文** —— 库里只有哈希。 */
export interface RedPacketItem {
  key_id: number;
  prefix: string;
  amount: number;
  enabled: boolean;
  used_tokens: number;
  used_credit: number;
}

export interface RedPacketDetail extends RedPacket {
  items: RedPacketItem[];
}

/**
 * 创建红包的结果：含**明文 key**，且**仅此一次**（与 keyApi.create 同理）。
 *
 * 界面上必须提示「离开后无法再看到」并提供复制/导出 —— 这是「直接发 key」
 * 方案的固有代价，不是缺陷。
 */
/** 抽奖页的元信息。**不含密钥** —— 没点「开启」之前不该能拿到。 */
export interface ClaimInfo {
  title: string;
  quota_kind: RedPacketKind;
  shares: number;
  /** 还剩几份 */
  left: number;
  models: string[];
  expires_at: number;
  expired: boolean;
  /** 本机（IP）是不是已经抽过了 */
  claimed: boolean;
  /**
   * 本机领到的那一份（没领过时为 null）。
   *
   * 第二次打开时会**直接展示**：关掉弹窗才想起没存密钥是很常见的，而明文
   * 只显示那一次 —— 刷新就能找回来，比「请联系发红包的人」有用。
   * 代价是同一 NAT 出口下的人能看到彼此的那份（红包的熟人场景下可接受）。
   */
  my_amount: number | null;
  my_key: string | null;
}

/** 抽到的那一份。`key` 是**明文**，只在抽的这一刻返回。 */
export interface DrawResult {
  amount: number;
  quota_kind: RedPacketKind;
  models: string[];
  key: string;
  expires_at: number;
}

export interface CreatedRedPacket {
  id: number;
  title: string;
  quota_kind: RedPacketKind;
  total_amount: number;
  shares: number;
  mode: RedPacketMode;
  /** 同上：token 红包非空、积分红包恒为空 */
  models: string[];
  /** 抽奖码 —— 拼分享链接用（仅此一次能拿到，之后详情接口还会给） */
  code: string;
  created_at: number;
  expires_at: number;
  keys: ApiKey[];
}

/* ── PostgreSQL 异地备份（设置 → 数据备份）────────────── */

/**
 * 连接配置。密码在读取时恒为掩码（`********`），保存时原样回传表示「不改动」——
 * 见 `server/services/pgsync.py` 的 merge_form。
 */
export interface PgSyncConfig {
  /** 是否开启定时自动备份 */
  enabled: boolean;
  host: string;
  port: number;
  dbname: string;
  user: string;
  password: string;
  /** libpq 的 sslmode；prefer 是默认值，不写进连接串 */
  sslmode: string;
  /** 自动备份间隔（分钟）；0 = 只手动 */
  interval_minutes: number;
  /** 恢复前是否自动备份本地库 */
  keep_local_backup: boolean;
  /** 上次导出完成的时刻（0 = 从未） */
  last_export_at: number;
  /** 上次恢复完成的时刻（0 = 从未） */
  last_import_at: number;
}

export interface PgSyncLogLine {
  ts: number;
  level: string;
  text: string;
}

export interface PgSyncStatus {
  running: boolean;
  /** 空串 = 从未跑过 */
  kind: '' | 'export' | 'import';
  /** null = 未运行过 */
  ok: boolean | null;
  step: string;
  /** 0-100 */
  percent: number;
  tables_total: number;
  tables_done: number;
  rows: number;
  logs: PgSyncLogLine[];
  started_at: number;
  finished_at: number;
  /**
   * 上次迁移 / 上次恢复的完成时刻（0 = 从未）。
   *
   * 随 `/status` 一起给出（issue #122）：面板的空闲轮询只拉这个轻量接口，
   * 靠这两个字段让「上次迁移 / 上次恢复」在定时备份跑完后自动刷新，
   * 不必再整份拉配置——那会覆盖用户正在填的表单。
   */
  last_export_at: number;
  last_import_at: number;
}

export interface PgSyncConfigResponse {
  config: PgSyncConfig;
  status: PgSyncStatus;
}

export interface PgSyncTestResult {
  ok: boolean;
  message: string;
  server_version?: string;
}

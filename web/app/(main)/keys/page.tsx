'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {KeyRound, Plus, Trash2, Ban, CircleCheck, Pencil, RotateCcw, Copy, Check, Loader2} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useAsyncAll} from '@/lib/use-async-data';
import {notify} from '@/lib/toast';
import {keyApi, upstreamsApi, errText} from '@/lib/api';
import {BASE_PATH} from '@/lib/base-path';
import type {ApiKey, KeyImportResult, KeyImportStatus, UpstreamEndpoint} from '@/lib/types';
import {copyText, fmtDateTime, fmtNumber} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {PageSectionTabs} from '@/components/common/layout/PageSectionTabs';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {ModelWhitelistInput} from '@/components/common/keys/ModelWhitelistInput';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {useAuth} from '@/lib/auth-context';
import {useRealm, type Realm} from '@/lib/realm-context';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
import {RichText} from '@/lib/i18n/rich-text';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {Label} from '@/components/ui/label';
import {Textarea} from '@/components/ui/textarea';
import {Tabs, TabsList, TabsTrigger} from '@/components/ui/tabs';
import {Switch} from '@/components/ui/switch';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

interface FormState {
  name: string;
  /** 新建：0 = 永不过期；编辑：0 = 保持当前有效期不变 */
  expiresDays: string;
  /** 编辑时的有效期操作：keep 保持不变 / days 从现在起 N 天 / never 永不过期 */
  expiryMode: 'keep' | 'days' | 'never';
  maxIps: string;
  ipAllowlist: string;
  models: string;
  quota: string;
  /** 积分额度（issue #27）：0 = 不限。按上游返回的**真实扣费**累计 */
  quotaCredit: string;
  /** 每日 token 限额：0 = 不限，按服务器本地日期每天 0 点恢复 */
  dailyQuota: string;
  /** 版本归属：'cn' | 'global' | ''（不限制，仅存量密钥） */
  realm: Realm | '';
  /** 绑定的上游（账号池分组）：null = 默认上游。见 server/upstreamsvc.py */
  upstream_id: number | null;
}

const emptyForm: FormState = {
  name: '',
  expiryMode: 'keep',
  expiresDays: '0',
  maxIps: '0',
  ipAllowlist: '',
  models: '',
  quota: '0',
  quotaCredit: '0',
  dailyQuota: '0',
  realm: 'cn',
  upstream_id: null,
};

function toLines(v: string): string[] {
  return v
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * 取数完成前的空值。必须是模块级同一份：写成 `values.keys ?? []` 的话每次渲染都会
 * 新建数组，进下游的依赖后每帧都变（同 dashboard / security 的处理）。
 */
const EMPTY_KEYS: ApiKey[] = [];
const EMPTY_UPSTREAMS: UpstreamEndpoint[] = [];

export default function KeysPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  const {realm} = useRealm();
  /**
   * 密钥列表 + 上游列表。**两份数据放同一个 hook，不拆。**
   *
   * 为什么这里与 `tasks` / `stats` 相反（那两页把「配件」单独拆出去）：上游列表不是
   * 配件，它是**弹窗里那个「上游」下拉的全部内容**。分开取会出现「弹窗已经打开、
   * 下拉里却只有『默认上游』」——用户会以为没有别的上游可绑，于是把密钥绑到默认池上。
   * 那是**静默的错误选择**，比「两块一起挂掉」更糟。所以宁可同生共死：
   * 一份没取到 → 整页错误态 + 重试，而不是渲染一个缺选项的表单。
   */
  const {values, errors, isInitialLoading, isInitialFailed, isRefreshing, reload} = useAsyncAll({
    keys: () => keyApi.list(),
    upstreams: () => upstreamsApi.list(),
  }, []);

  const keys: ApiKey[] = values.keys ?? EMPTY_KEYS;
  const upstreams: UpstreamEndpoint[] = values.upstreams?.items ?? EMPTY_UPSTREAMS;
  /**
   * 密钥列表**这一份**没取到。
   *
   * 两种进入方式：① 首屏那次就失败——若上游列表也没成功，整页错误态会先返回，
   * 走不到下面；② 首屏成功了、之后这次刷新失败。用来把「没有密钥」与「不知道有
   * 没有密钥」分开——两者都渲染成同一片空白，含义却正好相反。见下面空状态处的说明。
   */
  const keysFailed = 'keys' in errors;
  /** 有字段没刷新成功：常驻提示，但**不**顶掉已经显示出来的内容 */
  const partialFailed = Object.keys(errors).length > 0;
  /**
   * 列表分组。红包一次生成一批、额度零碎，与手工建的混在一起很难看。
   *
   * 默认停在「普通密钥」而不是「全部」：日常看的是自己发出去的那几把，
   * 红包那批是「发完就不太管」的；把它们混在首屏反而把常用的挤下去了。
   */
  const [tab, setTab] = useState<'normal' | 'packet'>('normal');
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ApiKey | null>(null);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [busy, setBusy] = useState(false);
  const [issued, setIssued] = useState<string | null>(null);
  /**
   * 「再复制一次」：正在查看的密钥（含明文）与正在请求的那一行。
   *
   * 明文只在点开时向后端要一次（`POST /api/keys/{id}/reveal`，会话管理员 +
   * 审计日志），**不随列表下发** —— 列表只读账号也能调，凭据不该跟着列表走。
   * `revealing` 记的是哪一行在等，避免连点把同一行请求两次。
   */
  const [revealed, setRevealed] = useState<{name: string; key: string} | null>(null);
  const [revealing, setRevealing] = useState<number | null>(null);
  /** 刚复制成功的那一行（短暂显示对勾，移动端看不到 toast 时也有反馈） */
  const [copiedId, setCopiedId] = useState<number | null>(null);
  /**
   * 一次性展示弹窗里选中的导出目标（cc-switch 还要分 Claude / Codex）。
   *
   * 为什么要选而不是直接给两个按钮：cc-switch 的 claude 与 codex 是**两份
   * 不同的配置**（前者写 env.ANTHROPIC_*，后者写 auth.OPENAI_API_KEY + TOML），
   * 点哪个都能出结果，用户得先说明要哪一份，否则只能二选一猜。
   */
  const [exportTarget, setExportTarget] = useState<'ccswitch-claude' | 'ccswitch-codex' | 'zcode'>('ccswitch-claude');
  /** 导出结果（原样展示的文本片段）；null = 还没导出 */
  const [exported, setExported] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  /**
   * 本机一键导入的状态与结果。
   *
   * `importStatus === null` 同时表示"还没探测"和"探测失败"——两种情况界面都
   * 按不可导入处理：这是给刚建完密钥的人加的一个便利，探测失败不该用报错
   * 打断他（导出那条路始终可用）。
   */
  const [importStatus, setImportStatus] = useState<KeyImportStatus | null>(null);
  const [importing, setImporting] = useState(false);
  const [detecting, setDetecting] = useState(false);
  const [imported, setImported] = useState<KeyImportResult | null>(null);
  /** 是否把导入的供应商设为当前。默认关：这等于替用户切换正在用的供应商，
   *  该由他自己点头，不能靠"顺手"发生。 */
  const [importSetCurrent, setImportSetCurrent] = useState(false);
  /**
   * 模型白名单里**匹配不到已知模型**的名字（issue #46）。
   *
   * 为什么要有：白名单是自由文本，填错不会报错，只会在下游表现为「模型列表是
   * 空的」——而空列表看不出原因（少个连字符？填成了显示名？）。所以在编辑处
   * 当场点出来。判据由后端给（与调用侧同一份），这里只负责显示。
   *
   * `null` = 还没查/查不了（拿不到模型清单）。**不能**把 null 当成「都没问题」——
   * 那会把「暂时查不了」显示成「全部正确」。
   */
  const [unknownModels, setUnknownModels] = useState<string[] | null>(null);
  /**
   * 提示元素的 ref：它出现时要**滚进可视区**。
   *
   * 为什么需要：模型白名单是弹窗里最后一个字段，提示挂在它下面——实测在
   * 常见窗口高度下，提示会落在弹窗内容区的**可视区之下**（被滚动容器裁掉），
   * 用户看不到任何东西。而这条提示的全部价值就是「当场看见」，看不见等于没做。
   * 用 `block:'nearest'`：只在需要时滚最小距离，不会把界面拽跑。
   */
  const unknownRef = useRef<HTMLParagraphElement | null>(null);
  /**
   * 提交锁（同步生效，与 `busy` 的渲染状态无关）。
   *
   * 为什么状态不够：`setBusy(true)` 要等下一次渲染才反映到按钮的 disabled 上，
   * 而快速连点/回车触发的第二次提交可能在那之前就进来了 —— 结果创建出**多把
   * 同名密钥**（实测：连点 3 次进 3 把）。用 ref 记录「已提交」，在当前这次
   * 调用栈里立即生效，不依赖渲染。
   *
   * 另外这把锁在 `finally` 里释放，失败（比如接口 500）时用户可以重试 ——
   * 不能因为一次报错就把表单永久锁死。
   */
  const submitting = useRef(false);

  // 提示出现时确保它在可视区内（见 unknownRef 的说明）
  useEffect(() => {
    if (unknownModels && unknownModels.length) {
      unknownRef.current?.scrollIntoView({block: 'nearest'});
    }
  }, [unknownModels]);

  // 密钥状态可能被下游调用改变（配额用尽、过期），心跳刷新保持同步。
  // 走 reload（刷新模式）：只把新数据换上去，**不**重走首屏流程——否则骨架会每
  // 60 秒闪一次。
  useHeartbeat(reload, 60000);

  /** 列表里显示上游名；找不到（刚被删）时退回 #id —— 不显示空白。 */
  function upstreamName(id: number): string {
    const hit = upstreams.find((u) => u.id === id);
    return hit ? hit.name : `#${id}`;
  }

  function openCreate() {
    setEditing(null);
    // 新建时默认跟随当前所在版本：在哪个版本的界面里建，就是哪个版本的密钥
    setForm({...emptyForm, realm});
    setUnknownModels(null);
    setFormOpen(true);
  }

  function openEdit(k: ApiKey) {
    setEditing(k);
    setForm({
      name: k.name,
      // 默认不改动有效期；要续期/取消过期需显式选择
      expiryMode: 'keep',
      expiresDays: '0',
      maxIps: String(k.max_ips || 0),
      ipAllowlist: (k.ip_allowlist || []).join('\n'),
      models: (k.models || []).join(', '),
      quota: String(k.quota ?? 0),
      quotaCredit: String(k.quota_credit ?? 0),
      dailyQuota: String(k.daily_quota ?? 0),
      realm: k.realm || '',
      upstream_id: k.upstream_id ?? null,
    });
    setUnknownModels(null);
    setFormOpen(true);
  }

  async function submit() {
    // 同步锁：必须在任何 await / setState 之前判断并置位。
    // 只看 `busy` 不够——那个状态要等重渲染才生效，连点会漏过去。
    if (submitting.current) return;
    if (!form.name.trim()) {
      notify.err(t('keys.nameRequired'));
      return;
    }
    submitting.current = true;
    setBusy(true);
    try {
      const days = Number(form.expiresDays) || 0;
      const payload: Record<string, unknown> = {
        name: form.name.trim(),
        max_ips: Number(form.maxIps) || 0,
        ip_allowlist: toLines(form.ipAllowlist),
        models: toLines(form.models),
        quota: Number(form.quota) || 0,
        quota_credit: Number(form.quotaCredit) || 0,
        daily_quota: Math.max(0, Math.floor(Number(form.dailyQuota) || 0)),
        // '' 是有意义的取值（不限制版本），必须照传——后端以它区分
        // 「存量密钥，两版都能调」与「限定了某一版」
        //
        // 新建时默认**以当前所在版本为准**（而不是 openCreate 时的快照）：
        // 弹窗开着的时候用户可能切了版本，若沿用快照，创建出来的密钥版本
        // 会与界面上显示的不一致——那种错是静默的，只有调用时才暴露。
        // 例外是用户在新建弹窗里显式打开了「不限制」（form.realm === ''，
        // issue #191）：那是明确的意图，不能被页面版本覆盖掉。
        realm: editing || form.realm === '' ? form.realm : realm,
        // 绑定上游（多上游 / 分组隔离）：null = 默认上游，照传——
        // 后端以它区分「显式改回默认上游」与「本次没提交该字段」（PATCH 语义）。
        upstream_id: form.upstream_id,
      };

      // 新建：填了天数才设过期（0 = 永不过期，不下发 expires_at）
      // 编辑：按显式选择处理，避免「打开就保存」把有效期重置
      if (!editing) {
        if (days > 0) payload.expires_at = Math.floor(Date.now() / 1000) + days * 86400;
      } else if (form.expiryMode === 'days') {
        if (days <= 0) {
          notify.err(t('keys.daysRequired'));
          setBusy(false);
          return;
        }
        payload.expires_at = Math.floor(Date.now() / 1000) + days * 86400;
      } else if (form.expiryMode === 'never') {
        // 后端以 null 表示「无过期时间」
        payload.expires_at = null;
      }

      if (editing) {
        await keyApi.update(editing.id, payload as Partial<ApiKey>);
        notify.ok(t('keys.keyUpdated'));
      } else {
        const created = await keyApi.create(payload as Partial<ApiKey>);
        notify.ok(t('keys.keyCreated'));
        if (created.key) {
          setIssued(created.key);
          // 上一把密钥的「一键导入」结果不能留到这一把：否则用户会以为新密钥
          // 已经写入目标、或对着旧备份路径去核对。
          setImported(null);
        }
      }
      setFormOpen(false);
      // 刷新失败**不能**把这次创建判成失败：密钥已经建好了。
      // 所以这里用 reload() 的返回值判断，而不是 `.catch()` —— reload 内部已经
      // 把异常收进 errors 并返回布尔值，返回的 Promise 永远不 reject，
      // 用 .catch 的话这段提示永远不会出现，用户只会看到「失败了」。
      if (!(await reload())) {
        notify.warn(t('keys.createdButRefreshFailed'), t('keys.createdButRefreshFailedHint'));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
      submitting.current = false;
    }
  }

  async function toggle(k: ApiKey) {
    try {
      await keyApi.update(k.id, {enabled: !k.enabled});
      notify.ok(k.enabled ? t('keys.disabled') : t('keys.enabled'));
      reload();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  /**
   * 「再复制一次」：向后端要一次明文，然后直接写进剪贴板。
   *
   * 为什么点一下就是复制、而不是先开弹窗：用户来这一行就是为了复制（密钥本身
   * 贴进客户端配置），多一次点击没有意义。`copyText` 在非安全上下文里会退回
   * `execCommand('copy')`；两条路都不行时（浏览器拦了）才把密钥显示出来让他手动
   * 选中复制 —— 静默失败只会变成「点了没反应」。
   *
   * 取不回来（存量密钥，库里只有哈希）时后端回 409 且说明该怎么办，
   * 这里原样弹给他，不再自己编一句。
   */
  async function copyKey(k: ApiKey) {
    if (revealing !== null) return;
    setRevealing(k.id);
    try {
      const r = await keyApi.reveal(k.id);
      if (await copyText(r.key)) {
        notify.ok(t('keys.keyCopied'), r.name || k.name);
        setCopiedId(k.id);
        window.setTimeout(() => setCopiedId(null), 1500);
      } else {
        setRevealed({name: r.name || k.name, key: r.key});
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setRevealing(null);
    }
  }

  // 下游接入地址：客户端才能拿到当前 origin，静态导出阶段为空。
  // 子路径部署时网关也挂在前缀下（反代剥掉前缀再转发），所以要带上 basePath，
  // 否则会把用户引到一个 404 的地址。
  const baseUrl =
    typeof window !== 'undefined' ? `${window.location.origin}${BASE_PATH}` : '';

  /**
   * 导出刚创建的密钥为客户端配置片段。
   *
   * 为什么只在弹窗里可用：面板只存哈希，`issued` 是明文唯一存在的一刻；
   * 关掉弹窗后就连服务端也拿不回完整密钥了（`/api/keys/export` 因此要求
   * 调用方传明文，而不是传 key_id）。这里把明文原样交给后端生成片段。
   *
   * 拿到结果**只展示、不落盘**：写客户端配置属「一键导入」，涉及客户端是否
   * 在运行、面板是否与本机同机等问题，是独立特性（见 docs 提案）。导出本身
   * 无副作用，可以放心先上。
   */
  // 导出与导入共用同一份请求体：目标选择器（cc-switch Claude / Codex / ZCode）
  // 同时决定"导出哪一份"和"导到哪里"。两处各写一遍迟早会漂移——后端也正是
  // 为此把两条路收敛到同一个 helper（见 routers/keys.py 的 _client_settings）。
  const clientBody = useCallback(() => {
    if (!issued) return null;
    const app = exportTarget === 'zcode' ? undefined
      : (exportTarget === 'ccswitch-codex' ? 'codex' : 'claude');
    const models = form.models.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
    return exportTarget === 'zcode'
      ? {client: 'zcode' as const, token: issued, baseUrl, models}
      : {client: 'ccswitch' as const, app: app as 'claude' | 'codex',
         token: issued, baseUrl, models};
  }, [issued, exportTarget, baseUrl, form.models]);

  /** 当前目标客户端名。导出、导入、自动检测三处都要用，抽出来免得各写一遍。 */
  const importClient: 'ccswitch' | 'zcode' =
    exportTarget === 'zcode' ? 'zcode' : 'ccswitch';

  const doExport = useCallback(async () => {
    const body = clientBody();
    if (!body) return;
    setExporting(true);
    try {
      const res = await keyApi.exportConfig(body);
      // 展示可直接粘贴的片段：cc-switch 用 settings_config，ZCode 用整个片段
      const payload = res.client === 'ccswitch' ? res.settings_config : res.provider;
      setExported(JSON.stringify(payload, null, 2));
      notify.ok(t('keys.exported'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setExporting(false);
    }
  }, [clientBody, t]);

  /**
   * 把刚创建的密钥**一键写进本机**客户端。
   *
   * 失败信息原样透传服务端那句（`errText` 取的就是 detail）：后端已经写清了
   * 是"请先退出客户端"还是"请先打开开关"，前端再包一层只会更模糊。
   *
   * `mode: 'auto'` 让后端自己选路：**能走客户端官方的 ccswitch:// 深链就走深链**
   * （不用关客户端、不用改它的库、由客户端自己弹确认框），否则才直接写配置。
   * 直写时授权 `closeRunning`——客户端开着也由它替用户关掉再拉起来，
   * 前提是它先确认定位得到客户端（关掉却拉不起来比不改更糟，后端会拒绝）。
   */
  const doImport = useCallback(async () => {
    const body = clientBody();
    if (!body) return;
    setImporting(true);
    try {
      const res = await keyApi.importLocal({
        ...body, setCurrent: importSetCurrent, mode: 'auto', closeRunning: true,
      });
      setImported(res);
      notify.ok(res.method === 'deeplink' ? t('keys.importHandedOff') : t('keys.importDone'));
      // 写完重新探测：客户端可能被我们关了又起来，状态得跟着变
      try {
        setImportStatus(await keyApi.importLocalStatus());
      } catch {
        /* 探测失败不影响"已经写成功"这个事实，保留旧状态即可 */
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setImporting(false);
    }
  }, [clientBody, importSetCurrent, t]);

  /**
   * 让面板自己去找客户端装在哪。
   *
   * 为什么需要：安装路径**因机器而异**——绿色版可能解压在 `E:\cc swich\`，
   * 安装版在 `%LOCALAPPDATA%\Programs\…`。写死任何一个都会在别人机器上失效。
   * 检测结果会缓存，之后导入 / 关闭 / 重新拉起都用它，所以是"点一次长期有效"。
   */
  const doDetect = useCallback(async () => {
    setDetecting(true);
    try {
      const res = await keyApi.importLocalDetect(importClient);
      if (!res.found) {
        notify.warn(t('keys.detectMissing'), t('keys.detectMissingHint'));
        return;
      }
      notify.ok(t('keys.detectFound'));
      // 定位结果体现在 status 的 exe 上，重新取一次让界面跟着更新
      try {
        setImportStatus(await keyApi.importLocalStatus());
      } catch {
        /* 同上：探测失败不该把"已找到"这件事说成失败 */
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setDetecting(false);
    }
  }, [importClient, t]);

  // 弹窗打开（拿到明文）时才去探测本机状态：没建密钥的人不需要这次请求。
  useEffect(() => {
    if (!issued) return;
    let alive = true;
    keyApi.importLocalStatus()
      .then((s) => { if (alive) setImportStatus(s); })
      .catch(() => { if (alive) setImportStatus(null); });
    return () => { alive = false; };
  }, [issued]);

  /** 当前目标客户端的状态；null = 未探测 / 探测失败 */
  const importClientState = importStatus?.clients.find(
    (c) => c.client === importClient) ?? null;
  const importReady = !!importStatus?.enabled && !!importStatus.local_caller;
  /**
   * 能不能点导入。三种情况都行：
   *  · 深链可用 —— 首选，客户端在不在跑都无所谓；
   *  · 客户端没在跑 —— 直接写它的配置；
   *  · 在跑但定位得到 exe —— 后端会替用户关掉、写完再拉起来。
   * 注意深链可用时**不看 `installed`**：那指的是客户端的配置库存在与否，
   * 而深链是客户端自己去建/去合并，不需要我们先看见那个文件。
   */
  const canImport = importReady
    && (!!importClientState?.deeplink || !!importClientState?.importable
        || !!importClientState?.needs_close);
  /**
   * 不能导入时要**说清是哪一条不满足**：用户照着一句话就能用上，
   * 比一个灰按钮加一个问号有用得多。顺序 = 判定的优先级，别乱。
   */
  const importHint = !importStatus || !importStatus.enabled
    ? t('keys.importHintDisabled')
    : !importStatus.local_caller
      ? t('keys.importHintRemote')
      : importClientState?.deeplink
        ? t('keys.importHintDeeplink')
        : importClientState?.reason === 'not_installed'
          ? t('keys.importHintNotInstalled')
          : importClientState?.needs_close
            ? t('keys.importHintWillClose')
            : importClientState?.reason === 'client_running'
              ? t('keys.importHintRunning')
              : importClientState?.reason === 'cannot_detect'
                ? t('keys.importHintUnknown')
                : canImport
                  ? t('keys.importHintReady')
                  : t('keys.importHintUnknown');

  // 分组过滤。keys 的量级是「几十到几百」，一次渲染算两遍不值得上 useMemo
  // （那要多写一层依赖数组，还更容易漏依赖）。
  const normalKeys = keys.filter((k) => !k.packet_id);
  const packetKeys = keys.filter((k) => k.packet_id);
  const shownKeys = tab === 'packet' ? packetKeys : normalKeys;

  const header = (
    <>
      <PageHeader
        title={t('keys.title')}
        description={t('keys.description')}
        actions={
          <>
            {isAdmin && (
              <Button size="sm" className="rounded-full" onClick={openCreate}>
                <Plus />
                {t('keys.newKey')}
              </Button>
            )}
          </>
        }
      />
      {/* 二级导航（批次 4 ②：本页吸收了「红包」——红包产出的就是密钥，
          一份一个 key，只是多了「一次建一批、额度随机分配」这层封装）。
          放进 `header` 而不是两个 return 分支里各写一遍：下面的首屏守卫
          是**早返回**，漏一处就会出现「取不到密钥时 Tab 不见了」。 */}
      <PageSectionTabs />
    </>
  );

  // 首屏：一次都没取到。
  //
  // 为什么必须在这里拦住、而不是让下面的空状态兜住：**取不到密钥 ≠ 没有密钥。**
  // 原先的做法是取数失败只弹一条几秒后消失的提示，然后 `keys` 仍是 `[]`、`loading`
  // 已经变回 false —— 于是页面显示出「暂无密钥」和一个「新建密钥」按钮。密钥是**凭据**，
  // 用户看到这四个字的反应是「我的密钥被删了 / 我没建过」，而事实是**不知道有没有**；
  // 更糟的是他会顺着那个按钮重建 —— 于是建出重复密钥，**正好绕过 9-16 那次为
  // 「创建成功但刷新失败」加的防线**（那条防的是写之后，这条是打开页面时）。
  if (isInitialFailed || isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? <LoadError variant="page" onRetry={reload} /> : <KeysSkeleton />}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* 刷新时有请求失败：常驻提示，**不**顶掉已经显示出来的内容——那些数据仍然
          是对的，只是可能不是最新的。 */}
      {/* 上游清单没取到：新建密钥的「绑定上游」只剩默认项，静默建出来会绑错池。
          单独说清（比笼统的「部分失败」更能指导下一步）。 */}
      {'upstreams' in errors && (
        <LoadError message={t('keys.upstreamsFailed')} onRetry={reload} />
      )}
      {partialFailed && (
        <LoadError message={t('state.partialFailed')} onRetry={reload} />
      )}

      {/* 分组：红包一次生成一批、额度零碎，与手工建的混在一起很难看。
          数字直接标在 tab 上，不用切过去才知道另一边有多少个。
          ⚠️ 列表没取到时这两个数字要**一起收起来**：`keys` 为空时它会写成
          「普通密钥 · 0」，那是和「暂无 API 密钥」同一句谎话的另一半（任务记录页
          的「共 0 条」是同一处，PR #97 才补上）。判据同样用 `keysFailed`
          ——「这一份失败没」，上游列表挂掉时这两个 0 是如实的。 */}
      <Tabs value={tab} onValueChange={(v) => setTab(v as 'normal' | 'packet')}>
        <TabsList className="rounded-full">
          <TabsTrigger value="normal" className="rounded-full">
            {t('keys.tabNormal')}
            {!keysFailed && <> · {normalKeys.length}</>}
          </TabsTrigger>
          <TabsTrigger value="packet" className="rounded-full">
            {t('keys.tabPacket')}
            {!keysFailed && <> · {packetKeys.length}</>}
          </TabsTrigger>
        </TabsList>
      </Tabs>

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('metric.name')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colPrefix')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('accounts.colStatus')}</TableHead>
              {/* 版本列紧跟状态：它和状态一样是「这把密钥的属性」，位置与账号页的列序一致 */}
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.realm')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.expiry')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colIpModels')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colUsedTokens')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colLastUsed')}</TableHead>
              {isAdmin && <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">{t('accounts.colActions')}</TableHead>}
            </TableRow>
          </TableHeader>
          <TableBody>
            {shownKeys.map((k) => {
              const expired = !!k.expires_at && k.expires_at * 1000 < Date.now();
              // 两种额度任一超限都算「超额」——界面上必须与网关的拒绝口径**一致**，
              // 否则会出现「列表显示正常、调用却被 429」，用户会以为是网关坏了。
              const overQuota =
                (!!k.quota && k.used_tokens >= k.quota) ||
                (!!k.quota_credit && k.used_credit >= k.quota_credit) ||
                (!!k.daily_quota && (k.daily_used_tokens ?? 0) >= k.daily_quota);
              return (
                <TableRow key={k.id} className="border-b border-border/40">
                  <TableCell className="pl-4 text-sm font-medium">
                    <div>{k.name}</div>
                    {/* 只标「绑定了上游」的行：默认上游是绝大多数，每行都标等于没标，
                        有值才说明这把钥匙走的是另一个账号池（见 server/upstreamsvc.py） */}
                    {k.upstream_id ? (
                      <div className="mt-0.5">
                        <Badge variant="secondary" className="rounded-full text-[10px]">
                          {t('keys.upstreamTag', {name: upstreamName(k.upstream_id)})}
                        </Badge>
                      </div>
                    ) : null}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">{k.prefix}…</TableCell>
                  <TableCell>
                    {!k.enabled ? (
                      <Badge variant="secondary" className="rounded-full text-muted-foreground">{t('keys.badgeDisabled')}</Badge>
                    ) : expired ? (
                      <Badge variant="destructive" className="rounded-full">{t('keys.badgeExpired')}</Badge>
                    ) : overQuota ? (
                      <Badge variant="destructive" className="rounded-full">{t('keys.badgeOverQuota')}</Badge>
                    ) : (
                      <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">{t('keys.badgeOk')}</Badge>
                    )}
                  </TableCell>
                  {/* 版本必须排在有效期**前面**，与表头一致（issue #68：这两列的
                      单元格与表头顺序反了，界面上「版本」列显示的是有效期、「有效期」
                      列显示的是版本 —— 用户看到的就是这个错位）。 */}
                  <TableCell>
                    {k.realm === 'global' ? (
                      <Badge variant="secondary" className="rounded-full text-[10px]">{t('realm.global')}</Badge>
                    ) : k.realm === 'cn' ? (
                      <Badge variant="secondary" className="rounded-full text-[10px]">{t('realm.cn')}</Badge>
                    ) : (
                      // 存量密钥：本字段引入前创建的，两版都能调。单独标出来
                      // 而不是默认显示成国内版——那会让人以为它已被限定。
                      <span
                        className="text-[10px] text-amber-600 dark:text-amber-400"
                        title={t('keys.realmUnsetTitle')}
                      >
                        {t('keys.realmUnset')}
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {k.expires_at ? fmtDateTime(k.expires_at) : t('keys.neverExpires')}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {k.max_ips ? t('keys.ipLimit', {n: k.max_ips}) : t('keys.ipUnlimited')} /{' '}
                    {k.models?.length
                      ? t('keys.modelsCount', {count: k.models.length, n: k.models.length})
                      : t('keys.modelsAll')}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">
                    {(() => {
                      const ratio = k.quota ? k.used_tokens / k.quota : 0;
                      const tone = !k.quota
                        ? 'text-muted-foreground'
                        : ratio >= 1
                          ? 'text-red-600 dark:text-red-400 font-medium'
                          : ratio >= 0.8
                            ? 'text-amber-600 dark:text-amber-400 font-medium'
                            : 'text-foreground';
                      // 积分额度设了才显示积分那一行：没设的密钥（绝大多数）
                      // 保持原来的单行 token 展示，不让默认视图变吵。
                      const cRatio = k.quota_credit ? k.used_credit / k.quota_credit : 0;
                      const cTone = cRatio >= 1
                        ? 'text-red-600 dark:text-red-400 font-medium'
                        : cRatio >= 0.8
                          ? 'text-amber-600 dark:text-amber-400 font-medium'
                          : 'text-muted-foreground';
                      return (
                        <span className="flex flex-col">
                          <span className={tone}>
                            {fmtNumber(k.used_tokens)}
                            {k.quota ? ` / ${fmtNumber(k.quota)}` : ''}
                          </span>
                          {!!k.daily_quota && (() => {
                            const dUsed = k.daily_used_tokens ?? 0;
                            const dRatio = dUsed / k.daily_quota;
                            const dTone = dRatio >= 1
                              ? 'text-red-600 dark:text-red-400 font-medium'
                              : dRatio >= 0.8
                                ? 'text-amber-600 dark:text-amber-400 font-medium'
                                : 'text-muted-foreground';
                            return (
                              <span className={`text-[10px] ${dTone}`} title={t('keys.quotaDaily')}>
                                {t('keys.dailyUsed', {
                                  used: fmtNumber(dUsed),
                                  quota: fmtNumber(k.daily_quota),
                                })}
                              </span>
                            );
                          })()}
                          {!!k.quota_credit && (
                            <span
                              className={`text-[10px] ${cTone}`}
                              title={t('keys.quotaCredit')}
                            >
                              {t('keys.creditUsed', {
                                used: String(k.used_credit),
                                quota: String(k.quota_credit),
                              })}
                            </span>
                          )}
                        </span>
                      );
                    })()}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {k.last_used_at ? (
                      fmtDateTime(k.last_used_at)
                    ) : (
                      <span className="text-muted-foreground/70">{t('keys.neverUsed')}</span>
                    )}
                  </TableCell>
                  {isAdmin && (
                    <TableCell className="pr-4">
                      <div className="flex justify-end gap-1">
                        {/* 「复制密钥」：明文不随列表下发，点这里才向后端要
                            （会话管理员 + 审计）。存量密钥取不回来时后端会说明原因。 */}
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 rounded-md"
                          title={t('keys.copyKey')}
                          aria-label={t('keys.copyKey')}
                          disabled={revealing !== null}
                          onClick={() => void copyKey(k)}
                        >
                          {revealing === k.id
                            ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            : copiedId === k.id
                              ? <Check className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
                              : <Copy className="h-3.5 w-3.5" />}
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title={t('keys.edit')} onClick={() => openEdit(k)}>
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 rounded-md"
                          title={k.enabled ? t('keys.disable') : t('keys.enable')}
                          onClick={() => toggle(k)}
                        >
                          {k.enabled ? <Ban className="h-3.5 w-3.5" /> : <CircleCheck className="h-3.5 w-3.5" />}
                        </Button>
                        <ConfirmDialog
                          title={t('keys.resetUsageTitle')}
                          description={t('keys.resetUsageDesc', {name: k.name})}
                          onConfirm={async () => {
                            await keyApi.resetUsage(k.id);
                            notify.ok(t('keys.resetDone'));
                            reload();
                          }}
                          trigger={
                            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title={t('keys.resetUsage')}>
                              <RotateCcw className="h-3.5 w-3.5" />
                            </Button>
                          }
                        />
                        {/* 与账号页删除弹窗同一处理：密钥名可能很长，放 18px 标题里一折行就挤乱，
                            改放说明里，标题固定一行。窄屏用 min() 保住两侧 1rem 留白。 */}
                        <ConfirmDialog
                          title={t('keys.deleteTitle')}
                          description={t('keys.deleteDesc', {name: k.name})}
                          contentClassName="max-w-[min(380px,calc(100%-2rem))] sm:max-w-[380px]"
                          confirmText={t('keys.delete')}
                          destructive
                          onConfirm={async () => {
                            await keyApi.remove(k.id);
                            notify.ok(t('keys.deleted'));
                            reload();
                          }}
                          trigger={
                            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md text-red-500 hover:text-red-600" title={t('keys.delete')}>
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          }
                        />
                      </div>
                    </TableCell>
                  )}
                </TableRow>
              );
            })}
          </TableBody>
        </Table>

        {/* 「暂无密钥」是**一条都没有**的断言，取不到时不能说这句（见上面首屏守卫的
            说明）。`keysFailed` 覆盖「另一份取到了、这一份没有」的部分失败：那时首屏
            守卫不会拦（values 里还有 upstreams），但列表这一块同样不知道有没有。 */}
        {!shownKeys.length && !keysFailed && (
          <EmptyState
            icon={KeyRound}
            title={t('keys.emptyTitle')}
            description={t('keys.emptyDesc')}
            className="flex flex-col items-center justify-center py-16 text-center"
          >
            {isAdmin && (
              <Button className="mt-4 rounded-full" onClick={openCreate}>
                <Plus />
                {t('keys.newKey')}
              </Button>
            )}
          </EmptyState>
        )}
      </section>

      {/* 新建 / 编辑 */}
      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="max-w-[520px]">
          <DialogHeader>
            <DialogTitle>{editing ? t('keys.editTitle') : t('keys.createTitle')}</DialogTitle>
            <DialogDescription>
              {editing ? t('keys.editDesc') : t('keys.createDesc')}
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="max-h-[min(70vh,560px)]">
            <div className="space-y-4 px-6 pb-2">
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.name')}</Label>
                <Input value={form.name} onChange={(e) => setForm({...form, name: e.target.value})} placeholder={t('keys.namePlaceholder')} />
              </div>
              {!editing ? (
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('keys.expiresInDays')}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={form.expiresDays}
                    onChange={(e) => setForm({...form, expiresDays: e.target.value})}
                  />
                </div>
              ) : (
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">
                    {t('keys.expiry')}
                    {editing.expires_at
                      ? t('keys.expiryCurrent', {at: fmtDateTime(editing.expires_at)})
                      : t('keys.expiryNever')}
                  </Label>
                  <div className="flex items-center gap-2">
                    <Select
                      value={form.expiryMode}
                      onValueChange={(v) => setForm({...form, expiryMode: v as FormState['expiryMode']})}
                    >
                      <SelectTrigger className="flex-1" aria-label={t('keys.expiry')}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="keep">{t('keys.expiryKeep')}</SelectItem>
                        <SelectItem value="days">{t('keys.expiryFromNow')}</SelectItem>
                        <SelectItem value="never">{t('keys.expiryNeverOption')}</SelectItem>
                      </SelectContent>
                    </Select>
                    {form.expiryMode === 'days' && (
                      <Input
                        type="number"
                        min={1}
                        className="w-24"
                        placeholder={t('keys.daysPlaceholder')}
                        value={form.expiresDays}
                        onChange={(e) => setForm({...form, expiresDays: e.target.value})}
                      />
                    )}
                  </div>
                </div>
              )}
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('keys.maxIps')}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={form.maxIps}
                    onChange={(e) => setForm({...form, maxIps: e.target.value})}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('keys.quotaTokens')}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={form.quota}
                    onChange={(e) => setForm({...form, quota: e.target.value})}
                  />
                </div>
                <div className="space-y-1.5">
                  {/* 每日 token 限额：与总额度各自独立，按服务器本地日期每天 0 点恢复 */}
                  <Label className="text-[11px] text-muted-foreground">{t('keys.quotaDaily')}</Label>
                  <Input
                    type="number"
                    min={0}
                    step={1}
                    value={form.dailyQuota}
                    onChange={(e) => setForm({...form, dailyQuota: e.target.value})}
                  />
                  <p className="text-[11px] leading-4 text-muted-foreground">
                    {t('keys.quotaDailyHint')}
                  </p>
                </div>
                <div className="space-y-1.5">
                  {/* 积分额度：与 token 额度各自独立，任一超限即拒绝，两个都留 0 = 不限。
                      step=any 是必要的——上游按倍率扣费，值本身可能是小数（如 0.05），
                      限成整数会让小额预算根本没法设。 */}
                  <Label className="text-[11px] text-muted-foreground">{t('keys.quotaCredit')}</Label>
                  <Input
                    type="number"
                    min={0}
                    step="any"
                    value={form.quotaCredit}
                    onChange={(e) => setForm({...form, quotaCredit: e.target.value})}
                  />
                  <p className="text-[11px] leading-4 text-muted-foreground">
                    {t('keys.quotaCreditHint')}
                  </p>
                </div>
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.ipWhitelist')}</Label>
                <Textarea
                  rows={3}
                  value={form.ipAllowlist}
                  onChange={(e) => setForm({...form, ipAllowlist: e.target.value})}
                  placeholder={'10.0.0.0/8\n1.2.3.4'}
                />
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.realmLimit')}</Label>
                {/* 版本归属：**新建时不显示选择器**，直接跟随当前所在版本——
                    在哪个版本的界面里建，就是哪个版本的密钥。
                    用户明确要求过不要让他在这里选：选错了是**静默的**（只有真正
                    调用时才报「仅限某版本」），不如跟随页面、并把结果写清楚。
                    编辑时保留选择器：存量密钥（尤其"不限制"的老密钥）需要能改。 */}
                {editing ? (
                  <>
                    <Select
                      value={form.realm || '__all__'}
                      onValueChange={(v) => setForm({...form, realm: (v === '__all__' ? '' : v) as FormState['realm']})}
                    >
                      <SelectTrigger aria-label={t('keys.realm')}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="cn">{t('keys.realmCnOnly')}</SelectItem>
                        <SelectItem value="global">{t('keys.realmGlobalOnly')}</SelectItem>
                        {/* 只有存量密钥会停留在这个取值上 */}
                        <SelectItem value="__all__">{t('keys.realmAll')}</SelectItem>
                      </SelectContent>
                    </Select>
                    <p className="text-[11px] leading-4 text-muted-foreground">
                      {form.realm === ''
                        ? t('keys.realmHintUnlimited')
                        : form.realm === 'global'
                          ? t('keys.realmHintGlobal')
                          : t('keys.realmHintCn')}
                    </p>
                  </>
                ) : (
                  <>
                    <div className="flex flex-wrap items-center gap-1.5 rounded-lg bg-muted px-3 py-2">
                      <Badge
                        variant="secondary"
                        className={
                          'rounded-full text-[10px] ' +
                          (form.realm === ''
                            ? ''
                            : realm === 'global'
                              ? 'text-blue-600 dark:text-blue-400'
                              : 'text-emerald-600 dark:text-emerald-400')
                        }
                      >
                        {form.realm === ''
                          ? t('keys.realmUnset')
                          : realm === 'global'
                            ? t('realm.global')
                            : t('realm.cn')}
                      </Badge>
                      <span className="text-[11px] leading-4 text-muted-foreground">
                        {form.realm === '' ? t('keys.realmUnlimitedOn') : t('keys.realmFollowsPage')}
                      </span>
                    </div>
                    {/* 新建默认跟随当前版本（不给「国内版/国际版」二选一，选错了是静默的），
                        但要留一个**显式**的「不限制」出口（issue #191）：有人需要一把两版
                        都能调的密钥，而不必先建出来再编辑。 */}
                    <label
                      htmlFor="key-realm-unlimited"
                      className="mt-1.5 flex cursor-pointer items-center gap-1.5 text-[11px] text-muted-foreground"
                    >
                      <Switch
                        id="key-realm-unlimited"
                        checked={form.realm === ''}
                        disabled={!isAdmin}
                        onCheckedChange={(on) => setForm({...form, realm: on ? '' : realm})}
                      />
                      {t('keys.realmAll')}
                    </label>
                  </>
                )}
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.upstreamLabel')}</Label>
                {/* 多上游（分组隔离）：选了某个上游，这把密钥的请求就只走那个上游的
                    账号池。默认上游 = 不绑定，也就是升级前的行为。 */}
                <Select
                  value={form.upstream_id == null ? '__default__' : String(form.upstream_id)}
                  onValueChange={(v) =>
                    setForm({...form, upstream_id: v === '__default__' ? null : Number(v)})
                  }
                >
                  <SelectTrigger aria-label={t('keys.upstreamLabel')}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__default__">{t('keys.upstreamDefault')}</SelectItem>
                    {upstreams
                      .filter((u) => !u.is_default && u.id != null)
                      .map((u) => (
                        <SelectItem key={u.id} value={String(u.id)}>
                          {u.enabled ? u.name : `${u.name}${t('keys.upstreamDisabledSuffix')}`}
                        </SelectItem>
                      ))}
                  </SelectContent>
                </Select>
                <p className="text-[11px] leading-4 text-muted-foreground">
                  {t('keys.upstreamHint')}
                </p>
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.modelWhitelist')}</Label>
                {/* 手输保留（清单里暂时没有的模型仍要能填），旁边给「从清单选择」：
                    候选按版本分组，勾选即加入 —— 记不住名字、写错了等调用失败才发现的日子结束。 */}
                <ModelWhitelistInput
                  value={form.models}
                  realm={form.realm}
                  placeholder="glm-5.2, global:gpt-5.4"
                  onChange={(next) => {
                    setForm({...form, models: next});
                    // 改了内容就作废上次结论，避免显示过期的「都对」
                    setUnknownModels(null);
                  }}
                  // 还在打字（草稿未提交）时同样要作废：否则改到一半就切走视线，
                  // 看到的仍是上一版的结论（见组件里 onEdit 的说明）
                  onEdit={() => setUnknownModels(null)}
                  // 离开输入框时查一次：不在每次按键时打接口（那是逐字请求），
                  // 但要早于提交——提交时才发现就得重填一遍
                  onBlur={async (next) => {
                    // 用组件交回来的新值校验：此时 form.models 还没更新，读它会漏掉刚输入的模型。
                    const names = toLines(next);
                    if (!names.length) {
                      setUnknownModels([]);
                      return;
                    }
                    try {
                      const r = await keyApi.checkModels(names, form.realm || '');
                      setUnknownModels(r.checked ? r.unknown : null);
                    } catch {
                      setUnknownModels(null);   // 查不了就不显示，不编造
                    }
                  }}
                />
                {unknownModels !== null && unknownModels.length > 0 && (
                  <p ref={unknownRef}
                     className="text-[10px] leading-4 text-amber-600 dark:text-amber-400">
                    {t('keys.modelsUnknown', {names: unknownModels.join(t('common.listSeparator'))})}
                  </p>
                )}
                {/* 模型白名单与版本归属是**两道**检查，都要过：
                    版本归属由上面的选项控制（粗粒度，拦跨版本调用），
                    白名单在版本之内再收窄到具体几个模型（细粒度）。
                    写模型名时注意与所选版本一致——带 global: 前缀的是国际版模型。 */}
                <p className="text-[11px] leading-4 text-muted-foreground">
                  {/* 反引号包住的模型名由 RichText 渲染成等宽字体 */}
                  <RichText text={t('keys.modelPrefixNote')} />
                </p>
              </div>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" className="rounded-full" onClick={() => setFormOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button className="rounded-full" onClick={submit} disabled={busy}>
              {editing ? t('common.save') : t('keys.create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 一次性展示新密钥 */}
      <Dialog open={!!issued}
              onOpenChange={(v) => { if (!v) { setIssued(null); setExported(null); setImported(null); } }}>
        {/* 尺寸必须**跟着视口走**：这个弹窗里有密钥、有一整段 JSON、还有导入区，
            内容比一般弹窗高得多。之前只写了个 max-w，高度没上限，于是窗口一矮
            （或系统缩放一大）底部就被 `overflow-hidden` 直接裁掉，连「我已保存」
            都点不到——只能按 Esc 关掉。现在：整体不超视口，主体自己滚动。
            宽度：**必须连 `sm:` 一起写**。DialogContent 基类当年带了个 `sm:max-w-lg`
            （512px），而 Tailwind 把响应式规则排在样式表更靠后的位置，于是任何
            ≥640px 的屏幕上它都会盖过这里传的 `max-w-[…]` —— 这就是「怎么改都还是
            窄」的原因（本轮已在组件里去掉那条冗余规则，这里再写一遍 sm: 是防它被
            加回来）。920px 给"密钥 + JSON + 导入区"留足一行放得下的宽度，
            仍然用 calc(100vw-2rem) 兜住小屏，不会顶到屏幕边。 */}
        <DialogContent className="max-h-[calc(100dvh-2rem)] w-[min(920px,calc(100vw-2rem))] max-w-[min(920px,calc(100vw-2rem))] sm:max-w-[min(920px,calc(100vw-2rem))]">
          <DialogHeader>
            <DialogTitle>{t('keys.createdTitle')}</DialogTitle>
            <DialogDescription>{t('keys.createdDesc')}</DialogDescription>
          </DialogHeader>
          {/* 主体限高：给 header/footer 留出约 11rem，剩下的都归它。
              用 dvh 而不是 vh：移动端浏览器工具栏收起/展开时 vh 会跳。 */}
          <DialogBody className="max-h-[min(560px,calc(100dvh-14rem))]">
            <div className="space-y-3 px-6 pb-3">
              {/* min-w-0 必不可少：flex 项默认 min-width:auto，长密钥会把
                  复制按钮挤出去（移动端就点不到了） */}
              <div className="flex items-center gap-2 rounded-2xl bg-muted p-3">
                <code className="min-w-0 flex-1 break-all font-mono text-xs">{issued}</code>
                <CopyButton value={issued || ''} size="sm" showLabel label={t('keys.copyKey')} />
              </div>
              <div className="flex items-center gap-2">
                <span className="text-[11px] text-muted-foreground">Base URL</span>
                <code className="min-w-0 flex-1 break-all font-mono text-[11px]">{baseUrl}/v1</code>
                <CopyButton value={`${baseUrl}/v1`} title={t('keys.copyBaseUrl')} />
              </div>

            {/* 导出为客户端配置片段（cc-switch / ZCode）。
                只在**这一刻**可行：面板只存哈希，关掉弹窗后谁也拿不回明文。 */}
            <div className="space-y-2 rounded-2xl border p-3">
              {/* flex-wrap：窄宽度下让"导出"按钮掉到下一行，而不是把 select 挤扁 */}
              <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1.5">
                <span className="text-xs font-medium">{t('keys.exportTitle')}</span>
                <div className="flex items-center gap-2">
                  <Select value={exportTarget}
                          onValueChange={(v) => {
                            setExportTarget(v as typeof exportTarget);
                            // 换了目标就清掉旧结果：两个客户端的内容不一样，
                            // 留着上一份会造成"看的是 claude、复制的是 codex"。
                            setExported(null);
                            setImported(null);
                          }}>
                    <SelectTrigger className="h-7 w-[168px] rounded-full text-xs" aria-label={t('keys.exportTitle')}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="ccswitch-claude">{t('keys.exportCcClaude')}</SelectItem>
                      <SelectItem value="ccswitch-codex">{t('keys.exportCcCodex')}</SelectItem>
                      <SelectItem value="zcode">{t('keys.exportZcode')}</SelectItem>
                    </SelectContent>
                  </Select>
                  <Button size="sm" className="h-7 rounded-full text-xs"
                          onClick={doExport} disabled={exporting}>
                    {exporting ? t('common.loading') : t('keys.exportBtn')}
                  </Button>
                </div>
              </div>
              <p className="text-[11px] text-muted-foreground">{t('keys.exportHint')}</p>
              {exported ? (
                <div className="flex items-start gap-2 rounded-xl bg-muted p-2">
                  {/* wrap="off"：这一段是给机器读的配置，换行会把一行拆成两行，
                      复制出去就是坏的。宁可他横向滚动，也不要看上去"读得舒服"
                      但实际无法直接粘贴。高度跟着视口走，别把弹窗顶长。
                      field-sizing-fixed + min-w-0：Textarea 基类带 field-sizing-content
                      （按内容自适应宽度），叠上 wrap="off" 后固有宽度=最长一行；
                      而 DialogBody(Radix ScrollArea) 的内层是 display:table，按
                      max-content 取列宽 —— 于是 Codex 那种"TOML 塞进 JSON config
                      字段"的一整行长字符串会把整块内容撑出弹窗右缘（select 和
                      按钮被推到画面外）。field-sizing-fixed 让它回到按 CSS 宽度
                      （w-full）排布，min-w-0 拆掉 flex 项的 min-width:auto 兜底。 */}
                  <Textarea readOnly wrap="off" value={exported}
                            className="max-h-[min(240px,28dvh)] min-h-[120px] flex-1 min-w-0 field-sizing-fixed overflow-auto font-mono text-[11px] leading-relaxed" />
                  <CopyButton value={exported} size="sm" showLabel label={t('common.copy')} />
                </div>
              ) : null}

              {/* 一键导入到本机：与上面共用同一个目标选择器，区别只在去向——
                  导出把片段给用户，导入替他落盘（或交给客户端自己导入）。
                  门槛（开关 / 是否本机访问 / 客户端状态）全由后端判，
                  这里只如实展示状态，免得用户点完才知道不行。 */}
              <div className="space-y-2 border-t pt-2.5">
                <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1.5">
                  <span className="text-xs font-medium">{t('keys.importTitle')}</span>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                    <label htmlFor="key-import-set-current"
                           className="flex cursor-pointer items-center gap-1.5 text-[11px] text-muted-foreground">
                      <Switch id="key-import-set-current" checked={importSetCurrent}
                              disabled={!canImport}
                              onCheckedChange={setImportSetCurrent} />
                      {t('keys.importSetCurrent')}
                    </label>
                    {/* 定位不到客户端时才给「自动检测」：安装路径因机器而异
                        （绿色版可能解压在 E:\cc swich\），写死任何一个都不通用。 */}
                    {!importClientState?.exe ? (
                      <Button variant="outline" size="sm"
                              className="h-7 rounded-full text-xs"
                              onClick={doDetect}
                              disabled={detecting || !importReady}>
                        {detecting ? t('common.loading') : t('keys.detectBtn')}
                      </Button>
                    ) : null}
                    <Button size="sm" className="h-7 rounded-full text-xs"
                            onClick={doImport} disabled={!canImport || importing}>
                      {importing ? t('common.loading') : t('keys.importBtn')}
                    </Button>
                  </div>
                </div>
                <p className="text-[11px] text-muted-foreground">{importHint}</p>
                {/* 认到哪个可执行文件要说出来：用户才知道"关闭/重新打开"的是哪一个，
                    也才能发现面板是不是认错了东西。 */}
                {importClientState?.exe ? (
                  <p className="flex gap-1.5 text-[11px] text-muted-foreground">
                    <span className="shrink-0">{t('keys.detectLocated')}</span>
                    <code className="min-w-0 break-all font-mono">{importClientState.exe}</code>
                  </p>
                ) : null}
                {imported ? (
                  <div className="space-y-1 rounded-xl bg-muted p-2 text-[11px]">
                    <div className="flex items-center gap-1.5 font-medium">
                      <CircleCheck className="size-3.5 shrink-0" />
                      {imported.method === 'deeplink'
                        ? t('keys.importHandedOff')
                        : t('keys.importDone')}
                    </div>
                    {/* 深链没有落盘、也没有备份，就没有"写入/备份"两行可显示，
                        换成一句"接下来该在哪点确认"——否则用户以为已经生效了。 */}
                    {imported.method === 'deeplink' ? (
                      <p className="text-muted-foreground">{t('keys.importDeeplinkNote')}</p>
                    ) : (
                      <>
                        <div className="flex gap-1.5">
                          <span className="shrink-0 text-muted-foreground">{t('keys.importTarget')}</span>
                          <code className="min-w-0 break-all font-mono">{imported.target}</code>
                        </div>
                        <div className="flex gap-1.5">
                          <span className="shrink-0 text-muted-foreground">{t('keys.importBackup')}</span>
                          <code className="min-w-0 break-all font-mono">{imported.backup}</code>
                        </div>
                      </>
                    )}
                    {/* 客户端被我们关过就一定要说：不说的话用户只会发现
                        "我的 cc-switch 怎么没了/重启了"。拉不起来要说成警告——
                        配置是写进去了，但工具没替他打开。 */}
                    {imported.lifecycle?.stopped ? (
                      imported.lifecycle.restarted
                        ? <p className="text-muted-foreground">{t('keys.importClientRestarted')}</p>
                        : <p className="text-amber-600 dark:text-amber-400">
                            {t('keys.importClientClosedOnly')}
                          </p>
                    ) : null}
                  </div>
                ) : null}
              </div>
            </div>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button className="rounded-full" onClick={() => { setIssued(null); setExported(null); }}>
              {t('keys.savedIt')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 再复制一次：查看已创建密钥的明文（管理员 + 审计） */}
      <Dialog open={!!revealed} onOpenChange={(v) => { if (!v) setRevealed(null); }}>
        <DialogContent className="w-[min(560px,calc(100vw-2rem))] max-w-[min(560px,calc(100vw-2rem))] sm:max-w-[min(560px,calc(100vw-2rem))]">
          <DialogHeader>
            <DialogTitle>{t('keys.revealTitle')}</DialogTitle>
            <DialogDescription>{t('keys.revealDesc')}</DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-3 px-6 pb-3">
              {/* 名称单独一行：密钥可能叫「客服组-夜班」，与密钥本身一样长时会挤在一起 */}
              <p className="break-all text-sm font-medium">{revealed?.name}</p>
              {/* min-w-0 不能少：flex 项默认 min-width:auto，长密钥会把复制按钮挤出去 */}
              <div className="flex items-center gap-2 rounded-2xl bg-muted p-3">
                <code className="min-w-0 flex-1 break-all font-mono text-xs">{revealed?.key}</code>
                <CopyButton value={revealed?.key || ''} size="sm" showLabel label={t('keys.copyKey')} />
              </div>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button className="rounded-full" onClick={() => setRevealed(null)}>
              {t('keys.savedIt')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/**
 * 首屏骨架。结构与真实内容**逐块对应**（分组 tab / 表格块 + 若干行），而不是一坨
 * 居中的转圈：数据到位时版面不会整体跳一下。
 *
 * 页面每 60 秒心跳刷新一次，但只有「一份都没取到」才会走到这里
 * （见 use-async-data 的 isInitialLoading），所以不会一闪一闪。
 */
function KeysSkeleton() {
  return (
    <>
      <SkeletonBar className="h-9 w-52 rounded-full" />
      <section className="overflow-hidden rounded-[20px] bg-muted p-4">
        <SkeletonBar className="mb-4 h-4 w-28" />
        <div className="space-y-3">
          {Array.from({length: 5}, (_, i) => (
            <SkeletonBar key={i} className="h-11 w-full rounded-xl" />
          ))}
        </div>
      </section>
    </>
  );
}

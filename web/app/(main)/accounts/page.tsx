'use client';

import Link from 'next/link';
import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  Gift,
  Zap,
  KeyRound,
  Pause,
  Play,
  Trash2,
  Plus,
  Users,
  Power,
  CalendarCheck,
  Check,
  Coins,
  ChevronLeft,
  ChevronRight,
  Search,
  ArrowUpDown,
  RotateCcw,
  Sparkles,
  StickyNote,
  FolderInput,
  TriangleAlert,
} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useAsyncAll} from '@/lib/use-async-data';
import {notify} from '@/lib/toast';
import {accountApi, upstreamApi, upstreamsApi, errText} from '@/lib/api';
import type {
  Account,
  CreditExpiry,
  CreditsMeta,
  UpstreamEndpoint,
  UpstreamStatus,
} from '@/lib/types';
import {expiryBarPercent, expiryVisual, fmtAgo, fmtDateTime, fmtNumber, fmtRemain} from '@/lib/format';
import {
  AVAILABILITY_GROUPS,
  availabilityGroup,
  availabilityGroupLabelKey,
  availabilityLabelKey,
  availabilityOf,
  isDegraded,
  mergePoolStatus,
  rateLimitedModels,
} from '@/lib/account-status';
import {
  groupCounts,
  matchesQuery,
  paginate,
  selectAccounts,
  type GroupFilter,
  type SortKey,
} from '@/lib/account-list';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {PageSectionTabs} from '@/components/common/layout/PageSectionTabs';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {AddAccountDialog} from '@/components/common/accounts/AddAccountDialog';
import {UploadAccountsButton} from '@/components/common/accounts/UploadAccountsButton';
import {CreditCountdown} from '@/components/common/accounts/CreditCountdown';
import {AccountNoteDialog} from '@/components/common/accounts/AccountNoteDialog';
import {MoveAccountDialog} from '@/components/common/accounts/MoveAccountDialog';
import {UpstreamFormDialog} from '@/components/common/upstreams/UpstreamFormDialog';
import {AccountTaskDialog} from '@/components/common/accounts/AccountTaskDialog';
import {useAuth} from '@/lib/auth-context';
import {realmLabel, useRealm} from '@/lib/realm-context';
import {useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

/**
 * 一页显示多少个账号。
 *
 * 账号一多（几十个）整张表会把页面撑得很长，翻页比无限滚动更适合「我要找某一个号」
 * 的场景；页码条同时给出「共 N 个」，不会让人以为剩下的号不见了。
 * **只有超过一页时才渲染页码条**，小池子看不到多余的控件。
 */
const PAGE_SIZE = 20;

/**
 * 账号 → 4 组展示语义。
 *
 * 定义在模块级是为了**引用稳定**：它会进 `useMemo` 的依赖（`groupCounts` /
 * `selectAccounts`），每次渲染现写一个箭头函数会让那些 memo 每帧重算。
 * 映射表本身只有一份，在 lib/account-status.ts 里。
 */
const groupOf = (a: Account) => availabilityGroup(availabilityOf(a));

export default function AccountsPage() {
  const {realm, label: realmName} = useRealm();
  const t = useT();
  const {isAdmin} = useAuth();
  const [addOpen, setAddOpen] = useState(false);
  const [proxyRoutes, setProxyRoutes] = useState<string[] | null>(null);
  // 备注编辑（issue #67）：记的是**哪个账号**而不是布尔——弹窗要以该账号当前的
  // 备注为初值，否则会拿上一个账号的内容去保存。
  const [noteTarget, setNoteTarget] = useState<Account | null>(null);
  // 活动任务（单账号执行成长任务）的目标账号；null = 对话框关闭
  const [taskTarget, setTaskTarget] = useState<Account | null>(null);
  const [busyFile, setBusyFile] = useState<string | null>(null);
  /**
   * 列表的检索 / 状态筛选 / 排序 / 页码（P1-3）。
   *
   * 这四件事**全在客户端做**：一次请求就把整个分组的账号都拿回来了（`accountApi.list`
   * 不分页），所以改条件既不重新取数、也不会边打字边发请求——不需要防抖。
   * 日志页那边是**服务端**筛选（每个条件都是查询参数），所以那边的文本框必须防抖，
   * 两页的差别来自数据在哪一侧，不是风格不一致。
   */
  const [q, setQ] = useState('');
  const [statusGroup, setStatusGroup] = useState<GroupFilter>('all');
  const [sort, setSort] = useState<SortKey>('default');
  const [page, setPage] = useState(1);
  /**
   * 账号分组（多账号池）：一个分组 = 一套上游实例（账号池）。
   * null = 默认分组（升级前那套，行为逐字不变）；其余 = 上游记录的 id。
   */
  const [groupId, setGroupId] = useState<number | null>(null);
  const [groupDialogOpen, setGroupDialogOpen] = useState(false);
  const [moveTarget, setMoveTarget] = useState<Account | null>(null);
  const [checkinAllBusy, setCheckinAllBusy] = useState(false);
  /** 每个账号积分是实时查询还是命中缓存（含缓存已存在秒数） */
  const [creditsMeta, setCreditsMeta] = useState<Record<string, CreditsMeta>>({});
  /**
   * 查到的积分按 uid 单独存一份，渲染时再叠加到账号上。
   * 不能直接改写账号列表：积分请求与账号列表是并发的，
   * 积分常常先返回，那时列表还是空的，就地改写会落空。
   */
  const [liveCredits, setLiveCredits] = useState<Record<string, number>>({});
  /**
   * 进页面时那次「自动拉实时积分」失败了。
   *
   * 这一次失败**不是致命的**：上游 `/status` 里带着一份快照积分，界面会照常显示，
   * 并打上「上游快照」标签、用弱化的颜色。问题在于**用户看不出「为什么是快照」**
   * ——快照可能滞后数小时（上游按小时刷新），而用户会以为这就是当前余额。
   * 所以这里如实记下，并在积分列旁给一句说明（原实现是裸 `catch {}` 吞掉）。
   */
  const [creditsFailed, setCreditsFailed] = useState(false);

  /**
   * 账号池 = 这一页的**主数据**，单独一个 hook。
   *
   * 为什么不让它和上游状态同 hook：`isInitialLoading` / `isInitialFailed` 的判据是
   * 「**这一组**里有没有任何一个字段成功」。掺进上游状态之后，上游恰好返回 200 就能
   * 把「账号池还没到」判成「已就绪」——界面于是渲染出一个空列表，而账号其实还在路上。
   * 单独一个字段，这两个标记才精确等于「账号池取到了没有」。
   *
   * `groupId` 进 `deps` 而不是 `refreshDeps`：切分组换的是**数据集**，不是查询范围。
   * 留着上一组的账号渲染，分组按钮已经写着新分组了、列表却还是旧分组的人——那正是
   * 这个 hook 要消灭的谎话（同 `tasks` 的 `realm`）。
   */
  const {values, isInitialLoading, isInitialFailed, isRefreshing, reload} =
    useAsyncAll({pool: () => accountApi.list(groupId)}, [groupId]);

  /**
   * 当前分组的**上游状态**（冷却 / 成功计数 / 池内成员）。
   *
   * 单独一个 hook：它失败时账号列表仍然是好的，只是每个号的状态只能标成「未知」
   * （`mergePoolStatus` 的既定行为，见 lib/account-status 的注释——刻意不标「未加载」，
   * 那会把「连不上上游」误报成「账号文件坏了」）。所以它**不该**把整页判成失败，
   * 但也不能像原来那样静默：由 `upstreamFailed` 挂一条行内说明。
   *
   * 原实现这里是 `if (upRes.status === 'fulfilled')` —— 没有 else，失败完全无声。
   */
  const poolStatus = useAsyncAll({upstream: () => upstreamApi.status(groupId)}, [groupId]);

  /**
   * 分组清单**单独一个 hook**：它不随 `groupId` 变化（列的是所有分组）。
   *
   * 若与账号池同 hook，切分组时 `deps` 变化会把 `groups` 一并清空 —— 分组切换条
   * 会消失，而**如果这次请求恰好失败，切换条就再也回不来了**，用户被困在一个
   * 打不开的分组里。同理它也单独承担自己的失败，不把账号列表拖成错误态。
   */
  const groupList = useAsyncAll({groups: () => upstreamsApi.list()}, []);

  /** 账号池；`undefined` = 还没取到（首屏加载中，或它自己失败了） */
  const accounts: Account[] | undefined = values.pool?.accounts;
  const upstream: UpstreamStatus | null = poolStatus.values.upstream ?? null;
  const groups: UpstreamEndpoint[] = groupList.values.groups?.items || [];
  /**
   * 当前分组的元信息：manageable = 该分组配了本地账号目录（可加 / 移 / 删账号）。
   * 与账号池同一次响应，所以直接从它派生，不再单独存一份（少一处会不同步的状态）。
   */
  const groupInfo = values.pool
    ? {name: values.pool.upstream?.name || '', manageable: values.pool.manageable !== false}
    : null;

  /** 上游状态没取到 → 每个账号的状态只能标「未知」，得说一句为什么 */
  const upstreamFailed = 'upstream' in poolStatus.errors;
  /** 分组清单没取到 → 切换条上只剩「默认分组」，看着像没有别的分组 */
  const groupsFailed = 'groups' in groupList.errors;

  /**
   * 刷新：三份数据各刷各的。
   *
   * 返回值合并成「是否全部成功」——本页有若干「先写再刷新」的动作（签到、删账号、
   * 移分组…），它们要能区分「写失败」与「写成功但列表没刷上」，后者绝不能报成前者。
   */
  const reloadAll = useCallback(async () => {
    const [a, b, c] = await Promise.all([reload(), poolStatus.reload(), groupList.reload()]);
    return a && b && c;
  }, [reload, poolStatus.reload, groupList.reload]);

  // 打开页面时自动拉一次实时积分：上游 /status 的 credits 可能滞后数小时，
  // 首次进入应展示真实余额。服务端有 TTL 缓存，重复进入不会频繁请求。
  useEffect(() => {
    let alive = true;
    // 换分组就换了一套账号，上一组的实时积分/缓存元信息不再适用，先清掉；
    // 否则同一个 uid 在两组的账目会串味。
    setLiveCredits({});
    setCreditsMeta({});
    setCreditsFailed(false);
    (async () => {
      try {
        // force=false：60 秒内重复打开页面直接命中服务端缓存，
        // 不再每次都全量请求腾讯；命中时界面会明确标注「缓存」
        const r = await accountApi.refreshCredits(false, groupId);
        if (!alive) return;
        setLiveCredits(
          Object.fromEntries(
            Object.entries(r.credits).filter(([, v]) => typeof v === 'number') as [string, number][],
          ),
        );
        setCreditsMeta(r.meta ?? {});
      } catch {
        // 这次没取到**不是**致命的：上游 /status 自带快照值，界面会照常显示并标注
        // 「上游快照」。但不能像原来那样裸吞——用户会以为快照就是当前余额。
        // 记下来，由积分列旁的一句说明如实交代（见 creditsFailed）。
        if (alive) setCreditsFailed(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [groupId]);

  // 上游状态（冷却 / 成功计数等）会随时间变化，页面停留时定时刷新，
  // 否则会一直显示打开页面那一刻的旧数据。
  //
  // 走 reload（刷新模式）：只把新数据换上去，**不**重走首屏流程——否则骨架会每
  // 30 秒闪一次。分组清单不随心跳刷新（它变的是「有哪些分组」，不是运行时状态）。
  useHeartbeat(reload, 30000);

  /** 刷新所有账号的实时积分（直接向腾讯查询，非上游缓存值） */
  const [creditsBusy, setCreditsBusy] = useState(false);
  const refreshCredits = useCallback(async () => {
    setCreditsBusy(true);
    try {
      const r = await accountApi.refreshCredits(true, groupId);
      setLiveCredits(
        Object.fromEntries(
          Object.entries(r.credits).filter(([, v]) => typeof v === 'number') as [string, number][],
        ),
      );
      setCreditsMeta(r.meta ?? {});
      if (r.failed.length === 0) {
        notify.ok(t('accounts.creditsRefreshed'), t('accounts.creditsRefreshedDetail', {ok: r.succeeded, total: r.total}));
      } else {
        notify.warn(t('accounts.creditsPartial'), t('accounts.creditsPartialDetail', {ok: r.succeeded, total: r.total}));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setCreditsBusy(false);
    }
  }, [groupId]);

  /** 批量签到：逐账号记录结果 */
  const checkinAll = useCallback(async () => {
    setCheckinAllBusy(true);
    try {
      const r = await accountApi.checkinAll(groupId);
      const failed = r.total - r.succeeded;
      // 两类账号都不计入分母，各自补一句说明，否则用户会以为漏签了：
      //   skipped — 国际版没有签到体系，点多少次都是「已跳过」
      //   already — 今天已经签过，本次没再打上游（这正是「不重复签到」的效果）
      const notes = [
        r.skipped > 0 ? t('accounts.checkinSkippedNote', {skipped: r.skipped}) : '',
        r.already > 0 ? t('accounts.checkinAlreadyNote', {n: r.already}) : '',
      ].filter(Boolean).join(' ');
      const note = notes ? ' ' + notes : '';
      if (r.total === 0) {
        // 「今天都已签到」和「没有可签到的账号」是两回事：前者是正常且理想的状态
        // （说明当天不用再操作），后者说明池子里没有国内版账号。混成一句「没有
        // 可签到的账号」会让前者看起来像出了问题。
        if (r.already > 0) {
          notify.ok(
            t('accounts.checkinNothingPending'),
            t('accounts.checkinNothingPendingDetail', {n: r.already}) + note,
          );
        } else {
          notify.info(t('accounts.noCheckinTargets'), notes.trim() || undefined);
        }
      } else if (failed === 0) {
        notify.ok(
          t('accounts.checkinAllDone'),
          t('accounts.checkinAllDoneDetail', {ok: r.succeeded, total: r.total}) + note,
        );
      } else {
        notify.warn(
          t('accounts.checkinPartial', {failed}),
          t('accounts.checkinPartialDetail', {ok: r.succeeded, total: r.total}) + note,
        );
      }
      await reloadAll();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setCheckinAllBusy(false);
    }
  }, [reloadAll, groupId]);

  /**
   * 将本地 auths 文件与上游账号池状态按 uid 合并。
   *
   * 合并与分档规则都在 `lib/account-status` 里——首页的健康快照要用**同一套**
   * 规则（此前它只看 Token 有效期，于是同一个账号首页说「在线」、这里说
   * 「未加载」，用户看到自相矛盾的面板）。
   */
  const merged = useMemo(() => mergePoolStatus(accounts ?? [], upstream), [accounts, upstream]);

  /**
   * 当前分组是不是和默认分组**共用同一套上游实例**（地址相同）。
   *
   * 用户反馈（issue #94）：把账号「移动到分组」之后，切到那一组看，账号一律显示
   * 「未加载」——在那组里新扫码加的号也一样。这不是加载失败，而是这种分组的必然
   * 结果：面板把账号文件放进了这一组的目录，而**上游只读默认分组的账号目录**，
   * 所以那些号根本不在池子里：转发选不中、签到也不行。
   *
   * 判定只看地址：地址相同 = 同一套实例（api_key 留空时后端本就沿用默认那把，
   * 见 upstreamsvc.forward_api_key）。要真正分开账号池得在服务器上再部署一套
   * workbuddy2api —— 面板不替用户改部署，但必须**在这里把原因说清楚**，
   * 否则「未加载」看起来像坏了，用户会一直重扫码、重试。
   */
  const sharedInstance = useMemo(() => {
    if (groupId == null) return false;             // 默认分组自己不算
    const cur = groups.find((g) => g.id === groupId);
    const def = groups.find((g) => g.is_default);
    if (!cur || !def) return false;
    const addr = String(cur.base_url || '').trim();
    return addr !== '' && addr === String(def.base_url || '').trim();
  }, [groupId, groups]);

  /**
   * 按当前版本过滤。
   *
   * 上游是单实例双版本共存，账号池里两种账号都有；不区分的话切到国际版
   * 仍会看到国内版账号（反之亦然），「切换」就没有意义了。
   * 存量账号没有 realm 字段，后端按域名回退（多为 cn），与升级前一致。
   */
  const visible = useMemo(
    () => merged.filter((a) => (a.realm ?? 'cn') === realm),
    [merged, realm],
  );

  /**
   * 各状态组（外加「全部」）的条数，给筛选按钮上的数字。
   *
   * 只受**搜索**影响，**不**受状态筛选影响：否则选中「需处理」之后其余按钮全变成 0，
   * 看不出池子本来的分布，也就失去了「一眼扫过」的意义。
   */
  const counts = useMemo(
    () => groupCounts(visible.filter((a) => matchesQuery(a, q)), groupOf),
    [visible, q],
  );

  /**
   * 搜索 + 状态筛选 + 排序之后的结果（还没分页）。
   *
   * `creditOf` 必须带上刚查到的**实时**积分：排序用的得是用户看到的那份数字，
   * 否则会出现「按积分排序、显示出来的数值顺序却是乱的」——而且不报错。
   */
  const filtered = useMemo(
    () =>
      selectAccounts(visible, {q, group: statusGroup, sort}, {
        groupOf,
        creditOf: (a) => liveCredits[a.uid] ?? a.credits ?? null,
      }),
    [visible, q, statusGroup, sort, liveCredits],
  );

  const paged = useMemo(() => paginate(filtered, page, PAGE_SIZE), [filtered, page]);

  /**
   * 页码越界时在**渲染期**夹回来（删掉一个账号、或改筛选让条数变少就会越界）。
   *
   * 用渲染期纠正而不是 `useEffect`：后者会先用旧页码提交一帧空表，用户看到表格
   * 闪一下「没有数据」再恢复。同日志页切版本的处理方式。
   */
  if (paged.page !== page) setPage(paged.page);

  /** 三种「看不到账号」的原因，必须分开说——它们要用户做的事完全不同 */
  const noAccountsAtAll = merged.length === 0;
  const noAccountsInRealm = !noAccountsAtAll && visible.length === 0;
  const noMatch = visible.length > 0 && filtered.length === 0;

  /** 清掉列表自己的筛选（不动分组，也不动版本） */
  function clearListFilters() {
    setQ('');
    setStatusGroup('all');
    setSort('default');
    setPage(1);
  }

  /**
   * 今天还没签到的账号数 —— 「全部签到」按钮据此显示与禁用。
   *
   * 判据与签到按钮的**可见条件**保持一致（国内版、且未被面板停用）：面板停用的
   * 账号已完全退出账号池、签到也跟着停了，把它算进「待签到」会让按钮牌子上
   * 永远留着一个点不掉、也签不了的数字。
   */
  const pendingCheckin = useMemo(
    () =>
      visible.filter(
        (a) =>
          (a.realm ?? 'cn') === 'cn' &&
          a.disabled_by_panel !== true &&
          a.checkin_today == null,
      ).length,
    [visible],
  );

  /** 执行单账号操作（签到 / 测活 / 刷新 / 删除），成功后同步底栏计数 */
  async function run(
    file: string,
    fn: () => Promise<unknown>,
    okMsg: string,
    preferOkMsg = false,
  ) {
    setBusyFile(file);
    try {
      const res = (await fn()) as {
        message?: string;
        ok?: boolean;
        credits?: number | null;
        expiries?: CreditExpiry[];
      };
      const ok = res.ok !== false;
      // 签到 / 测活会带回**刚问到**的实时积分与到期时间。
      //
      // 写进 `liveCredits`（渲染时优先取的那一份）而不是账号列表里的 `credits`：
      // 后者是上游 `/status` 的快照位，`renderCredits` 只在没有实时值时才用它。
      // 原来写进快照位，等于写了一个**看不见的值**（实时值一存在就被盖住），
      // 而且两个值的语义本来就不同——一个是我刚问到的，一个是上游按小时刷的。
      const uid = accounts?.find((a) => a.file === file)?.uid;
      if (typeof res.credits === 'number' && uid) {
        setLiveCredits((prev) => ({...prev, [uid]: res.credits as number}));
      }
      if (uid && res.expiries) {
        setCreditsMeta((prev) => ({
          ...prev,
          [uid]: {...prev[uid], cached: false, cache_age: null, expiries: res.expiries},
        }));
      }
      (ok ? notify.ok : notify.err)(ok && preferOkMsg ? okMsg : (res.message || okMsg));
      await reloadAll();
      window.dispatchEvent(new Event('workbuddy-manager:accounts-changed'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusyFile(null);
    }
  }

  /**
   * 删除当前分组（只删面板记录，账号目录与文件不动）。
   *
   * 两道闸在后端：分组里还有账号、或有密钥绑着它 → 409 且说明数量，这里原样弹出。
   */
  async function deleteGroup() {
    if (groupId == null) return;
    try {
      await upstreamsApi.remove(groupId);
      notify.ok(t('accounts.groupDeleted'));
      // 切回默认分组：`groupId` 进的是主数据的 `deps`，这一改就会自动清空重取，
      // 不需要再手工调一次（手工调反而会用**旧的** groupId 打一次废请求）。
      setGroupId(null);
      // 被删掉的那个分组得从切换条上消失——分组清单是另一个 hook，单独刷。
      await groupList.reload();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  /** 仅重启上游容器，不涉及单个账号，因此单独处理 */
  const [restarting, setRestarting] = useState(false);
  async function restartUpstream() {
    setRestarting(true);
    try {
      const res = await accountApi.restart(groupId);
      (res.ok ? notify.ok : notify.err)(res.message || t('accounts.restarted'));
      await reloadAll();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setRestarting(false);
    }
  }

  /** 账号状态徽章（表格与移动端卡片共用）。
   *
   *  分档顺序与文案都取自 `lib/account-status`——首页的健康快照用**同一套**
   *  判定，两页因此不会对同一个账号给出不同说法（此前首页只看 Token 有效期，
   *  于是同一个账号首页说「在线」、这里说「未加载」）。
   *  这里只负责「怎么画」，不再自行判断。 */
  function renderStatus(a: Account) {
    const tier = availabilityOf(a);
    const label = t(availabilityLabelKey(tier, a));

    if (tier === 'disabled') {
      const reason = String(a.disabled_reason || '');
      return (
        <Badge
          variant="destructive"
          className="rounded-full"
          title={reason ? t('accounts.disabledReason', {reason}) : undefined}
        >
          {label}
        </Badge>
      );
    }
    if (tier === 'disabledByPanel') {
      // 面板主动停用（改名）：中性灰而不是告警红——这是用户自己的选择，不是故障。
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-muted-foreground"
          title={t('accounts.badgeDisabledByPanelTitle')}
        >
          {label}
        </Badge>
      );
    }
    if (tier === 'manualDisabled') {
      // 上游状态位停用（issue #45）：同样中性灰，但提示文案不同——这条是
      // 「还在池里、签到与保活照常」，与上一条「完全退出账号池」差别很大，
      // 用户看不出机制差别，必须写清楚。停用原因也一并带上。
      const reason = String(a.manual_reason || '');
      const nl = String.fromCharCode(10);
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-muted-foreground"
          title={t('accounts.badgeManualDisabledTitle') + (reason ? nl + reason : '')}
        >
          {label}
        </Badge>
      );
    }
    if (tier === 'expired') {
      return <Badge variant="destructive" className="rounded-full">{label}</Badge>;
    }
    // 上游状态这次没取到 —— 冷却 / 禁用 / 在不在池里**全都无从判断**。
    // 本地能确定的（令牌是否过期）已在分档里先判过，不受影响。
    if (tier === 'unknown') {
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-muted-foreground"
          title={t('accounts.badgeUnknownTitle')}
        >
          {label}
        </Badge>
      );
    }
    if (tier === 'cooling') {
      // 带上「还要等多久」：只写「冷却中」的话用户不知道是几秒还是几小时，
      // 只能反复刷新碰运气。剩余时间是上游状态机给的权威值。
      const secs = a.cool_remaining_sec;
      const left = typeof secs === 'number' && secs > 0 ? fmtRemain(secs) : '';
      // 按**原因**分组展示：上游两类条目的含义完全不同，混在一起会误导——
      //   6004  → 这个模型被限流了（等一会儿就好）
      //   11102 → 这个账号根本没有这个模型（等多久都不会好，该换模型）
      // 上游的 reason 前缀就是判据（"6004 model rate limit" / "11102 model not available"）。
      const limited: string[] = [];
      const missing: string[] = [];
      // 枚举分隔符跟随语言：中文用「、」，拉丁语系用「, 」
      const sep = t('common.listSeparator');
      for (const m of a.rate_limited_models ?? []) {
        const r = m.reason ?? '';
        (r.startsWith('11102') ? missing : limited).push(m.model);
      }
      const tip = [
        // 降权要**排在剩余时间前面**：它是「为什么在冷却」的答案，而剩余时间
        // 只是「还要等多久」。先给原因，用户才知道该等还是该去查这个号。
        isDegraded(a)
          ? t('accounts.degradedReason', {n: a.consecutive_fails ?? 0})
          : '',
        left ? t('accounts.etaRecovery', {left}) : '',
        limited.length ? t('accounts.limitedModels', {models: limited.join(sep)}) : '',
        missing.length
          ? t('accounts.missingModels', {models: missing.join(sep)})
          : '',
      ]
        .filter(Boolean)
        .join('\n');
      const total = limited.length + missing.length;
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-amber-600 dark:text-amber-400"
          title={tip || undefined}
        >
          {label}{left ? ` · ${left}` : ''}
          {total > 0 && <span className="ml-1 opacity-70">{t('accounts.modelsCount', {count: total, n: total})}</span>}
        </Badge>
      );
    }
    // 「一直在失败，但状态看着正常」——上游对**未命中它那几条规则**的 4xx
    // （例如被 WAF 拦下的 403）只「换号不罚」：不冷却、不熔断、不禁用
    // （其 applyErrorPolicy 的 default 分支，为防雪崩而刻意如此）。于是这种
    // 账号在面板上一直显示「正常」，实际每次请求都失败，可持续几小时
    // （issue #14 报告的第二点）。我们能做的是**把它标出来** —— 否则用户
    // 只看到「状态正常」却一直在报错，完全无从下手。
    if (tier === 'neverSucceeded') {
      const errs = typeof a.err_total === 'number' ? a.err_total : 0;
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-rose-600 dark:text-rose-400"
          title={t('accounts.badgeNeverSucceededTitle', {errs})}
        >
          {label}
        </Badge>
      );
    }
    // 上游没有加载这个账号 —— 它**不在账号池里，永远选不中**。
    //
    // 为什么必须单独标：面板读的是 auths/ 目录下的文件，上游读的才是池。
    // 上游 `LoadDir` 对解析失败的 auth 文件静默跳过（如 accessToken 为空），
    // 那种文件永远进不了池。此前我们会兜底显示「● 在线」——于是出现
    // 「面板全绿、调用却报没有健康账号」的矛盾，用户完全无从下手（实测反馈）。
    //
    // 也可能只是「刚添加、上游还没重载」，所以文案不写成故障，
    // 而是说明它尚未进入账号池、并给出可做的动作。
    if (tier === 'notLoaded') {
      const why = String(a.invalid_reason || '');
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-rose-600 dark:text-rose-400"
          title={why ? t('accounts.badgeNotLoadedWhy', {why}) : t('accounts.badgeNotLoadedTitle')}
        >
          {label}
        </Badge>
      );
    }
    return (
      <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
        {label}
      </Badge>
    );
  }

  /** 模型级限流标记（issue #43）。
   *
   *  为什么单独做一个徽章、而不是并进状态列：**账号本身是在线的**，只是某个
   *  模型暂时被腾讯限流（6004）。用户的实际观察是「换个模型就能继续用」，
   *  但状态列显示「在线」，于是「这个模型用不了」这件事在面板上完全不可见
   *  —— 有人只能进容器翻 state.json 才知道。
   *
   *  所以它与状态徽章**并列**展示（不是替代）：账号确实可用，只是有模型受限。
   *  徽章里列具体有哪些模型受限，悬停再给出每个模型的恢复时间——上游台账里
   *  带权威的重置时刻。 */
  function renderModelLimit(a: Account) {
    const limited = rateLimitedModels(a);
    if (!limited.length) return null;
    const sep = t('common.listSeparator');
    const lines = limited.map((m) => {
      const until = a.rate_limited_models?.find((x) => x.model === m.model)?.until;
      const at = until ? new Date(until).toLocaleTimeString() : '';
      // 11102（该账号没有这个模型）与 6004（限流）含义不同：前者等多久都不会好，
      // 该换模型；后者等一会儿就恢复。上游的 reason 前缀就是判据。
      const why = m.reason.startsWith('11102')
        ? t('accounts.modelMissing')
        : at
          ? t('accounts.modelLimitUntil', {at})
          : t('accounts.modelLimited');
      return `${m.model} · ${why}`;
    });
    // 徽章里**直接给出模型名**（用户反馈：只知道「有 2 个模型受限」不够用，得知道
    // 是哪些，才能换模型或者告诉调用方避开它们）。名字可能很长（`global:` 前缀的
    // 型号尤甚），所以最多列两个，其余用「+N」带过；完整清单与各自恢复时间仍在
    // 悬停里——那里才是逐条说明的地方。
    const MAX_INLINE = 2;
    const names = limited.slice(0, MAX_INLINE).map((m) => m.model).join(sep);
    const shown = limited.length > MAX_INLINE
      ? `${names} +${limited.length - MAX_INLINE}`
      : names;
    return (
      <Badge
        variant="secondary"
        className="rounded-full text-amber-600 dark:text-amber-400"
        title={[t('accounts.modelLimitTitle'), ...lines].join(String.fromCharCode(10))}
      >
        {t('accounts.modelLimitBadge', {models: shown})}
      </Badge>
    );
  }

  /** 积分余额 + 数据来源标注。
   *  明确区分「实时」「缓存 x 秒前」与「上游快照」三种来源，避免把滞后的数字
   *  当成刚查到的。
   *
   *  为什么要单独标出「上游快照」（issue #56）：实时值取不到时（查询失败，或
   *  只读账号早先根本调不到该接口），界面会回退到上游 `/status` 的快照值 ——
   *  那是**上游上次调度这个账号时记下的**，可能滞后数小时，也可能仍是 0。
   *  实测只读账号看到两个账号显示 0（管理员视角是 6420 / 9353），而 0 会被当成
   *  「余额耗尽」用红色渲染，看起来像账号出了故障。
   *
   *  所以快照值：① 打「上游快照」标签；② **不用红色** ——「快照说 0」不等于
   *  「确实没积分」，不该报警。
   */
  function renderCredits(a: Account) {
    // 优先用刚查到的实时值，其次上游 /status 的快照值
    const live = liveCredits[a.uid];
    const value = live ?? a.credits;
    if (value === null || value === undefined) {
      return (
        <span
          className="text-xs text-muted-foreground"
          title={t('accounts.creditsNoneTitle')}
        >
          —
        </span>
      );
    }
    const meta = creditsMeta[a.uid];
    const fromSnapshot = live === undefined;
    const tone = fromSnapshot
      ? 'text-muted-foreground'
      : value <= 0
        ? 'text-red-600 dark:text-red-400'
        : value < 200
          ? 'text-amber-600 dark:text-amber-400'
          : 'text-foreground';
    return (
      <span className="inline-flex items-center gap-1.5">
        <span className={`text-xs font-medium tabular-nums ${tone}`} title={t('accounts.creditsTitle')}>
          {fmtNumber(value)}
        </span>
        {fromSnapshot ? (
          <span
            className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] leading-3 text-muted-foreground"
            title={t('accounts.snapshotTitle')}
          >
            {t('accounts.snapshot')}
          </span>
        ) : (
          meta &&
          (meta.cached ? (
            <span
              className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] leading-3 text-amber-600 dark:text-amber-400"
              title={t('accounts.cacheTitle')}
            >
              {meta.cache_age != null ? t('accounts.cacheAge', {n: meta.cache_age}) : t('accounts.cache')}
            </span>
          ) : (
            <span
              className="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] leading-3 text-emerald-600 dark:text-emerald-400"
              title={t('accounts.liveTitle')}
            >
              {t('accounts.live')}
            </span>
          ))
        )}
        <CreditCountdown expiries={fromSnapshot ? undefined : meta?.expiries} />
      </span>
    );
  }

  /** Token 有效期进度条 */
  useEffect(() => {
    let active = true;
    accountApi.proxies().then((data) => {
      if (active) setProxyRoutes(data.routes);
    }).catch((error) => { if (active) notify.err(errText(error)); });
    return () => { active = false; };
  }, []);

  /**
   * 要不要显示「线路」这一列。
   *
   * 线路表读自**上游配置**的 `proxies`：没配过时那个下拉里只有「直连」一项 —— 对绝大
   * 多数部署那只是一列噪声，还会让人以为能选却选不动（维护者复核补）。所以两种情况
   * 才显示：有线路可选，或者确实有账号绑着线路（配置被移除后仍要看得见、改得回来）。
   */
  const hasProxyUi = (proxyRoutes ?? []).length > 0
    || (accounts ?? []).some((a) => a.proxy);   // accounts 可能还没取到

  function renderProxy(a: Account) {
    if (!isAdmin) return <span className="text-xs">{a.proxy || t('accounts.proxyDirect')}</span>;
    return (
      <Select value={a.proxy ? `route:${a.proxy}` : 'default'} disabled={proxyRoutes === null || busyFile === a.file}
              onValueChange={async (value) => {
                setBusyFile(a.file);
                try {
                  await accountApi.setProxy(a.file, value === 'default' ? '' : value.slice(6), groupId);
                  notify.ok(t('accounts.proxySaved'));
                  await reloadAll();
                } catch (error) { notify.err(errText(error)); }
                finally { setBusyFile(null); }
              }}>
        <SelectTrigger className="h-8 min-w-[100px] max-w-[155px] text-xs" aria-label={t('accounts.proxyLine')}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="default">{t('accounts.proxyDirect')}</SelectItem>
          {a.proxy && !(proxyRoutes ?? []).includes(a.proxy) &&
            <SelectItem value={`route:${a.proxy}`}>{a.proxy} ({t('accounts.proxyMissing')})</SelectItem>}
          {(proxyRoutes ?? []).map((route) => <SelectItem key={route} value={`route:${route}`}>{route}</SelectItem>)}
        </SelectContent>
      </Select>
    );
  }

  function renderExpiry(a: Account) {
    const pct = expiryBarPercent(a.remain_seconds, a.ttl_seconds);
    const vis = expiryVisual(a.remain_seconds);
    // 「有效期」是剩余时间，刷新会把它重新拉满，所以单看天数分不清
    // 「刚被保活续期」和「从没刷新过、还用着当初扫码的长令牌」。
    // 补一行签发时间（≈ 最近一次刷新）才能区分——后者是保活没覆盖到的隐患账号。
    const issued = a.issued_at ?? null;
    return (
      <div className="w-[150px]">
        <div className={`mb-1 text-[11px] font-medium tabular-nums ${vis.textClass}`}>
          {fmtRemain(a.remain_seconds)}
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-border">
          <div className="h-full rounded-full transition-all" style={{width: `${pct}%`, background: vis.barColor}} />
        </div>
        {issued != null && (
          <div
            className="mt-1 text-[10px] text-muted-foreground/70"
            title={t('accounts.issuedAt', {at: fmtDateTime(issued)})}
          >
            {t('accounts.lastRenewed', {ago: fmtAgo(issued)})}
          </div>
        )}
      </div>
    );
  }

  /** 单账号操作按钮组 */
  function renderActions(a: Account) {
    const busy = busyFile === a.file;
    // 国际版没有签到体系（上游对 global 账号直接过滤，不发请求）。
    // 这一行的「签到」按钮对国际版账号只会返回「已跳过」，属误导，故不显示。
    const canCheckin = (a.realm ?? 'cn') === 'cn';
    // 今天已经签过：按钮转为「已签到」并禁用。
    // 为什么要在界面上拦：腾讯对重复签到回 10001（幂等成功），所以点下去「看着
    // 也能成功」——但每次都真打一次 RPC，还会在签到记录里堆一串「今日已签到」，
    // 把真正的失败记录挤下去。后端另有一道拦截（不发请求），这里是让用户
    // 一眼看出「不用再点了」，而不是点了之后才知道。
    const checkinDone = a.checkin_today != null;
    // 本面板改名停用（issue #21）：账号已完全退出账号池，签到等任务也随之停掉，
    // 所以隐藏其它操作（它们对这个号已无意义）。
    const paused = a.disabled_by_panel === true;
    // 上游状态位停用（issue #45）：**任务照常执行**是这条路的重点，所以签到 /
    // 测试 / 刷新 Token 这些操作仍然可用、也必须仍然可见——否则用户会以为
    // 「停用把签到也停了」，正好把这条路与改名那条的区别抹掉了。
    const viaBit = a.manual_disabled === true;
    // 上游**系统自动禁用**（12153 连败 / 11140 被封）：也不在选号池里
    // （`healthy()` 直接 false），但 `manual_disabled` 是 false。
    const autoDisabled = a.disabled === true;
    // `off` = **完全退出账号池**：隐藏签到 / 测试 / 刷新这些操作（对这个号已无意义）。
    // 注意自动禁用**不算**——它的账号文件仍被上游加载、凭证仍然有效，测试/刷新
    // 恰恰是排查它为什么坏的手段（用户就是靠「连通性测试通过」确认凭证没问题的）。
    const off = paused || viaBit;
    // 「停用 / 启用」开关显示哪一面：三种「不在池里」都算。此前漏了自动禁用，
    // 于是被禁用的号按钮显示成「停用」，点一下反而给**已被禁用**的号再加一层
    // manual_disabled（越弄越糟），而真正需要的「启用」根本没有入口。
    const stopped = paused || viaBit || autoDisabled;
    const hasClearableState = a.cooling === true || rateLimitedModels(a).length > 0;
    return (
      <div className="flex justify-end gap-1">
        {/* 备注（issue #67）：只动本端库里的一行文本，不碰上游、不重启容器，
            所以放在最前——它是最轻的动作。任何状态下的账号都能写备注：
            停用的号恰恰更容易忘了它是谁。 */}
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 rounded-md"
          title={a.note ? t('accounts.noteEditTitle') : t('accounts.noteAdd')}
          disabled={busy}
          onClick={() => setNoteTarget(a)}
        >
          <StickyNote className={'h-3.5 w-3.5 ' + (a.note ? 'text-amber-600 dark:text-amber-400' : '')} />
        </Button>
        {/* 临时停用 / 启用（issue #21、#45）。放在最前：它是最轻的「止血」动作——
            某个号在拖后腿（一直失败、触发风控）时，先停用它比删掉更合适
            （删除会丢凭证、只能重新扫码；停用是可逆的）。 */}
        {(!off || viaBit) && canCheckin && (
          <Button
            variant="ghost"
            size="icon"
            className={
              'h-7 w-7 rounded-md ' +
              (checkinDone
                ? 'text-emerald-600 hover:text-emerald-600 disabled:opacity-100 dark:text-emerald-400'
                : '')
            }
            title={
              checkinDone
                ? t('accounts.checkinDoneTitle', {at: fmtDateTime(a.checkin_today as number)})
                : t('accounts.checkin')
            }
            disabled={busy || checkinDone}
            onClick={() => run(a.file, () => accountApi.checkin(a.file, groupId),
                               t('accounts.opDone'))}
          >
            {checkinDone ? <Check className="h-3.5 w-3.5" /> : <Gift className="h-3.5 w-3.5" />}
          </Button>
        )}
        {/* 活动任务（单账号）：与「任务」页的一键执行是同一套（调用上游
            task_runner.py），区别是只对这一个账号跑 —— 池子里某个号想单独
            补一轮任务时，不必把全部账号再跑一遍。国际版不适用 CN 任务中心
            （脚本自己会 skip），所以与签到按钮同一个可见条件。
            分组账号暂不在这里跑任务（任务脚本目前只覆盖默认分组的目录，
            见 server/services/taskrun.py），所以非默认分组下不显示这个按钮。 */}
        {(!off || viaBit) && canCheckin && groupId == null && (
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 rounded-md"
            title={t('accounts.taskRun')}
            disabled={busy}
            onClick={() => setTaskTarget(a)}
          >
            <Sparkles className="h-3.5 w-3.5" />
          </Button>
        )}
        {(!off || viaBit) && (
          <>
            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title={t('accounts.test')} disabled={busy}
              onClick={() => run(a.file, () => accountApi.test(a.file, groupId),
                                 t('accounts.testDone'))}>
              <Zap className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title={t('accounts.refreshToken')} disabled={busy}
              onClick={() => run(a.file, () => accountApi.refresh(a.file, groupId),
                                 t('accounts.refreshDone'))}>
              <KeyRound className="h-3.5 w-3.5" />
            </Button>
          </>
        )}
        {!paused && hasClearableState && (
          <ConfirmDialog
            title={t('accounts.forceClearCoolingTitle')}
            description={t('accounts.forceClearCoolingDesc')}
            confirmText={t('accounts.forceClearCoolingConfirm')}
            destructive
            onConfirm={() => run(
              a.file,
              () => accountApi.clearCooling(a.file, groupId),
              t('accounts.forceClearCoolingDone'),
              true,
            )}
            trigger={
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 rounded-md text-rose-600 hover:text-rose-700 dark:text-rose-400"
                title={t('accounts.forceClearCooling')}
                disabled={busy}
              >
                <RotateCcw className="h-3.5 w-3.5" />
              </Button>
            }
          />
        )}
        {/* 移动到分组（多账号池）：把账号文件转移到另一个分组——凭证一个
            字节不改。只有存在「别的分组」时才显示；能不能收由后端判（目标
            没有本地账号目录会 409，弹窗里直接禁掉那些选项）。 */}
        {isAdmin && groups.filter((g) => !g.is_default).length > 0 && (
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 rounded-md"
            title={t('accounts.move')}
            disabled={busy}
            onClick={() => setMoveTarget(a)}
          >
            <FolderInput className="h-3.5 w-3.5" />
          </Button>
        )}
        <Button
          variant="ghost"
          size="icon"
          className={
            'h-7 w-7 rounded-md ' +
            (stopped ? 'text-emerald-600 hover:text-emerald-600' : 'text-amber-600 hover:text-amber-600')
          }
          title={stopped ? t('accounts.enableTitle') : t('accounts.disableTitle')}
          disabled={busy}
          onClick={() => run(
            a.file,
            // 传 `stopped` 的反面：自动禁用的号点这里会走「启用」，后端在启用路径上
            // 会一并解除系统禁用位（调上游的 revive）——这正是它此前缺的入口。
            () => accountApi.setDisabled(a.file, !stopped, groupId),
            // 文案如实反映用的是哪种机制：状态位停用后任务照常，改名停用则全停。
            stopped ? t('accounts.enabled')
                    : (viaBit ? t('accounts.manualDisabled') : t('accounts.disabled')),
          )}
        >
          {stopped ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
        </Button>
        <ConfirmDialog
          title={t('accounts.deleteTitle', {name: a.nickname || a.uid})}
          description={t('accounts.deleteDesc')}
          confirmText={t('accounts.delete')}
          destructive
          onConfirm={() => run(a.file, () => accountApi.remove(a.file, groupId),
                               t('accounts.deleted'))}
          trigger={
            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md text-red-500 hover:text-red-600" title={t('accounts.delete')}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          }
        />
      </div>
    );
  }

  /** 头像（首字母） */
  function renderAvatar(a: Account) {
    return (
      <div
        className={
          'grid h-7 w-7 shrink-0 place-items-center rounded-full text-[11px] font-semibold ' +
          (a.is_expired ? 'bg-muted-foreground/20 text-muted-foreground' : 'bg-primary text-primary-foreground')
        }
      >
        {(a.nickname || '?').charAt(0)}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      <PageHeader
        title={t('accounts.title')}
        description={
          realm === 'global'
            ? t('accounts.descGlobal')
            : t('accounts.descCn')
        }
        actions={
          <>
            {isAdmin && (
              <ConfirmDialog
                title={t('accounts.restartTitle')}
                description={t('accounts.restartDesc')}
                confirmText={t('accounts.restartConfirm')}
                onConfirm={restartUpstream}
                trigger={
                  <Button variant="outline" size="sm" className="rounded-full" disabled={restarting}>
                    <Power className={restarting ? 'animate-spin' : ''} />
                    <span className="hidden sm:inline">{t('accounts.forceRestart')}</span>
                    <span className="sm:hidden">{t('accounts.restartShort')}</span>
                  </Button>
                }
              />
            )}
            {/* 「刷新积分」= 强制查询（绕过 60 秒缓存），只有管理员能调。
                只读账号页面加载时本来就会走缓存路径拿到实时值，所以这个按钮
                对它们没有意义 —— 显示出来只会点了报 403（issue #56 之后权限
                是明确的：force 仍限管理员）。与上面几个管理员操作同一个口径。 */}
            {isAdmin && (
            <Button
              size="sm"
              variant="outline"
              className="rounded-full"
              onClick={refreshCredits}
              disabled={creditsBusy || !visible.length}
              title={t('accounts.refreshCreditsTitle')}
            >
              <Coins className={creditsBusy ? 'animate-pulse' : ''} />
              <span className="hidden sm:inline">{t('accounts.refreshCredits')}</span>
              <span className="sm:hidden">{t('accounts.creditsShort')}</span>
            </Button>
            )}
            {/* 「全部签到」仅国内版显示：国际版**没有签到体系**（上游调度器对
                global 账号直接过滤，不发请求）。显示一个按下去只会得到「已跳过」
                的按钮是误导，直接不给。 */}
            {/* 「全部签到」反映当天状态：
                · 还有待签的 → 文案回到「全部签到」，并带上待签数（不用点进去数）
                · 今天都签过了 → 文案变「今日已签」并禁用。禁用比「点了提示已签到」
                  更好：重复点击本来就不该发生，让按钮自己说出原因，比点完再报错省一次往返。
                判据 pendingCheckin 与每行签到按钮的可见条件一致，不会出现
                「按钮说还有 1 个、列表里却找不到那个号」的矛盾。 */}
            {isAdmin && realm === 'cn' && (
              <Button
                size="sm"
                variant="outline"
                className="rounded-full"
                onClick={checkinAll}
                disabled={checkinAllBusy || pendingCheckin === 0}
                title={
                  pendingCheckin === 0
                    ? t('accounts.checkinAllNoneTitle')
                    : t('accounts.checkinAllTitle', {n: pendingCheckin})
                }
              >
                <CalendarCheck className={checkinAllBusy ? 'animate-pulse' : ''} />
                {pendingCheckin === 0 ? t('accounts.checkinAllNone') : t('accounts.checkinAll')}
                {pendingCheckin > 0 && (
                  <span className="tabular-nums opacity-70">{pendingCheckin}</span>
                )}
              </Button>
            )}
            {isAdmin && (
              <UploadAccountsButton upstreamId={groupId} onSuccess={reloadAll} />
            )}
            {isAdmin && (
              <Button size="sm" className="rounded-full" onClick={() => setAddOpen(true)}>
                <Plus />
                {t('accounts.addAccount')}
              </Button>
            )}
          </>
        }
      />

      {/* 二级导航（批次 4 ②：本页吸收了「任务记录」——账号是「主体」，
          任务记录是「这个主体干了什么」，用户在两分钟内必然要一起看）。
          本页没有早返回的整页守卫（取不到账号时只在下面给错误态），
          所以放在这里一处即可。 */}
      <PageSectionTabs />

      {/* 账号分组（多账号池）：默认分组 = 升级前那套（环境变量 / 上游
          config.json）；「添加分组」只填名称即可（地址默认沿用默认分组的、
          账号目录自动带出建议路径，其余字段到「设置 → 上游」再调）。
          密钥绑定哪个分组，请求就走那一组的接入点。 */}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant={groupId == null ? 'default' : 'outline'}
          size="sm"
          className="rounded-full"
          onClick={() => setGroupId(null)}
        >
          {t('accounts.groupDefault')}
        </Button>
        {groups
          .filter((g) => !g.is_default && g.id != null)
          .map((g) => (
            <Button
              key={g.id}
              variant={groupId === g.id ? 'default' : 'outline'}
              size="sm"
              className="rounded-full"
              onClick={() => setGroupId(g.id as number)}
            >
              {g.name}
              {!g.enabled ? t('accounts.groupDisabledSuffix') : ''}
            </Button>
          ))}
        {isAdmin && (
          <Button
            variant="ghost"
            size="sm"
            className="rounded-full"
            onClick={() => setGroupDialogOpen(true)}
          >
            <Plus className="h-3.5 w-3.5" />
            {t('accounts.groupAdd')}
          </Button>
        )}
        {isAdmin && groupId != null && (
          <ConfirmDialog
            title={t('accounts.groupDeleteTitle', {name: groupInfo?.name || ''})}
            description={t('accounts.groupDeleteDesc')}
            confirmText={t('accounts.groupDelete')}
            destructive
            onConfirm={() => deleteGroup()}
            trigger={
              <Button variant="ghost" size="sm" className="rounded-full text-destructive">
                <Trash2 className="h-3.5 w-3.5" />
                {t('accounts.groupDelete')}
              </Button>
            }
          />
        )}
      </div>
      {groupInfo && !groupInfo.manageable && groupId != null && (
        <p className="px-1 text-[11px] leading-4 text-muted-foreground">
          {t('accounts.groupNoDir', {name: groupInfo.name})}
        </p>
      )}

      {/* 分组和默认分组共用一套上游实例 → 这一组的账号不会被加载。不解释的话，
          「未加载」看起来像坏了：用户会反复重扫码、重试（issue #94）。 */}
      {sharedInstance && (
        <div className="flex items-start gap-2.5 rounded-2xl bg-amber-500/10 px-3.5 py-3 ring-1 ring-amber-500/20">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
          <div className="min-w-0 space-y-1">
            <div className="text-xs font-medium">{t('accounts.sharedInstanceTitle')}</div>
            <p className="text-[11px] leading-4 text-muted-foreground">
              {t('accounts.sharedInstanceHint')}
            </p>
            <Link href="/settings" className="inline-block text-[11px] text-blue-500 hover:underline">
              {t('accounts.sharedInstanceAction')}
            </Link>
          </div>
        </div>
      )}

      {/* 部分取数失败：**不能**升级成整页错误——账号列表本身可能是好的，
          把它顶掉等于因为一个配件坏掉就说「什么都看不到」。逐条说清是哪一份、
          以及它会让界面上的哪个地方失真。 */}
      {upstreamFailed && (
        <LoadError message={t('accounts.upstreamStatusFailed')} onRetry={reload} />
      )}
      {groupsFailed && (
        <LoadError message={t('accounts.groupsFailed')} onRetry={groupList.reload} />
      )}
      {creditsFailed && (
        <LoadError message={t('accounts.creditsAutoFailed')} onRetry={refreshCredits} />
      )}

      {/* 账号池：首屏还没取到就渲染骨架，取不到就给错误态 + 重试。
          ⚠️ 判据是「有没有数据」而不是「请求在不在飞」（见 lib/async-state 的
          asyncFlags）：后者会让 30 秒一次的心跳把骨架闪一遍。切换分组会清空重取
          （换的是数据集），那时确实该显示骨架。
          这一块**不**早返回整页：分组切换条必须一直可用，否则某一组的账号取不到时
          用户连切回默认分组自救都做不到。 */}
      {(isInitialFailed || isInitialLoading) && (
        isInitialFailed
          ? <LoadError variant="page" onRetry={reload} />
          : <AccountsSkeleton />
      )}

      {/* 检索与状态筛选（P1-3）。账号还没取到时不渲染：那会儿计数全是 0，
          摆出来等于说「一个账号都没有」。
          这一排按钮是**状态信息不再依赖 hover** 的关键——移动端没有 hover，
          原先 9 档状态只能靠徽章的悬浮提示解释，等于状态信息不可见。 */}
      {accounts && (
        <section className="rounded-[20px] bg-muted p-3.5">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={q}
                onChange={(e) => { setQ(e.target.value); setPage(1); }}
                placeholder={t('accounts.searchPlaceholder')}
                className="h-9 bg-background pl-8"
              />
            </div>
            <div className="-mx-0.5 flex flex-wrap items-center gap-1.5 overflow-x-auto px-0.5 pb-0.5">
              <span className="flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground">
                <Coins className="h-3.5 w-3.5" />
                {t('accounts.colStatus')}
              </span>
              {(['all', ...AVAILABILITY_GROUPS] as const).map((g) => (
                <Button
                  key={g}
                  variant={statusGroup === g ? 'default' : 'outline'}
                  size="sm"
                  className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
                  onClick={() => { setStatusGroup(g); setPage(1); }}
                >
                  {g === 'all' ? t('common.all') : t(availabilityGroupLabelKey(g))}
                  <span className="ml-1 tabular-nums opacity-70">{counts[g]}</span>
                </Button>
              ))}
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <ArrowUpDown className="h-3.5 w-3.5 text-muted-foreground" />
              <Select value={sort} onValueChange={(v) => { setSort(v as SortKey); setPage(1); }}>
                <SelectTrigger className="h-9 w-[150px] bg-background"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">{t('accounts.sortDefault')}</SelectItem>
                  <SelectItem value="remain">{t('accounts.sortRemain')}</SelectItem>
                  <SelectItem value="credits">{t('accounts.sortCredits')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </section>
      )}

      {accounts && (
      <section className="overflow-hidden rounded-[20px] bg-muted">
        {/* 手机端：卡片列表。表格 6 列在窄屏需要横向滚动，读一行要来回拖，
            改为纵向卡片后信息一眼可见 */}
        <div className="divide-y divide-border/40 md:hidden">
          {paged.rows.map((a) => (
            <div key={a.file} className="space-y-2.5 px-3.5 py-3">
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2.5">
                  {renderAvatar(a)}
                  <div className="min-w-0">
                    <div
                      className={
                        'truncate text-sm font-medium ' + (a.is_expired ? 'text-muted-foreground' : '')
                      }
                    >
                      {a.nickname || t('accounts.unnamed')}
                    </div>
                    <div className="truncate font-mono text-[10px] text-muted-foreground">{a.uid}</div>
                    {a.note ? (
                      <div className="truncate text-[10px] text-muted-foreground" title={a.note}>
                        {t('accounts.noteLabel')}{a.note}
                      </div>
                    ) : null}
                  </div>
                </div>
                <div className="flex flex-wrap items-center justify-end gap-1.5">
                  {renderStatus(a)}
                  {renderModelLimit(a)}
                </div>
              </div>

              <div className="flex items-center justify-between gap-3">
                <div className="flex shrink-0 items-center gap-1.5">
                  <Coins className="h-3.5 w-3.5 text-muted-foreground" />
                  {renderCredits(a)}
                </div>
                {renderExpiry(a)}
              </div>

              {hasProxyUi && (
                <div className="flex items-center gap-2 text-xs"><span className="text-muted-foreground">{t('accounts.proxyLine')}</span>{renderProxy(a)}</div>
              )}
              {isAdmin && renderActions(a)}
            </div>
          ))}
        </div>

        {/* 桌面端：表格 */}
        <div className="hidden md:block">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('accounts.colNickname')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">UID</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('accounts.colStatus')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('metric.credits')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('accounts.colExpiry')}</TableHead>
              {hasProxyUi && <TableHead className="text-[11px] text-muted-foreground">{t('accounts.proxyLine')}</TableHead>}
              {isAdmin && <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">{t('accounts.colActions')}</TableHead>}
            </TableRow>
          </TableHeader>
          <TableBody>
            {paged.rows.map((a) => (
              <TableRow key={a.file} className="border-b border-border/40">
                <TableCell className="pl-4">
                  <div className="flex items-center gap-2.5">
                    {renderAvatar(a)}
                    <div className="min-w-0">
                      <div className={'truncate text-sm font-medium ' + (a.is_expired ? 'text-muted-foreground' : '')}>
                        {a.nickname || t('accounts.unnamed')}
                      </div>
                      {a.note ? (
                        <div className="truncate text-[10px] text-muted-foreground" title={a.note}>
                          {t('accounts.noteLabel')}{a.note}
                        </div>
                      ) : null}
                    </div>
                    <Badge
                      variant="secondary"
                      className={
                        'shrink-0 rounded-md px-1.5 py-0 text-[10px] ' +
                        ((a.realm ?? 'cn') === 'global'
                          ? 'bg-sky-500/10 text-sky-700 dark:text-sky-400'
                          : '')
                      }
                    >
                      {realmLabel(a.realm)}
                    </Badge>
                  </div>
                </TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">{a.uid}</TableCell>
                <TableCell>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {renderStatus(a)}
                    {renderModelLimit(a)}
                  </div>
                </TableCell>
                <TableCell>{renderCredits(a)}</TableCell>
                <TableCell>{renderExpiry(a)}</TableCell>
                {hasProxyUi && <TableCell>{renderProxy(a)}</TableCell>}
                {isAdmin && <TableCell className="pr-4">{renderActions(a)}</TableCell>}
              </TableRow>
            ))}
          </TableBody>
        </Table>
        </div>

        {/*
          「看不到账号」有三种原因，**必须分开说**——它们要用户做的事完全不同：
            ① 一个账号都没有 → 去添加（原来只有这一种说法）
            ② 有账号，但都不是当前版本的 → 去切版本。原来说的是「暂无账号」，
               而这正是本次要修的谎话：切到国际版而池子里只有国内版账号时，
               页面说「暂无账号」，读起来像号池是空的。
            ③ 有账号，被搜索/状态筛选滤掉了 → 去清筛选。说「暂无账号」更糟：
               账号明明在，只是被他自己刚才的筛选挡住了。
        */}
        {noAccountsAtAll ? (
          <EmptyState
            icon={Users}
            title={t('accounts.emptyTitle')}
            description={isAdmin ? t('accounts.emptyDescAdmin') : t('accounts.emptyDescViewer')}
            className="flex flex-col items-center justify-center py-16 text-center"
          >
            {isAdmin && (
              <Button className="mt-4 rounded-full" onClick={() => setAddOpen(true)}>
                <Plus />
                {t('accounts.addAccount')}
              </Button>
            )}
          </EmptyState>
        ) : noAccountsInRealm ? (
          <EmptyState
            icon={Users}
            title={t('accounts.emptyRealmTitle', {realm: realmName})}
            description={t('accounts.emptyRealmDesc')}
            className="flex flex-col items-center justify-center py-16 text-center"
          />
        ) : noMatch ? (
          <EmptyState
            icon={Search}
            title={t('accounts.noMatchTitle')}
            description={t('accounts.noMatchDesc')}
            className="flex flex-col items-center justify-center py-16 text-center"
          >
            <Button variant="outline" className="mt-4 rounded-full" onClick={clearListFilters}>
              {t('accounts.clearFilters')}
            </Button>
          </EmptyState>
        ) : null}

        {/* 页码条只在不止一页时出现——小池子（绝大多数部署）看不到多余控件 */}
        {paged.pages > 1 && (
          <div className="flex items-center justify-between border-t border-border/40 px-4 py-3">
            <div className="text-[11px] text-muted-foreground">
              {t('accounts.pageInfo', {total: paged.total, page: paged.page, pages: paged.pages})}
            </div>
            <div className="flex gap-1">
              <Button
                variant="outline"
                size="icon"
                className="h-7 w-7 rounded-md"
                disabled={paged.page <= 1}
                onClick={() => setPage(paged.page - 1)}
              >
                <ChevronLeft className="h-3.5 w-3.5" />
              </Button>
              <Button
                variant="outline"
                size="icon"
                className="h-7 w-7 rounded-md"
                disabled={paged.page >= paged.pages}
                onClick={() => setPage(paged.page + 1)}
              >
                <ChevronRight className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>
        )}
      </section>
      )}

      {/* 签到与任务记录已独立成页（账号一多，堆在本页会越滑越长） */}
      <div className="flex flex-wrap items-center gap-2 px-1 text-[11px] text-muted-foreground">
        <span>{t('accounts.movedTo')}</span>
        <Link href="/tasks" className="inline-flex items-center gap-1 rounded-full bg-muted px-2.5 py-1 font-medium text-foreground transition-colors hover:bg-muted/70">
          {t('nav.tasks')}
          <ChevronRight className="h-3 w-3" />
        </Link>
      </div>

      <AddAccountDialog open={addOpen} onOpenChange={setAddOpen} onSuccess={reloadAll}
                        upstreamId={groupId} />
      <MoveAccountDialog
        account={moveTarget}
        open={moveTarget !== null}
        onOpenChange={(open) => { if (!open) setMoveTarget(null); }}
        groups={groups}
        fromGroupId={groupId}
        onMoved={reloadAll}
      />
      <UpstreamFormDialog
        open={groupDialogOpen}
        onOpenChange={setGroupDialogOpen}
        editing={null}
        defaultUpstream={groups.find((g) => g.is_default) ?? null}
        simple
        onSaved={(item) => {
          // 新分组建好就切过去看它（`groupId` 进主数据的 deps，一改自动重取）；
          // 分组清单要单独刷，否则新建的分组不会出现在切换条上。
          if (item && item.id != null) setGroupId(item.id);
          void groupList.reload();
        }}
      />
      <AccountNoteDialog
        account={noteTarget}
        open={noteTarget !== null}
        onOpenChange={(open) => { if (!open) setNoteTarget(null); }}
        onSaved={reloadAll}
        upstreamId={groupId}
      />
      <AccountTaskDialog
        account={taskTarget}
        open={taskTarget !== null}
        onOpenChange={(open) => { if (!open) setTaskTarget(null); }}
        onFinished={reloadAll}
      />
    </div>
  );
}

/**
 * 首屏骨架：与真实版面**逐块对应**——一张 `bg-muted` 卡片，每行是
 * 「头像 + 两行文字（昵称 / 备注）+ 几个状态块」，宽屏下再多两列（状态徽章、积分）。
 *
 * 为什么不沿用原来的纯文本「加载中…」：它和空态长得太像（都是一行灰字），
 * 用户分不清「正在取」与「取到了、只是没有号」。骨架把版面形状先摆出来，
 * 两种状态一眼可辨。
 *
 * 行数固定 4：真实行数此时还不知道，固定值只为给出「这是一张列表」的形状；
 * 给多了反而像「已经取到很多账号」。
 */
function AccountsSkeleton() {
  return (
    <section className="overflow-hidden rounded-[20px] bg-muted">
      <div className="divide-y divide-border/40">
        {Array.from({length: 4}, (_, i) => (
          <div key={i} className="flex items-center gap-3 px-3.5 py-3 md:px-4">
            <SkeletonBar className="h-8 w-8 shrink-0 rounded-full" />
            <div className="min-w-0 flex-1 space-y-2">
              <SkeletonBar className="h-3.5 w-28 max-w-[40%]" />
              <SkeletonBar className="h-2.5 w-40 max-w-[60%]" />
            </div>
            <SkeletonBar className="hidden h-5 w-16 shrink-0 rounded-full sm:block" />
            <SkeletonBar className="hidden h-3.5 w-14 shrink-0 md:block" />
            <SkeletonBar className="h-3.5 w-12 shrink-0" />
          </div>
        ))}
      </div>
    </section>
  );
}

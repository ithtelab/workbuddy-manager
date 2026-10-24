import {useState, useEffect, useRef, useCallback} from 'react';
import {FloatingDock} from '@/components/ui/floating-dock';
import packageJson from '../../../package.json';
import {
  MessageCircleIcon,
  BarChart3,
  Users,
  KeyRound,
  Boxes,
  ScrollText,
  TrendingUp,
  ShieldCheck,
  Settings,
  PlusCircle,
  User,
  LogOut as LogOutIcon,
  ShieldAlert as ShieldAlertIcon,
  Link2,
  FolderGit2,
  ChevronRight,
  Pin as PinIcon,
  Move as MoveIcon,
} from 'lucide-react';
import {useThemeUtils} from '@/hooks/use-theme-utils';
import {useAuth} from '@/lib/auth-context';
import {accountApi, authApi, errText, systemApi} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import {CountingNumber} from '@/components/animate-ui/text/counting-number';
import {Button} from '@/components/ui/button';
import Link from 'next/link';
import {Badge} from '@/components/ui/badge';
import {Separator} from '@/components/ui/separator';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogHeader,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from '@/components/animate-ui/radix/dialog';
import {Avatar, AvatarFallback} from '@/components/ui/avatar';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {AddAccountDialog} from '@/components/common/accounts/AddAccountDialog';

const IconOptions = {
  className: 'h-4 w-4',
} as const;

// 构建时间：由 next.config.ts 在构建时注入（`NEXT_PUBLIC_BUILD_TIME`）。
// package.json 里的 buildDate 是手写死值、从没更新过，用它会让「Build At」
// 永远显示同一个日期。取不到就不显示这一项，不编造。
const BUILD_TIME = process.env.NEXT_PUBLIC_BUILD_TIME || '';

// v2：坐标语义由「左边缘」改为「水平中心」，旧版本存储的位置不再兼容
const DOCK_STORAGE_KEY = 'workbuddy-manager:dock-position-v2';
const DOCK_TIP_STORAGE_KEY = 'workbuddy-manager:dock-tip-dismissed';
// 底栏位置模式。floating = 自由拖动（默认，沿用原有形态）；pinned = 固定底部居中、不可拖。
// 之所以要有 pinned：底栏是**浮动**的，拖到页面中部就会压住正文（`docs/images/settings.png`
// 里它正压着「自定义系统提示词」输入区）。形态本身按既定决策保留，但得给用户一个
// 「别再挡我」的确定性选项 —— 拖回去靠的是「记得它本来在哪」。
const DOCK_MODE_STORAGE_KEY = 'workbuddy-manager:dock-mode';
const DOCK_MARGIN = 16;
// 180ms 太短：正常慢点击也会越过阈值，进入拖动后 pointerup 还会吞掉 click。
// 500ms 与系统长按手势一致；拖动前还要再确认「指针没先移动」。
const DOCK_LONG_PRESS_MS = 500;
const DOCK_DRAG_CANCEL_DISTANCE = 8;
const DOCK_DRAG_MOVE_EPSILON = 2;
const DOCK_CLICK_SUPPRESS_MS = 220;
const DOCK_INTERACTIVE_SELECTOR = 'a,button,input,textarea,select,[role="button"],[data-dock-no-drag="true"]';

type DockViewport = 'desktop' | 'mobile';
type DockMode = 'floating' | 'pinned';

type DockPosition = {
  x: number;
  y: number;
};

type StoredDockPosition = DockPosition & {
  viewportWidth?: number;
  viewportHeight?: number;
};

type DockPositions = Partial<Record<DockViewport, StoredDockPosition>>;

const SystemTheme = {
  LIGHT: 'light',
  DARK: 'dark',
} as const;

export function ManagementBar() {
  const themeUtils = useThemeUtils();
  const {me, isAdmin, logout} = useAuth();
  const t = useT();
  const [mounted, setMounted] = useState(false);
  const [dockViewport, setDockViewport] = useState<DockViewport>('desktop');
  const [profileOpen, setProfileOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [dockPosition, setDockPosition] = useState<DockPosition | null>(null);
  const [dockMode, setDockMode] = useState<DockMode>('floating');
  const [showDockTip, setShowDockTip] = useState(false);
  const [dockTipStep, setDockTipStep] = useState(0);
  /** 受管账号数量（真实数据，供个人信息面板展示） */
  const [accountCount, setAccountCount] = useState<number | null>(null);
  /**
   * 真实运行版本（取自后端）。
   *
   * 为什么不用 `package.json` 里的 version：那个字段没人维护——它停在 1.0.0，
   * 于是「关于」永远显示 1.0.0；用户刚更新完也看到旧号，会以为更新没生效。
   * 后端版本号才是更新流程实际替换的（`server/main.py` 的 app.version），
   * 以它为准才不会骗人。
   */
  const [runtimeVersion, setRuntimeVersion] = useState<string | null>(null);
  const dockRef = useRef<HTMLDivElement>(null);
  const dockViewportRef = useRef<DockViewport>('desktop');
  /**
   * 位置解析要读模式，但模式在 `syncDockPosition` 的依赖里会带来无谓的重建
   * （每次切换模式都会换掉 resize 监听）。用 ref 镜像一份，回调保持稳定。
   */
  const dockModeRef = useRef<DockMode>('floating');
  const dragOffsetRef = useRef({x: 0, y: 0});
  const dockPressTimerRef = useRef<number | null>(null);
  const pressStartRef = useRef({x: 0, y: 0});
  const pressCleanupRef = useRef<(() => void) | null>(null);
  const isDraggingRef = useRef(false);
  const dragMovedRef = useRef(false);
  const suppressClickUntilRef = useRef(0);

  const getViewport = useCallback((): DockViewport => (window.innerWidth >= 768 ? 'desktop' : 'mobile'), []);

  const readDockPositions = useCallback((): DockPositions => {
    if (typeof window === 'undefined') return {};

    try {
      const raw = window.localStorage.getItem(DOCK_STORAGE_KEY);
      return raw ? JSON.parse(raw) as DockPositions : {};
    } catch {
      return {};
    }
  }, []);

  const writeDockPosition = useCallback((viewport: DockViewport, position: DockPosition) => {
    if (typeof window === 'undefined') return;

    const nextPositions = {
      ...readDockPositions(),
      [viewport]: {
        ...position,
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
      },
    };

    window.localStorage.setItem(DOCK_STORAGE_KEY, JSON.stringify(nextPositions));
  }, [readDockPositions]);

  const getDockRect = useCallback(() => {
    const rect = dockRef.current?.getBoundingClientRect();
    return {
      width: rect?.width ?? (dockViewportRef.current === 'desktop' ? 620 : 52),
      height: rect?.height ?? (dockViewportRef.current === 'desktop' ? 88 : 52),
    };
  }, []);

  /**
   * 这里的坐标是「底栏水平中心」而非左边缘。
   * 容器通过 transform: translateX(-50%) 以中心对齐，
   * 这样鼠标悬停导致图标放大、底栏总宽变化时，会向两侧对称扩展，
   * 视觉上不会发生位移。
   */
  const clampDockPosition = useCallback((position: DockPosition): DockPosition => {
    if (typeof window === 'undefined') return position;

    const {width, height} = getDockRect();
    const halfW = width / 2;
    const minX = DOCK_MARGIN + halfW;
    const maxX = Math.max(minX, window.innerWidth - DOCK_MARGIN - halfW);
    const maxY = Math.max(DOCK_MARGIN, window.innerHeight - height - DOCK_MARGIN);

    return {
      x: Math.min(Math.max(position.x, minX), maxX),
      y: Math.min(Math.max(position.y, DOCK_MARGIN), maxY),
    };
  }, [getDockRect]);

  const getDefaultDockPosition = useCallback((viewport: DockViewport): DockPosition => {
    if (typeof window === 'undefined') return {x: DOCK_MARGIN, y: DOCK_MARGIN};

    const {width, height} = getDockRect();
    const basePosition = viewport === 'desktop' ?
      {
        x: window.innerWidth / 2,
        y: window.innerHeight - height - DOCK_MARGIN,
      } :
      {
        x: window.innerWidth - DOCK_MARGIN - width / 2,
        y: window.innerHeight - height - DOCK_MARGIN,
      };

    return clampDockPosition(basePosition);
  }, [clampDockPosition, getDockRect]);

  const getScaledDockPosition = useCallback((position: StoredDockPosition): DockPosition => {
    if (typeof window === 'undefined' || !position.viewportWidth || !position.viewportHeight) {
      return clampDockPosition(position);
    }

    const {width, height} = getDockRect();
    const halfW = width / 2;
    const oldMinX = DOCK_MARGIN + halfW;
    const oldMaxX = Math.max(oldMinX, position.viewportWidth - DOCK_MARGIN - halfW);
    const oldMaxY = Math.max(DOCK_MARGIN, position.viewportHeight - height - DOCK_MARGIN);
    const nextMinX = DOCK_MARGIN + halfW;
    const nextMaxX = Math.max(nextMinX, window.innerWidth - DOCK_MARGIN - halfW);
    const nextMaxY = Math.max(DOCK_MARGIN, window.innerHeight - height - DOCK_MARGIN);
    const xRatio = oldMaxX === oldMinX ? 0.5 : (position.x - oldMinX) / (oldMaxX - oldMinX);
    const yRatio = oldMaxY === DOCK_MARGIN ? 0 : (position.y - DOCK_MARGIN) / (oldMaxY - DOCK_MARGIN);

    return clampDockPosition({
      x: nextMinX + xRatio * (nextMaxX - nextMinX),
      y: DOCK_MARGIN + yRatio * (nextMaxY - DOCK_MARGIN),
    });
  }, [clampDockPosition, getDockRect]);

  /**
   * 当前模式下、某个视口应该把底栏放在哪。
   *
   * 固定底部时**一律用默认位置**（桌面：底部居中；手机：右下角），不再读 localStorage
   * 里那份拖动过的坐标 —— 否则窗口一 resize 就会跳回用户之前拖到的位置，
   * 而用户选「固定底部」正是为了让它别再乱跑。
   */
  const resolveDockPosition = useCallback((viewport: DockViewport): DockPosition => {
    if (dockModeRef.current === 'pinned') return getDefaultDockPosition(viewport);
    const savedPosition = readDockPositions()[viewport];
    return savedPosition ? getScaledDockPosition(savedPosition) : getDefaultDockPosition(viewport);
  }, [getDefaultDockPosition, getScaledDockPosition, readDockPositions]);

  const syncDockPosition = useCallback((nextViewport?: DockViewport) => {
    if (typeof window === 'undefined') return;

    const viewport = nextViewport ?? getViewport();
    dockViewportRef.current = viewport;
    setDockViewport(viewport);
    setDockPosition(resolveDockPosition(viewport));
  }, [getViewport, resolveDockPosition]);

  /**
   * 切换底栏位置模式。只写 localStorage 与状态，位置交给 `syncDockPosition` 重算
   * —— 两处各算一次的话，resize 与切换会得到不同结果。
   */
  const changeDockMode = useCallback((mode: DockMode) => {
    dockModeRef.current = mode;
    setDockMode(mode);
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(DOCK_MODE_STORAGE_KEY, mode);
    }
    syncDockPosition();
  }, [syncDockPosition]);

  useEffect(() => {
    setMounted(true);
    if (typeof window === 'undefined') return;
    const stored = window.localStorage.getItem(DOCK_MODE_STORAGE_KEY) === 'pinned' ? 'pinned' : 'floating';
    dockModeRef.current = stored;
    setDockMode(stored);
  }, []);

  // 取真实运行版本（后端为准）。失败就退回 package.json，不让面板空白。
  useEffect(() => {
    let alive = true;
    systemApi
      .versions()
      .then((v) => {
        if (alive && v?.manager) setRuntimeVersion(v.manager);
      })
      .catch(() => {
        /* 未登录/网络异常：保留 package.json 的回退值即可 */
      });
    return () => {
      alive = false;
    };
  }, []);

  // 拉取受管账号数量；账号页增删后通过自定义事件刷新
  useEffect(() => {
    let alive = true;
    const fetchCount = async () => {
      try {
        const data = await accountApi.list();
        if (alive) setAccountCount(data.total);
      } catch {
        if (alive) setAccountCount(null);
      }
    };
    fetchCount();
    window.addEventListener('workbuddy-manager:accounts-changed', fetchCount);
    return () => {
      alive = false;
      window.removeEventListener('workbuddy-manager:accounts-changed', fetchCount);
    };
  }, []);

  // 每个浏览器会话检测一次新版本，有更新则弹出提醒（避免打扰不重复提示）
  useEffect(() => {
    if (!mounted || typeof window === 'undefined') return;
    const KEY = 'workbuddy-manager:update-notified';
    if (window.sessionStorage.getItem(KEY) === '1') return;
    window.sessionStorage.setItem(KEY, '1');

    (async () => {
      try {
        const c = await systemApi.checkUpdate();
        if (!c.has_any) return;
        const parts: string[] = [];
        if (c.manager.has_update) parts.push(t('update.managerVersion', {v: c.manager.latest}));
        if (c.upstream.has_update) parts.push(t('update.upstreamVersion', {v: c.upstream.latest}));
        notify.warn(t('update.newVersion'), t('update.notice', {targets: parts.join(' · ')}));
      } catch {
        /* 检测失败静默：不打扰用户（如服务器访问 GitHub 受限） */
      }
    })();
  }, [mounted, t]);

  useEffect(() => {
    if (!mounted || typeof window === 'undefined') return;

    const dismissed = window.localStorage.getItem(DOCK_TIP_STORAGE_KEY) === 'true';
    if (!dismissed) {
      setShowDockTip(true);
    }
  }, [mounted]);

  useEffect(() => {
    if (!mounted) return;

    const frameId = window.requestAnimationFrame(() => {
      syncDockPosition();
    });

    const handleResize = () => {
      window.requestAnimationFrame(() => {
        const viewport = getViewport();

        dockViewportRef.current = viewport;
        setDockViewport(viewport);
        setDockPosition(resolveDockPosition(viewport));
      });
    };

    window.addEventListener('resize', handleResize);

    return () => {
      window.cancelAnimationFrame(frameId);
      window.removeEventListener('resize', handleResize);
    };
  }, [getViewport, mounted, resolveDockPosition, syncDockPosition]);

  useEffect(() => {
    const openAdd = () => {
      setProfileOpen(false);
      setAddOpen(true);
    };
    window.addEventListener('workbuddy-manager:open-add-account', openAdd);
    return () => {
      window.removeEventListener('workbuddy-manager:open-add-account', openAdd);
    };
  }, []);

  const handleLogout = () => {
    logout();
  };

  const handleDismissDockTip = useCallback(() => {
    setShowDockTip(false);
    setDockTipStep(0);
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(DOCK_TIP_STORAGE_KEY, 'true');
    }
  }, []);

  const dockTipSteps = dockViewport === 'mobile' ?
    [
      t('dock.mobile1'),
      // 「长按空白区域可拖动」这一条在固定底部模式下是**假的**（那时拖动被禁用）。
      // 引导里留一句做不到的话，比不写更糟 —— 用户会反复长按然后怀疑是自己手法不对。
      ...(dockMode === 'floating' ? [t('dock.mobile2')] : []),
      t('dock.mobile3'),
      t('dock.mobile4'),
    ] :
    [
      ...(dockMode === 'floating' ? [t('dock.desktop1')] : []),
      t('dock.desktop2'),
      t('dock.desktop3'),
    ];

  const handleNextDockTip = useCallback(() => {
    setDockTipStep((current) => {
      if (current >= dockTipSteps.length - 1) {
        handleDismissDockTip();
        return current;
      }
      return current + 1;
    });
  }, [dockTipSteps.length, handleDismissDockTip]);

  // 点底栏以外的任何地方就把引导收起来。原实现只能靠**把几条都点完**才关得掉，
  // 于是它会在页面上一直挂着（用户反馈「反复出现在页面中部」）—— 它挂在底栏正上方，
  // 而底栏可以被拖到页面中部，于是它正好压住正文。
  // 捕获阶段监听：底栏自己的 pointerdown 也要能让它先关掉。
  useEffect(() => {
    if (!showDockTip || typeof document === 'undefined') return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest('[data-slot=dock-tip]')) return;
      handleDismissDockTip();
    };

    document.addEventListener('pointerdown', handlePointerDown, true);
    return () => document.removeEventListener('pointerdown', handlePointerDown, true);
  }, [handleDismissDockTip, showDockTip]);

  const clearDockPressTimer = useCallback(() => {
    if (dockPressTimerRef.current !== null) {
      window.clearTimeout(dockPressTimerRef.current);
      dockPressTimerRef.current = null;
    }
  }, []);

  const beginDockDrag = useCallback((clientX: number, clientY: number) => {
    if (!dockPosition || typeof window === 'undefined') return;

    const viewport = getViewport();
    dockViewportRef.current = viewport;
    // dockPosition.x 是底栏中心点，因此偏移量相对中心计算
    dragOffsetRef.current = {
      x: clientX - dockPosition.x,
      y: clientY - dockPosition.y,
    };
    isDraggingRef.current = true;
    dragMovedRef.current = false;
    const pressStart = pressStartRef.current;

    const handlePointerMove = (moveEvent: PointerEvent) => {
      const dx = moveEvent.clientX - pressStart.x;
      const dy = moveEvent.clientY - pressStart.y;
      // 长按后没移动就不算拖动：否则 pointerup 会吞掉一次正常点击。
      if (!dragMovedRef.current &&
          Math.hypot(dx, dy) < DOCK_DRAG_MOVE_EPSILON) return;
      dragMovedRef.current = true;

      const nextPosition = clampDockPosition({
        x: moveEvent.clientX - dragOffsetRef.current.x,
        y: moveEvent.clientY - dragOffsetRef.current.y,
      });

      setDockPosition(nextPosition);
    };

    const handlePointerUp = (upEvent: PointerEvent) => {
      const nextPosition = clampDockPosition({
        x: upEvent.clientX - dragOffsetRef.current.x,
        y: upEvent.clientY - dragOffsetRef.current.y,
      });

      setDockPosition(nextPosition);
      writeDockPosition(dockViewportRef.current, nextPosition);
      isDraggingRef.current = false;
      if (dragMovedRef.current) {
        suppressClickUntilRef.current = Date.now() + DOCK_CLICK_SUPPRESS_MS;
      }
      dragMovedRef.current = false;
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerUp);
      window.removeEventListener('pointercancel', handlePointerUp);
    };

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', handlePointerUp);
    window.addEventListener('pointercancel', handlePointerUp);
  }, [clampDockPosition, dockPosition, getViewport, writeDockPosition]);

  const handleDockPointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (!dockPosition || event.button !== 0) return;
    // 固定底部时**彻底关掉拖动**，而不只是「拖完再弹回来」：后者会先跟手走一段、
    // 松手才归位，看起来像拖动坏了。
    if (dockMode !== 'floating') return;
    if ((event.target as HTMLElement).closest(DOCK_INTERACTIVE_SELECTOR)) return;

    pressStartRef.current = {x: event.clientX, y: event.clientY};
    clearDockPressTimer();
    // 上一轮按下如果还在等长按，先撤掉它的 window 监听。
    pressCleanupRef.current?.();
    pressCleanupRef.current = null;

    function cleanupPressListeners() {
      window.removeEventListener('pointermove', handlePreDragMove);
      window.removeEventListener('pointerup', handlePreDragEnd);
      window.removeEventListener('pointercancel', handlePreDragEnd);
      pressCleanupRef.current = null;
    }
    function handlePreDragMove(moveEvent: PointerEvent) {
      const dx = moveEvent.clientX - pressStartRef.current.x;
      const dy = moveEvent.clientY - pressStartRef.current.y;
      // 还没到长按时间就先移动了 → 这是点击/滑动，不是拖动底栏。
      if (Math.hypot(dx, dy) < DOCK_DRAG_CANCEL_DISTANCE) return;
      clearDockPressTimer();
      cleanupPressListeners();
    }
    function handlePreDragEnd() {
      clearDockPressTimer();
      cleanupPressListeners();
    }

    window.addEventListener('pointermove', handlePreDragMove);
    window.addEventListener('pointerup', handlePreDragEnd);
    window.addEventListener('pointercancel', handlePreDragEnd);
    pressCleanupRef.current = cleanupPressListeners;

    dockPressTimerRef.current = window.setTimeout(() => {
      cleanupPressListeners();
      beginDockDrag(pressStartRef.current.x, pressStartRef.current.y);
      dockPressTimerRef.current = null;
    }, DOCK_LONG_PRESS_MS);
  }, [beginDockDrag, clearDockPressTimer, dockMode, dockPosition]);

  useEffect(() => () => {
    // 卸载时别把等待中的长按 timer 留到下一次挂载后再触发。
    clearDockPressTimer();
    pressCleanupRef.current?.();
  }, [clearDockPressTimer]);

  const handleDockPointerEnd = useCallback(() => {
    if (!isDraggingRef.current) {
      clearDockPressTimer();
    }
  }, [clearDockPressTimer]);

  const handleDockClickCapture = useCallback((event: React.MouseEvent<HTMLDivElement>) => {
    if (Date.now() > suppressClickUntilRef.current) return;

    event.preventDefault();
    event.stopPropagation();
  }, []);

  /**
   * 底栏分组（P1-1：11 项平铺、分隔线无标签）。
   *
   * 三组语义 —— 总览 / 运营 / 治理；快速添加与个人信息不是「目的地」而是动作，
   * 单独一组（不带组名，只画一条分隔线）。
   *
   * 组名只写在该组**第一项**上：`splitByGroup` 取的是组内首项的 label，
   * 所以不必每项重复一遍。
   *
   * ⚠️ 原来这里有一个 `{title: 'divider', icon: <div />}` 的**哨兵条目**。
   * 它有两个问题：渲染层每个分支都得记得跳过它（移动端就是靠一句
   * `if (item.title === 'divider') return null` 硬跳过的），而且**手机端因此
   * 完全没有分组语义**。现在改成把分组键挂在条目上，由渲染层自己切。
   *
   * ⚠️ 批次 4 ② 把**目的地从 11 项收敛到 8 项**：`任务记录` / `红包` /
   * `聊天测试台` 不再各占一个入口，改成在「账号」/「密钥」/「模型」页里用
   * 页内二级导航切换（清单在 `@/lib/section-nav`，渲染在 `PageSectionTabs`）。
   * 判据是「用户会不会在同一段时间里来回切」——会，那就不是两个一级入口。
   * 三个被吸收的页面**路径没变**，所以既有的书签与深链仍然直接可用。
   */
  const dockItems = [
    {
      title: t('nav.dashboard'),
      icon: <BarChart3 {...IconOptions} />,
      href: '/dashboard',
      groupKey: 'overview',
      groupLabel: t('nav.groupOverview'),
    },
    {
      title: t('nav.accounts'),
      icon: <Users {...IconOptions} />,
      href: '/accounts',
      groupKey: 'ops',
      groupLabel: t('nav.groupOps'),
    },
    {
      title: t('nav.keys'),
      icon: <KeyRound {...IconOptions} />,
      href: '/keys',
      groupKey: 'ops',
    },
    {
      title: t('nav.models'),
      icon: <Boxes {...IconOptions} />,
      href: '/models',
      groupKey: 'ops',
    },
    {
      title: t('nav.stats'),
      icon: <TrendingUp {...IconOptions} />,
      href: '/stats',
      groupKey: 'governance',
      groupLabel: t('nav.groupGovernance'),
    },
    {
      title: t('nav.logs'),
      icon: <ScrollText {...IconOptions} />,
      href: '/logs',
      groupKey: 'governance',
    },
    {
      title: t('nav.security'),
      icon: <ShieldCheck {...IconOptions} />,
      href: '/security',
      groupKey: 'governance',
    },
    {
      title: t('nav.settings'),
      icon: <Settings {...IconOptions} />,
      href: '/settings',
      groupKey: 'governance',
    },
    {
      title: t('nav.quickAdd'),
      icon: <PlusCircle {...IconOptions} />,
      // 动作组：不带组名，只跟前面的目的地分开
      groupKey: 'actions',
      customComponent: (
        <div
          data-slot="dock-quick-add"
          data-dock-no-drag="true"
          onClick={() => {
            if (isAdmin) setAddOpen(true);
          }}
          className="w-full h-full flex items-center justify-center cursor-pointer rounded transition-colors"
        >
          <PlusCircle className="h-4 w-4" />
        </div>
      ),
    },
    {
      title: t('nav.profile'),
      icon: <User {...IconOptions} />,
      groupKey: 'actions',
      customComponent: (
        <>
          <Dialog open={profileOpen} onOpenChange={setProfileOpen}>
            <DialogTrigger asChild>
              <div
                data-slot="dock-profile-trigger"
                data-dock-no-drag="true"
                className="w-full h-full flex items-center justify-center cursor-pointer rounded transition-colors"
              >
                <User className="h-4 w-4" />
              </div>
            </DialogTrigger>
            <DialogContent
              showCloseButton
              className="max-w-[520px]"
              onOpenAutoFocus={(event) => {
                // 阻止自动聚焦到「退出登录」，否则按钮会出现焦点圈，易被误触
                event.preventDefault();
              }}
            >
              <DialogHeader>
                <DialogTitle>{t('profile.title')}</DialogTitle>
                <DialogDescription>
                  {t('profile.desc')}
                </DialogDescription>
              </DialogHeader>
              <DialogBody className="max-h-[min(72vh,560px)]">
                <div className="space-y-5 px-5 pb-4">
                  {me && (
                    <>
                      <div className="space-y-4">
                        <div className="flex items-start justify-between gap-3">
                          <div className="flex min-w-0 items-center gap-3">
                            <Avatar className="size-12 rounded-full">
                              <AvatarFallback className="bg-muted text-sm font-semibold text-foreground">
                                {me.username?.slice(0, 2).toUpperCase() || 'U'}
                              </AvatarFallback>
                            </Avatar>
                            <div className="min-w-0">
                              <div className="truncate text-[15px] font-semibold text-foreground">
                                {me.username}
                              </div>
                              <div className="truncate text-xs text-muted-foreground">
                                WorkBuddy Manager
                              </div>
                              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                                <Badge variant="secondary" className="h-5 rounded-full px-2 text-[10px]">
                                  {me.role === 'admin' ? t('profile.roleAdmin') : t('profile.roleViewer')}
                                </Badge>
                              </div>
                            </div>
                          </div>
                          <ConfirmDialog
                            title={t('profile.logoutTitle')}
                            description={t('profile.logoutDesc')}
                            confirmText={t('profile.logout')}
                            destructive
                            onConfirm={handleLogout}
                            trigger={
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-8 shrink-0 rounded-full text-muted-foreground hover:text-red-600"
                              >
                                <LogOutIcon className="size-3.5" />
                                {t('profile.logout')}
                              </Button>
                            }
                          />
                        </div>

                        {/* 吊销全部会话：与「退出登录」的区别是它**让服务端所有已签发的
                            cookie 立即失效**，而不只是清掉本机这一个。用于怀疑会话被人
                            拿到（在别人电脑上登过、旧设备没退、备份里有 cookie）——
                            不必改密码（那会连带影响下游配置）。 */}
                        <div className="mt-2 flex justify-end">
                          <ConfirmDialog
                            title={t('profile.revokeTitle')}
                            description={t('profile.revokeDesc')}
                            confirmText={t('profile.revoke')}
                            destructive
                            onConfirm={async () => {
                              try {
                                await authApi.revokeSessions();
                                notify.ok(t('profile.revoked'));
                                // 服务端已失效所有会话（含本机），必须回登录页
                                setProfileOpen(false);
                                logout();
                              } catch (e) {
                                notify.err(errText(e));
                              }
                            }}
                            trigger={
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-8 shrink-0 rounded-full text-muted-foreground hover:text-amber-600"
                                title={t('profile.revokeHint')}
                              >
                                <ShieldAlertIcon className="size-3.5" />
                                {t('profile.revoke')}
                              </Button>
                            }
                          />
                        </div>

                        <Separator />

                        <div className="space-y-2">
                          <div className="text-[11px] font-medium text-muted-foreground">{t('profile.poolOverview')}</div>
                          <div className="flex flex-wrap items-center gap-2">
                            <div className="inline-flex items-center gap-2 rounded-full bg-muted px-3 py-2">
                              <Users className="size-3.5 text-foreground/60" />
                              <span className="text-xs font-medium text-foreground">{t('profile.managedAccounts')}</span>
                              <span className="text-xs font-semibold tabular-nums text-foreground">
                                {accountCount === null ? (
                                  '—'
                                ) : (
                                  // 不用 inView：弹窗以缩放动画挂载时，
                                  // useInView(once) 可能判定为不可见而停在初始值 0
                                  <CountingNumber
                                    number={accountCount}
                                    fromNumber={0}
                                    transition={{stiffness: 200, damping: 25}}
                                  />
                                )}
                              </span>
                            </div>
                          </div>
                        </div>

                        {mounted && (
                          <div className="space-y-2">
                            <div className="text-[11px] font-medium text-muted-foreground">{t('profile.systemSettings')}</div>
                            <div className="flex flex-wrap items-center gap-2">
                              <button
                                type="button"
                                onClick={themeUtils.toggle}
                                className="inline-flex items-center gap-2 rounded-full bg-muted px-3 py-2 transition-colors hover:bg-muted/80"
                              >
                                <div className="flex items-center gap-2">
                                  {themeUtils.getIcon('size-3.5 text-foreground/60')}
                                  <span className="text-xs font-medium text-foreground">
                                    {themeUtils.getSystemTheme() === SystemTheme.LIGHT ? t('theme.light') : t('theme.dark')}
                                  </span>
                                </div>
                              </button>

                              {/* 底栏位置：底栏是浮动的，拖到页面中部就会压住正文。
                                  形态按既定决策保留，但给一个「别再挡我」的确定性选项。
                                  做成 toggle 而不是两个按钮：它是二选一的状态，
                                  两个按钮会让人以为可以同时开。 */}
                              <button
                                type="button"
                                data-slot="dock-mode-toggle"
                                aria-label={t('profile.dockLabel')}
                                title={t('profile.dockLabel')}
                                onClick={() => changeDockMode(dockMode === 'floating' ? 'pinned' : 'floating')}
                                className="inline-flex items-center gap-2 rounded-full bg-muted px-3 py-2 transition-colors hover:bg-muted/80"
                              >
                                <div className="flex items-center gap-2">
                                  {dockMode === 'pinned' ? (
                                    <PinIcon className="size-3.5 text-foreground/60" />
                                  ) : (
                                    <MoveIcon className="size-3.5 text-foreground/60" />
                                  )}
                                  <span className="text-xs font-medium text-foreground">
                                    {dockMode === 'pinned' ? t('profile.dockPinned') : t('profile.dockFloating')}
                                  </span>
                                </div>
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                    </>
                  )}

                  <div className="space-y-2">
                    <div className="text-[11px] font-medium text-muted-foreground">{t('profile.quickLinks')}</div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Link
                        href="https://linux.do"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-2 rounded-full bg-muted px-3 py-2 transition-colors hover:bg-muted/80"
                        title={t('profile.communityTitle')}
                      >
                        <div className="flex items-center gap-2">
                          <Users className="size-3.5 text-foreground/60" />
                          <span className="text-xs font-medium text-foreground">{t('profile.community')}</span>
                        </div>
                      </Link>
                      <Link
                        href="https://github.com/Sliverkiss/workbuddy2api"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-2 rounded-full bg-muted px-3 py-2 transition-colors hover:bg-muted/80"
                      >
                        <div className="flex items-center gap-2">
                          <FolderGit2 className="size-3.5 text-foreground/60" />
                          <span className="text-xs font-medium text-foreground">workbuddy2api</span>
                        </div>
                      </Link>
                      <Link
                        href="https://github.com/linux-do/cdk"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-2 rounded-full bg-muted px-3 py-2 transition-colors hover:bg-muted/80"
                      >
                        <div className="flex items-center gap-2">
                          <Link2 className="size-3.5 text-foreground/60" />
                          <span className="text-xs font-medium text-foreground">{t('profile.uiReference')}</span>
                        </div>
                      </Link>
                      <Link
                        href="/settings"
                        className="inline-flex items-center gap-2 rounded-full bg-muted px-3 py-2 transition-colors hover:bg-muted/80"
                      >
                        <div className="flex items-center gap-2">
                          <MessageCircleIcon className="size-3.5 text-foreground/60" />
                          <span className="text-xs font-medium text-foreground">{t('profile.help')}</span>
                        </div>
                      </Link>
                    </div>
                  </div>

                  <Separator />

                  <div className="space-y-2">
                    <div className="text-xs font-medium">{t('profile.about')}</div>
                    <div className="space-y-1.5">
                      <div className="text-[11px] font-light text-muted-foreground">
                        {/* 版本以后端为准（package.json 里那个没人维护，停在 1.0.0）；
                            构建时间由 next.config 在构建时注入，不再用手写的死值。 */}
                        {runtimeVersion
                          ? `Version ${runtimeVersion}`
                          : `Version ${packageJson.version}${t('profile.versionFallback')}`}
                        {BUILD_TIME ? `, Build At ${BUILD_TIME}` : ''}
                      </div>
                      <div className="text-[11px] font-light leading-5 text-muted-foreground">
                        {t('profile.aboutDesc')}
                      </div>
                    </div>
                  </div>
                </div>
              </DialogBody>
            </DialogContent>
          </Dialog>
        </>
      ),
    },
  ];

  return (
    <>
      <div
        ref={dockRef}
        data-slot="dock-root"
        className="fixed z-40 select-none touch-none"
        onPointerDown={handleDockPointerDown}
        onPointerUp={handleDockPointerEnd}
        onPointerCancel={handleDockPointerEnd}
        onClickCapture={handleDockClickCapture}
        style={
          dockPosition ?
            {left: dockPosition.x, top: dockPosition.y, transform: 'translateX(-50%)'} :
            {visibility: 'hidden'}
        }
      >
        {showDockTip && (
          <div
            data-dock-no-drag="true"
            data-slot="dock-tip"
            role="status"
            className="pointer-events-auto absolute bottom-full right-0 mb-2 w-[min(15rem,calc(100vw-2.5rem))] rounded-2xl border border-border/60 bg-background/95 px-2.5 py-2 text-left shadow-[0_18px_40px_rgba(15,23,42,0.12)] ring-1 ring-black/[0.03] backdrop-blur-sm md:left-1/2 md:right-auto md:mb-2.5 md:w-[17rem] md:-translate-x-1/2 md:px-3 dark:bg-background dark:shadow-[0_18px_40px_rgba(0,0,0,0.35)] dark:ring-white/[0.04]"
            onPointerDown={(event) => event.stopPropagation()}
            onPointerUp={(event) => event.stopPropagation()}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="space-y-1.5">
              <div className="text-[10px] font-medium leading-4 text-foreground md:text-[11px]">
                {t('dock.tipTitle')}
              </div>
              <p className="text-[10px] leading-4 text-muted-foreground md:text-[11px]">
                {/* 索引夹回范围内：切换「固定底部 / 悬浮」会让步骤数变化
                    （固定模式下「长按可拖动」那一条被去掉），停在最后一步时
                    索引可能越界，直接取会渲染出 undefined。 */}
                {dockTipSteps[Math.min(dockTipStep, dockTipSteps.length - 1)]}
              </p>
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-1.5">
                  {dockTipSteps.map((_, index) => (
                    <span
                      key={index}
                      className={`h-1.5 rounded-full transition-all ${index === dockTipStep ? 'w-4 bg-foreground/75' : 'w-1.5 bg-muted-foreground/25'}`}
                    />
                  ))}
                </div>
                <div className="flex items-center gap-0.5">
                  {/* 明确的关闭入口。原实现只能靠「把几条都点完」关掉，
                      于是这条气泡会在页面上一直挂着 —— 它挂在底栏正上方，
                      而底栏能被拖到页面中部，正好压住正文。 */}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-5 rounded-full px-1 text-[10px] text-muted-foreground md:h-6 md:px-1.5 md:text-[11px]"
                    onClick={handleDismissDockTip}
                  >
                    {t('dock.tipDismiss')}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={t('dock.tipNext')}
                    title={t('dock.tipNext')}
                    className="h-5 rounded-full px-1 text-[10px] text-muted-foreground md:h-6 md:px-1.5 md:text-[11px]"
                    onClick={handleNextDockTip}
                  >
                    <ChevronRight className="size-3 md:size-3.5" />
                  </Button>
                </div>
              </div>
            </div>
          </div>
        )}
        <FloatingDock
          items={dockItems}
          desktopClassName="bg-background/70 backdrop-blur-md border border-border/40 shadow-lg shadow-black/10 dark:shadow-white/5 h-16 pb-3 px-4 gap-2"
          mobileButtonClassName="bg-background/70 backdrop-blur-md border border-border/40 shadow-lg shadow-black/10 dark:shadow-white/5 h-12 w-12"
        />
      </div>

      <AddAccountDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        onSuccess={() => {
          /* 账号页会在打开时自行刷新 */
        }}
      />
    </>
  );
}

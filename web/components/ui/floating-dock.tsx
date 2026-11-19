/**
 * Note: Use position fixed according to your needs
 * Desktop navbar is better positioned at the bottom
 * Mobile navbar is better positioned at bottom right.
 **/

import {cn} from '@/lib/utils';
import {withBasePath} from '@/lib/base-path';
import {splitByGroup} from '@/lib/dock-groups';
import {IconLayoutNavbarCollapse} from '@tabler/icons-react';
import {
  AnimatePresence,
  MotionValue,
  motion,
  useMotionValue,
  useSpring,
  useTransform,
} from 'motion/react';

import {Fragment, useRef, useState, memo, useCallback} from 'react';

export type FloatingDockItem = {
  title: string;
  icon: React.ReactNode;
  href?: string;
  onClick?: () => void;
  tooltip?: string;
  customComponent?: React.ReactNode;
  external?: boolean;
  /** 分组键：相邻两项不同时，两组之间画一条分隔线（见 `@/lib/dock-groups`）。 */
  groupKey?: string;
  /** 分组名（已翻译）。桌面端挂在分隔线上做 hover 提示，移动端直接显示。 */
  groupLabel?: string;
};

export const FloatingDock = ({
  items,
  desktopClassName,
  mobileClassName,
  mobileButtonClassName,
}: {
  items: FloatingDockItem[];
  desktopClassName?: string;
  mobileClassName?: string;
  mobileButtonClassName?: string;
}) => {
  return (
    <>
      <FloatingDockDesktop items={items} className={desktopClassName} />
      <FloatingDockMobile
        items={items}
        className={mobileClassName}
        buttonClassName={mobileButtonClassName}
      />
    </>
  );
};

const FloatingDockMobile = memo(
    ({
      items,
      className,
      buttonClassName,
    }: {
    items: FloatingDockItem[];
    className?: string;
    buttonClassName?: string;
  }) => {
      const [open, setOpen] = useState(false);

      const toggleOpen = useCallback(() => {
        setOpen((prev) => !prev);
      }, []);

      return (
        <div className={cn('relative block md:hidden', className)}>
          <AnimatePresence>
            {open && (
              <motion.div
                layoutId="nav"
                className="absolute inset-x-0 bottom-full mb-2 flex max-h-[min(70dvh,420px)] flex-col gap-1 overflow-y-auto"
              >
                {/*
                  手机端也要分组。原来这里是 `if (item.title === 'divider') return null`
                  —— 分隔线被跳过后，手机上 11 项平铺，分组语义完全消失（P1-1）。
                  现在改成按 groupKey 切，并在交界处画一条**带组名**的横线。
                */}
                {(() => {
                  const groups = splitByGroup(items);
                  const total = items.length;
                  let flatIndex = -1;
                  return groups.map((group, groupIndex) => (
                    <Fragment key={group.key || `group-${groupIndex}`}>
                      {groupIndex > 0 && (
                        <div
                          data-slot="dock-group-separator"
                          className="flex flex-col items-center gap-1 pt-1"
                        >
                          <span className="h-px w-5 bg-border" />
                          {group.label ? (
                            <span className="text-[9px] leading-none text-muted-foreground">
                              {group.label}
                            </span>
                          ) : null}
                        </div>
                      )}
                      {group.items.map((item) => {
                        flatIndex += 1;
                        const idx = flatIndex;
                        return (
                          <motion.div
                            key={item.title}
                            initial={{opacity: 0, y: 10}}
                            animate={{
                              opacity: 1,
                              y: 0,
                            }}
                            exit={{
                              opacity: 0,
                              y: 10,
                              transition: {
                                delay: idx * 0.05,
                              },
                            }}
                            transition={{delay: (total - 1 - idx) * 0.05}}
                          >
                            {item.customComponent ? (
                              <div
                                className={cn(
                                    'flex h-8 w-8 items-center justify-center rounded-full bg-gray-50 dark:bg-neutral-900',
                                    buttonClassName,
                                )}
                              >
                                {item.customComponent}
                              </div>
                            ) : item.href ? (
                            <a
                              href={withBasePath(item.href)}
                              aria-label={item.title}
                              className={cn(
                                  'flex h-8 w-8 items-center justify-center rounded-full bg-gray-50 dark:bg-neutral-900',
                                  buttonClassName,
                              )}
                              {...((item.external || item.href.startsWith('https://')) ?
                                {target: '_blank', rel: 'noopener noreferrer'} :
                                {})}
                            >
                              <div className="flex items-center justify-center">
                                {item.icon}
                              </div>
                            </a>
                          ) : (
                            <button
                              onClick={item.onClick}
                              aria-label={item.title}
                              className={cn(
                                  'flex h-8 w-8 items-center justify-center rounded-full bg-gray-50 dark:bg-neutral-900',
                                  buttonClassName,
                              )}
                            >
                              <div className="flex items-center justify-center">
                                {item.icon}
                              </div>
                            </button>
                          )}
                          </motion.div>
                        );
                      })}
                    </Fragment>
                  ));
                })()}
              </motion.div>
            )}
          </AnimatePresence>
          <button
            data-slot="dock-mobile-toggle"
            aria-label={open ? 'collapse navigation' : 'expand navigation'}
            aria-expanded={open}
            onClick={toggleOpen}
            className={cn(
                'flex h-8 w-8 items-center justify-center rounded-full bg-gray-50 dark:bg-neutral-800',
                buttonClassName,
            )}
          >
            <motion.div
              animate={{rotate: open ? 180 : 0}}
              transition={{duration: 0.3, ease: 'easeInOut'}}
            >
              <IconLayoutNavbarCollapse className="h-4 w-4 text-neutral-500 dark:text-neutral-400" />
            </motion.div>
          </button>
        </div>
      );
    },
);

FloatingDockMobile.displayName = 'FloatingDockMobile';

/**
 * 分隔线的「命中区」半宽（px）。
 *
 * 为什么要留宽、而且**不能**靠分隔线自己的 hover 事件：
 *
 * 底栏是会放大的——鼠标一动，附近的图标就从 40px 长到 70px，整个底栏随之变宽
 * （实测 725 → 787px）。底栏是 `translateX(-50%)` 居中的，于是**分隔线会从光标
 * 底下挪走**（实测挪 8~30px，而线本身只有 1px 宽 + 8px 内边距）。用
 * `onMouseEnter`/`onMouseLeave` 的结果是「组名亮一下又灭」：用户在界面上看到的
 * 是一次闪烁，而代码里看不出任何异常。
 *
 * 所以判据改成在底栏的 mousemove 里**当场量**每条分隔线的矩形，看光标落在谁的
 * 命中区里。光标不动、只有布局在动时不会再触发 mousemove，标签就留在屏幕上——
 * 这正是想要的：它是「我把鼠标放上去了」的反馈，不该被图标放大挤掉。
 */
const DIVIDER_HIT_SLOP = 10;

const FloatingDockDesktop = memo(
    ({
      items,
      className,
    }: {
    items: FloatingDockItem[];
    className?: string;
  }) => {
      const mouseX = useMotionValue(Infinity);
      const rootRef = useRef<HTMLDivElement>(null);
      const [activeDivider, setActiveDivider] = useState<number | null>(null);

      const handleMouseMove = useCallback(
          (e: React.MouseEvent) => {
            mouseX.set(e.pageX);

            const nodes =
              rootRef.current?.querySelectorAll('[data-slot=dock-group-divider]');
            let next: number | null = null;
            if (nodes) {
              for (let i = 0; i < nodes.length; i += 1) {
                const rect = nodes[i].getBoundingClientRect();
                if (e.clientX >= rect.left - DIVIDER_HIT_SLOP &&
                    e.clientX <= rect.right + DIVIDER_HIT_SLOP) {
                  next = i;
                  break;
                }
              }
            }
            // 值没变时 React 会跳过重渲染，所以这里可以放心每次 mousemove 都设。
            setActiveDivider(next);
          },
          [mouseX],
      );

      const handleMouseLeave = useCallback(() => {
        mouseX.set(Infinity);
        setActiveDivider(null);
      }, [mouseX]);

      return (
        <motion.div
          ref={rootRef}
          onMouseMove={handleMouseMove}
          onMouseLeave={handleMouseLeave}
          className={cn(
              'mx-auto hidden items-end gap-2 rounded-xl bg-gray-50 px-2 pb-2 md:flex dark:bg-neutral-900',
              className,
          )}
        >
          {(() => {
            // 按 groupKey 切组。原来的写法是「以 title === 'divider' 的哨兵条目为界
            // 切成两段」——只支持两段，而且注释里正解释着「不能写死 slice(0, 3)：
            // 写死会把多出来的那个图标静默丢掉（曾经把『密钥』挤掉过）」。
            // 改成按分组键切之后，段数由数据决定，不再有「第几段」这个概念。
            const groups = splitByGroup(items);

            return (
              <>
                {groups.map((group, index) => (
                  <Fragment key={group.key || `group-${index}`}>
                    {/* `activeDivider` 数的是**分隔线**在 DOM 里的序号，而这里只有
                        `index > 0` 的组才画线（第一组之前没有线），所以是 index - 1。 */}
                    {index > 0 &&
                      <GroupDivider label={group.label} active={activeDivider === index - 1} />}
                    <div className="flex items-end gap-2">
                      {group.items.map((item) => (
                        <IconContainer mouseX={mouseX} key={item.title} {...item} />
                      ))}
                    </div>
                  </Fragment>
                ))}
              </>
            );
          })()}
        </motion.div>
      );
    },
);

FloatingDockDesktop.displayName = 'FloatingDockDesktop';

/**
 * 组间分隔线。光标靠近时在线的上方显示组名——**这是这一批要修的那句「分隔线无标签」**：
 * 原来的竖线不带任何说明，用户只能猜它分开的是什么。
 *
 * 为什么组名走「靠近才显示」而不是常显：底栏的高度是写死的 `h-16`，图标 hover 时会从
 * 40px 放大到 70px（本来就溢出容器）。再加一行常显的组名会把底栏顶高、并和放大后的
 * 图标打架。
 *
 * ⚠️ `active` 由**父组件按光标位置算**，本组件刻意不带自己的 hover 状态——理由见
 * `DIVIDER_HIT_SLOP` 上面那段（元素会被图标放大推走）。
 */
const GroupDivider = memo(({label, active}: {label?: string; active: boolean}) => {
  return (
    <div
      data-slot="dock-group-divider"
      className="relative flex items-center justify-center px-1 mx-1 self-stretch mt-3"
    >
      <div className="w-px h-full bg-border"></div>
      <AnimatePresence>
        {active && label ? (
          <motion.div
            initial={{opacity: 0, y: 10, x: '-50%'}}
            animate={{opacity: 1, y: 0, x: '-50%'}}
            exit={{opacity: 0, y: 2, x: '-50%'}}
            className="absolute -top-8 left-1/2 w-fit whitespace-nowrap rounded-md border border-gray-200 bg-gray-100 px-2 py-0.5 text-xs text-neutral-700 dark:border-neutral-900 dark:bg-neutral-800 dark:text-white"
          >
            {label}
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  );
});

GroupDivider.displayName = 'GroupDivider';

const IconContainer = memo(
    ({
      mouseX,
      title,
      icon,
      href,
      onClick,
      tooltip,
      customComponent,
      external,
    }: FloatingDockItem & {mouseX: MotionValue}) => {
      const ref = useRef<HTMLDivElement>(null);

      const distance = useTransform(mouseX, (val) => {
        const bounds = ref.current?.getBoundingClientRect() ?? {x: 0, width: 0};

        return val - bounds.x - bounds.width / 2;
      });

      const widthTransform = useTransform(distance, [-150, 0, 150], [40, 70, 40]);
      const heightTransform = useTransform(
          distance,
          [-150, 0, 150],
          [40, 70, 40],
      );

      const widthTransformIcon = useTransform(
          distance,
          [-150, 0, 150],
          [20, 35, 20],
      );
      const heightTransformIcon = useTransform(
          distance,
          [-150, 0, 150],
          [20, 35, 20],
      );

      const width = useSpring(widthTransform, {
        mass: 0.1,
        stiffness: 150,
        damping: 12,
      });
      const height = useSpring(heightTransform, {
        mass: 0.1,
        stiffness: 150,
        damping: 12,
      });

      const widthIcon = useSpring(widthTransformIcon, {
        mass: 0.1,
        stiffness: 150,
        damping: 12,
      });
      const heightIcon = useSpring(heightTransformIcon, {
        mass: 0.1,
        stiffness: 150,
        damping: 12,
      });

      const [hovered, setHovered] = useState(false);

      const handleMouseEnter = useCallback(() => {
        setHovered(true);
      }, []);

      const handleMouseLeave = useCallback(() => {
        setHovered(false);
      }, []);

      const Element = customComponent ? 'div' : href ? 'a' : 'button';
      const elementProps = customComponent ? {} : href ?
      {
        // 原生 <a> 不经过 next/link，basePath 不会自动生效
        href: withBasePath(href),
        'aria-label': title,
        ...((external || href.startsWith('https://')) ?
            {target: '_blank', rel: 'noopener noreferrer'} :
            {}),
      } :
      {onClick, 'aria-label': title};

      return (
        <Element {...elementProps}>
          <motion.div
            ref={ref}
            style={{width, height}}
            onMouseEnter={handleMouseEnter}
            onMouseLeave={handleMouseLeave}
            className="relative flex aspect-square items-center justify-center rounded-full bg-gray-200 cursor-pointer dark:bg-neutral-800"
          >
            <AnimatePresence>
              {hovered && (
                <motion.div
                  initial={{opacity: 0, y: 10, x: '-50%'}}
                  animate={{opacity: 1, y: 0, x: '-50%'}}
                  exit={{opacity: 0, y: 2, x: '-50%'}}
                  className="absolute -top-8 left-1/2 w-fit rounded-md border border-gray-200 bg-gray-100 px-2 py-0.5 text-xs whitespace-pre text-neutral-700 dark:border-neutral-900 dark:bg-neutral-800 dark:text-white"
                >
                  {tooltip || title}
                </motion.div>
              )}
            </AnimatePresence>
            <motion.div
              style={{width: widthIcon, height: heightIcon}}
              className="flex items-center justify-center"
            >
              {customComponent || icon}
            </motion.div>
          </motion.div>
        </Element>
      );
    },
);

IconContainer.displayName = 'IconContainer';

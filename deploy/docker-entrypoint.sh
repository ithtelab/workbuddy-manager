#!/bin/sh
# WorkBuddy Manager 容器入口：把容器内进程的 uid/gid 对齐到挂载目录的属主。
#
# 为什么需要它：bind mount 进来的目录属主由宿主机决定（群晖是 admin 1026，
# 普通 Linux 常见 1000，用 root 建目录则是 0）。镜像里固定跑 10001 时，
# 启动第一步就会拿到
#
#     sqlite3.OperationalError: unable to open database file
#     RuntimeError: 无法打开数据库文件：/app/data/manager.db
#     ERROR:    Application startup failed. Exiting.
#
# 然后进入 restart: unless-stopped 的无限重启。手工 `chown -R 10001:10001 ./data`
# 能修，但那要求用户在 NAS 上开 SSH —— 对群晖/极空间这类图形化设备是不合理的
# 前置条件。（命名卷能绕开属主问题，但数据就不在宿主目录里了，很多人要能直接
# 打开、备份、改配置。）
#
# 做法沿用 linuxserver.io 的约定：PUID / PGID 环境变量。
#   · 两个都不填 → 自动取「挂载目录」的属主（零配置即可启动）
#   · 填了       → 以你给的为准（compose 里 PUID: ${PUID:-}，传空串等同未填）
#   · 探测到 0   → 说明挂载目录归 root，回落 10001，绝不以 root 运行业务进程
#
# 对齐完属主后立即降权，root 只存在于 PID 1 启动的头几毫秒。降权刻意用
# `su-exec app` 而不是 `su-exec uid:gid`：只有前者会把附加组带进业务进程，而
# 挂 docker.sock 时正需要靠附加组拿到 socket 权限。详见中段「docker.sock 的组权限」。
#
# ── 与外置 wb2api 共存的注意点 ─────────────────────────────
#
# 本项目的 wb2api（账号池服务）是独立容器，通过 /opt/workbuddy2api 与面板共享
# 配置目录（config.json、auths/）。那个目录**不归本容器独占**，所以这里默认
# **只检查不修改**它的属主 —— 一旦按本容器的 PUID 改了，另一个容器的 10001
# 就写不进去了，表现是「面板能读、wb2api 落盘失败」这种很难查的故障。
#
# 确实需要本容器去改它（例如管理端是唯一使用者），设 WB_FIX_UPSTREAM_OWNER=1。
#
# 逃生开关：WB_FIX_OWNERSHIP=0 时完全跳过本脚本，直接执行 CMD
# （用于「我就想让容器以某个显式 user: 运行」的场景）。
set -eu

if [ "${WB_FIX_OWNERSHIP:-1}" != "1" ]; then
    exec "$@"
fi

# 可能被挂载的数据目录。v2.11.x 是外置形态，面板数据在 /app/data；
# /opt/workbuddy 是更早的单根布局，留着只为兼容从那边迁过来的挂载。
MOUNT_CANDIDATES="/app/data /opt/workbuddy"

is_mount() {
    awk -v p="$1" '$2 == p { found = 1 } END { exit !found }' /proc/mounts 2>/dev/null
}

detect() {  # $1 = u | g
    for d in $MOUNT_CANDIDATES; do
        if is_mount "$d"; then
            stat -c "%$1" "$d" 2>/dev/null && return 0
        fi
    done
    echo 10001
}

PUID="${PUID:-}"
PGID="${PGID:-}"
if [ -z "$PUID" ]; then PUID="$(detect u)"; fi
if [ -z "$PGID" ]; then PGID="$(detect g)"; fi

case "$PUID$PGID" in
    *[!0-9]*)
        echo "[entrypoint] PUID/PGID 必须是数字：PUID=$PUID PGID=$PGID" >&2
        exit 1
        ;;
esac

if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
    echo "[entrypoint] 挂载目录归 root 所有，回落 10001 启动（不以 root 运行业务进程）" >&2
    echo "[entrypoint] 若确实要以 root 运行，请设 WB_FIX_OWNERSHIP=0 并在 compose 写 user: \"0:0\"" >&2
    PUID=10001
    PGID=10001
fi

warn_fail() {
    echo "[entrypoint] 警告：$1 无法 chown（NFS/SMB 挂载？），继续启动" >&2
}

needs_fix() {  # $1 = 路径；属主不是目标值时返回 0
    [ -e "$1" ] || return 1
    [ "$(stat -c %u:%g "$1" 2>/dev/null)" != "$PUID:$PGID" ]
}

if [ "$(id -u)" = "0" ]; then
    # app 用户的 uid/gid 对齐到目标值。Alpine 的 busybox 没有 usermod/groupmod，
    # 镜像里装了 shadow 包提供它们；-o 允许 uid/gid 与宿主已有账号重复。
    #
    # 这两条命令失败不致命：某些基础镜像的 shadow 版本不认 -o，或者账号被锁定。
    # 真的降不了权，后面的 su-exec 会以数字 uid 直接跑 —— 那才是最终要的效果，
    # usermod 只是让 `id app`、HOME 这类附带信息跟着对。
    if [ "$(id -g app 2>/dev/null || echo '')" != "$PGID" ]; then
        groupmod -o -g "$PGID" app 2>/dev/null || \
            echo "[entrypoint] 提示：groupmod 未生效，仍以 gid=$PGID 运行" >&2
    fi
    if [ "$(id -u app 2>/dev/null || echo '')" != "$PUID" ]; then
        usermod -o -u "$PUID" -g "$PGID" app 2>/dev/null || \
            echo "[entrypoint] 提示：usermod 未生效，仍以 uid=$PUID 运行" >&2
    fi

    # /app 是镜像内容，也是容器内更新与 pip --user 的落点。属主不对就整棵改一次：
    # 只改顶层的话，以新身份运行时改不动 10001 拥有的那些文件，更新功能会坏。
    if needs_fix /app; then
        echo "[entrypoint] 修正 /app 属主 → $PUID:$PGID" >&2
        chown -R "$PUID:$PGID" /app 2>/dev/null || warn_fail /app
    fi

    # 数据目录递归修正。必须递归：老部署里子目录可能已由上一次运行建成别的属主，
    # 只看顶层会误判「已经对了」而跳过 —— 结果 manager.db 所在目录仍是旧属主，
    # 业务进程照样 Permission denied。
    for d in /app/data /opt/workbuddy/manager /opt/workbuddy/workbuddy2api; do
        if needs_fix "$d"; then
            echo "[entrypoint] 修正 $d 属主 → $PUID:$PGID" >&2
            chown -R "$PUID:$PGID" "$d" 2>/dev/null || warn_fail "$d"
        fi
    done

    # 上游共享目录：默认只检查，不修改（见文件头「与外置 wb2api 共存」）。
    if [ -d /opt/workbuddy2api ]; then
        if [ "${WB_FIX_UPSTREAM_OWNER:-0}" = "1" ]; then
            # 显式要求时才改，且只改面板真正要写的三处，不整棵递归 ——
            # 它可能是个 git 仓库，上万个对象文件每次启动遍历不划算。
            for d in /opt/workbuddy2api /opt/workbuddy2api/auths; do
                if needs_fix "$d"; then
                    echo "[entrypoint] 修正 $d 属主 → $PUID:$PGID" >&2
                    chown -R "$PUID:$PGID" "$d" 2>/dev/null || warn_fail "$d"
                fi
            done
            if needs_fix /opt/workbuddy2api/config.json; then
                chown "$PUID:$PGID" /opt/workbuddy2api/config.json 2>/dev/null ||
                    warn_fail /opt/workbuddy2api/config.json
            fi
        elif [ ! -w /opt/workbuddy2api ]; then
            echo "[entrypoint] 注意：$PUID 对 /opt/workbuddy2api 没有写权限，面板里改线路/定时任务" >&2
            echo "[entrypoint] 会失败（提示 config.json 不可写）。两种修法：" >&2
            echo "[entrypoint]   ① 让管理端跟 wb2api 用同一个 uid，即 PUID=10001 PGID=10001（推荐）" >&2
            echo "[entrypoint]   ② 设 WB_FIX_UPSTREAM_OWNER=1 由本容器接管该目录属主" >&2
            echo "[entrypoint]      （前提：wb2api 也以同一 uid 运行，否则会把它的写入搞坏）" >&2
        fi
    fi

    # 降权后 HOME 仍是 /root，会让部分工具往不可写的位置落盘，指回 app 的 home。
    export HOME=/app

    # ── docker.sock 的组权限 ──────────────────────────────────
    #
    # 挂了 /var/run/docker.sock 也不等于就能用：它通常归 root:docker（GID 998/999），
    # 业务进程不在那个组里就是 `permission denied`，表现是面板里「账号回填」那列
    # 一直空着（它靠 `docker logs` 读上游 stdout），重载/更新上游也降级。
    #
    # 为什么不能靠 compose 的 group_add：su-exec 的源码里，**带 `:gid` 调用**会走
    #
    #     if (group && group[0] != '\0') { /* group was specified, ignore
    #                                        grouplist for setgroups later */ pw = NULL; }
    #     ... if (pw == NULL) setgroups(1, &gid);      ← 附加组被压成只剩主 gid
    #
    # 也就是 `su-exec 10001:10001` 会把 group_add 给的组全部抹掉，socket 照样 EACCES。
    # 上游原版用 `USER app`（docker 启动时就设好 user + 附加组、直接 exec CMD）没这个
    # 问题，是本脚本引入 ENTRYPOINT + su-exec 之后才有的差异。
    #
    # 解法：把 socket 的**真实 GID** 建成容器内的组并加给 app 用户，降权改用
    # `su-exec app`（走 getgrouplist 分支，app 在 /etc/group 里的组都会带上）。
    # 好处是不用用户手算 DOCKER_GID 填进 .env。
    SOCK=/var/run/docker.sock
    sock_group_ready=0
    if [ -S "$SOCK" ]; then
        SOCK_GID="$(stat -c %g "$SOCK" 2>/dev/null || echo '')"
        SOCK_GRP=""
        if [ -n "$SOCK_GID" ]; then
            SOCK_GRP="$(awk -F: -v g="$SOCK_GID" '$3 == g { print $1; exit }' /etc/group)"
            if [ -z "$SOCK_GRP" ]; then
                SOCK_GRP=docker
                # 名字可能已被别的 GID 占着，换一个再用 groupadd，否则 EEXIST。
                if awk -F: -v n="$SOCK_GRP" '$1 == n { f = 1 } END { exit !f }' /etc/group; then
                    SOCK_GRP=dockersock
                fi
                groupadd -o -g "$SOCK_GID" "$SOCK_GRP" 2>/dev/null || SOCK_GRP=""
            fi
            if [ -n "$SOCK_GRP" ] && usermod -a -G "$SOCK_GRP" app 2>/dev/null; then
                sock_group_ready=1
                echo "[entrypoint] docker.sock 的组 $SOCK_GRP(gid=$SOCK_GID) 已加给 app" >&2
            fi
        fi
        if [ "$sock_group_ready" = "0" ]; then
            echo "[entrypoint] 警告：没能把 $SOCK 的属组加给 app，容器内 docker 功能仍会失败" >&2
            echo "[entrypoint]      （账号回填、自动重载反代、一键更新上游会降级）" >&2
        fi
    fi

    # 用**用户名**而不是 uid:gid 降权：只有前者会让 su-exec 走 getgrouplist 分支，
    # 把刚加的 socket 组带进业务进程。前提是 app 的 uid/gid 确实已对齐（usermod
    # 可能因 shadow 版本或账号锁定失败）—— 否则退回数字形式，身份正确优先。
    if [ "$(id -u app 2>/dev/null || echo '')" = "$PUID" ] && \
       [ "$(id -g app 2>/dev/null || echo '')" = "$PGID" ]; then
        exec su-exec app "$@"
    fi
    echo "[entrypoint] 提示：app 未对齐到 $PUID:$PGID，退回按数字身份降权" >&2
    echo "[entrypoint]      （这样会丢掉 docker.sock 的组，docker 相关功能会降级）" >&2
    exec su-exec "$PUID:$PGID" "$@"
fi

# 已经以非 root 启动（compose 里写了 user:）：尊重它，只提示不干预。
#
# 这种形态下 docker.sock 反而是**能**用的：docker 启动容器时就把 user 与
# group_add 的附加组一起设好、直接 exec，不经过 su-exec 的 setgroups，
# 所以前提是 group_add 里给了 socket 的 GID。
if [ "$(id -u)" != "$PUID" ]; then
    echo "[entrypoint] 当前 uid=$(id -u)，与 PUID=$PUID 不同（由 docker 的 user: 决定）" >&2
fi
if [ -S /var/run/docker.sock ] && ! timeout 5 docker info >/dev/null 2>&1; then
    echo "[entrypoint] 挂了 docker.sock 但 docker info 仍失败，多为 socket 的属组没进容器：" >&2
    echo "[entrypoint]   以 user: 启动时，请在 compose 的 group_add 里给出 socket 的 GID" >&2
    echo "[entrypoint]   GID 取法：docker run --rm -v /var/run/docker.sock:/s alpine stat -c %g /s" >&2
fi
exec "$@"

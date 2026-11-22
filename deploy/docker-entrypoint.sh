#!/bin/sh
# WorkBuddy Manager 容器入口：把容器内进程的 uid/gid 对齐到挂载目录的属主。
#
# 为什么需要它：bind mount 进来的目录，属主由宿主机决定（群晖是 admin 1026，
# 普通 Linux 常见 1000）。镜像里若固定跑 10001，就会在 `DATA_DIR.mkdir()` 上拿到
#
#     PermissionError: [Errno 13] Permission denied: '/opt/workbuddy/manager'
#     ERROR:    Application startup failed. Exiting.
#
# 然后进入 restart: unless-stopped 的无限重启。手工 `chown -R 10001:10001 ./data`
# 能修，但那要求用户在 NAS 上开 SSH —— 对群晖/极空间这类图形化设备是不合理的
# 前置条件。
#
# 做法沿用 linuxserver.io 的约定：PUID / PGID 环境变量。
#   · 两个都不填 → 自动取 /opt/workbuddy 挂载目录的属主（零配置即可启动）
#   · 填了       → 以你给的为准（compose 里 PUID: ${PUID:-} 传空串等同未填）
#   · 探测到 0   → 说明目录归 root，回落 10001，绝不以 root 运行业务进程
#
# 修完属主后立即 su-exec 降权，root 只存在于 PID 1 启动的几毫秒内。
#
# 逃生开关：WB_FIX_OWNERSHIP=0 时完全跳过本脚本的属主逻辑，直接执行 CMD。
# 用于「我就想让容器以某个显式 user: 运行」的场景（例如 user: "0:0"）。
set -eu

MOUNT_ROOT=/opt/workbuddy

if [ "${WB_FIX_OWNERSHIP:-1}" != "1" ]; then
    exec "$@"
fi

detect() {  # $1 = u | g
    stat -c "%$1" "$MOUNT_ROOT" 2>/dev/null || echo 10001
}

PUID="${PUID:-}"
PGID="${PGID:-}"
[ -n "$PUID" ] || PUID="$(detect u)"
[ -n "$PGID" ] || PGID="$(detect g)"

case "$PUID$PGID" in
    *[!0-9]*)
        echo "[entrypoint] PUID/PGID 必须是数字：PUID=$PUID PGID=$PGID" >&2
        exit 1
        ;;
esac

if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
    echo "[entrypoint] $MOUNT_ROOT 归 root 所有，回落 10001 启动（不以 root 运行业务进程）" >&2
    echo "[entrypoint] 如需容器以 root 运行，请设 WB_FIX_OWNERSHIP=0 并在 compose 写 user: \"0:0\"" >&2
    PUID=10001
    PGID=10001
fi

if [ "$(id -u)" = "0" ]; then
    # app 用户的 uid/gid 对齐到目标值。Alpine 的 busybox 没有 usermod/groupmod，
    # 镜像里装了 shadow 包提供它们；-o 允许 uid/gid 与宿主已有账号重复。
    if [ "$(id -g app 2>/dev/null || echo '')" != "$PGID" ]; then
        groupmod -o -g "$PGID" app
    fi
    if [ "$(id -u app 2>/dev/null || echo '')" != "$PUID" ]; then
        usermod -o -u "$PUID" -g "$PGID" app
    fi

    # /app 是镜像内容，也是容器内更新与 pip --user 的落点。属主不对就整棵改一次：
    # 只改顶层的话，以新身份运行时改不动 10001 拥有的那些文件，更新功能会坏。
    if [ "$(stat -c %u:%g /app)" != "$PUID:$PGID" ]; then
        echo "[entrypoint] 修正 /app 属主 → $PUID:$PGID" >&2
        chown -R "$PUID:$PGID" /app 2>/dev/null || \
            echo "[entrypoint] 警告：/app 无法 chown，继续启动" >&2
    fi

    # 挂载根只改顶层（代价 O(1)）。不能对整个挂载点递归：auths/ 下可能有上千个
    # 凭据文件、库也可能上 GB，每次启动遍历一遍不合理。
    chown "$PUID:$PGID" "$MOUNT_ROOT" 2>/dev/null || \
        echo "[entrypoint] 警告：$MOUNT_ROOT 无法 chown（NFS/SMB 挂载？）" >&2

    # 两个**已知**子目录单独递归。这一步不能省：老部署可能把它们建成了 10001，
    # 之后换成 PUID=1026 时，只看顶层会认为「已经对了」而跳过 —— 结果 manager
    # 目录仍是 10001，业务进程照样 Permission denied。
    for d in "$MOUNT_ROOT/manager" "$MOUNT_ROOT/workbuddy2api" /opt/workbuddy2api; do
        [ -d "$d" ] || continue
        if [ "$(stat -c %u:%g "$d")" != "$PUID:$PGID" ]; then
            echo "[entrypoint] 修正 $d 属主 → $PUID:$PGID" >&2
            chown -R "$PUID:$PGID" "$d" 2>/dev/null || \
                echo "[entrypoint] 警告：$d 无法 chown（NFS/SMB 挂载？），继续启动" >&2
        fi
    done

    # 降权后 HOME 仍是 /root，会让部分工具往不可写的位置落盘，指回 app 的 home。
    export HOME=/app
    exec su-exec "$PUID:$PGID" "$@"
fi

# 已经以非 root 启动（compose 里写了 user:）：尊重它，只提示不干预。
if [ "$(id -u)" != "$PUID" ]; then
    echo "[entrypoint] 当前 uid=$(id -u)，与 PUID=$PUID 不同（由 docker 的 user: 决定）" >&2
fi
exec "$@"

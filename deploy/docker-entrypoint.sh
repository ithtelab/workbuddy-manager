#!/bin/sh
# WorkBuddy Manager 容器入口：对齐挂载目录属主，并在同一个容器里同时拉起
# 管理端（7864）与账号池服务（7863）。
#
# 两件事合在一个脚本里的原因：单容器模式下必须由 PID 1 同时持有两个子进程，
# 才能在 `docker stop` 时把 SIGTERM 转发给它们（否则账号池服务会被 SIGKILL，
# 正在落盘的 state.json 可能写坏）。
#
# ── 一、属主对齐 ──────────────────────────────────────────
#
# bind mount 进来的目录，属主由宿主机决定（群晖是 admin 1026，普通 Linux 常见
# 1000，用 root 建目录则是 0）。镜像里固定跑 10001 时，启动第一步
# `DATA_DIR.mkdir()` 就会拿到
#
#     PermissionError: [Errno 13] Permission denied: '/opt/workbuddy/manager'
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
# ── 二、双进程 ────────────────────────────────────────────
#
# `WB2API_EMBEDDED=1`（镜像默认）时，在降权之后先起账号池服务、再起管理端，
# 两者都以同一个 PUID 运行 —— 这正是「面板有账号、池子里 0 个」那类问题消失的
# 原因：auths/ 只有一个属主，不存在两边 uid 不一致。
#
# 设 `WB2API_EMBEDDED=0` 只跑管理端（排查用：需要外部服务已在 WB2API_BASE 上）。
#
# 逃生开关：WB_FIX_OWNERSHIP=0 时完全跳过本脚本，直接执行 CMD
# （用于「我就想让容器以某个显式 user: 运行」的场景）。
set -eu

# 脚本自身的绝对路径：降权重入时用它，避免相对路径在 su-exec 后解析不到。
SELF="$(readlink -f "$0" 2>/dev/null || echo "$0")"

# ── 内部入口：双进程监督 ──────────────────────────────────
#
# 由主流程以 `--supervise` 重入一次（降权之后）。这样 root 只存在于启动的头几
# 毫秒，业务进程与监督进程都是非 root。
#
# 是否启动内置账号池服务：默认启动（单容器部署就是唯一形态）。
# 只有显式设成 0 才不启动 —— 那是排查用（比如临时换成外部服务对比），不是常规路径。
embedded_enabled() {
    case "${WB2API_EMBEDDED:-1}" in
        0 | false | no | off) return 1 ;;
    esac
    return 0
}

supervise() {
    _backend=""
    if embedded_enabled; then
        if [ ! -x /app/upstream/wb2api ]; then
            echo "[entrypoint] 警告：镜像内没有账号池服务二进制，跳过内嵌启动" >&2
        else
            _cfg="${WB_UPSTREAM_CONFIG:-/opt/workbuddy/workbuddy2api/config.json}"
            _dir="${WB_UPSTREAM_DIR:-$(dirname "$_cfg")}"
            # 配置里的 auth_dir: "./auths"、state_file: "./data/state.json" 都是
            # **相对工作目录**的，所以必须先 cd 到配置所在目录，而不是留在 /app。
            ( cd "$_dir" && exec /app/upstream/wb2api -config "$_cfg" ) &
            _backend=$!
            echo "[entrypoint] 账号池服务已启动：pid $_backend，监听 7863，配置 $_cfg" >&2
        fi
    else
        echo "[entrypoint] WB2API_EMBEDDED=0：不启动内置账号池服务，按 WB2API_BASE=${WB2API_BASE:-} 连接外部服务" >&2
    fi

    "$@" &
    _app=$!
    echo "[entrypoint] 管理端已启动：pid $_app" >&2

    # SIGTERM/SIGINT 同时转发给两个子进程。这里**只等面板**（指定 PID），
    # 不写无参 `wait`：后者会等到所有子进程都退出，万一账号池服务不响应 TERM
    # 就会挂住整个停止流程。它不退出也没关系 —— docker 在 stop_grace_period
    # 之后会 SIGKILL 整个容器。
    _stop() {
        trap '' TERM INT
        echo "[entrypoint] 收到停止信号，正在关闭两个进程…" >&2
        [ -z "${_backend}" ] || kill -TERM "$_backend" 2>/dev/null || true
        kill -TERM "$_app" 2>/dev/null || true
        wait "$_app" 2>/dev/null || true
        exit 0
    }
    trap _stop TERM INT

    # 管理端是主进程：它退出就意味容器该退出（交回 restart 策略）。
    wait "$_app" || _rc=$?
    _rc="${_rc:-0}"
    [ -z "${_backend}" ] || kill -TERM "$_backend" 2>/dev/null || true
    wait 2>/dev/null || true
    exit "$_rc"
}

if [ "${1:-}" = "--supervise" ]; then
    shift
    supervise "$@"
    exit 0  # 走不到这里，supervise 内部已 exit
fi

if [ "${WB_FIX_OWNERSHIP:-1}" != "1" ]; then
    exec "$@"
fi

# 任务脚本用 WB2A_AUTHS 定位凭证目录（优先级：环境变量 > 脚本同级 auths/ >
# /root/... 兜底）。镜像里的脚本在 /app/upstream/scripts/，同级没有 auths，
# 不显式指定就会读到空目录 —— 表现是「成长任务里一个账号都没有」。
export WB2A_AUTHS="${WB2A_AUTHS:-${WB_AUTH_DIR:-/opt/workbuddy/workbuddy2api/auths}}"

# 数据挂载点。只有一个：/opt/workbuddy（下面分 manager/ 与 workbuddy2api/）。
# 只认**确实是挂载点**的目录：镜像里会预建同名目录（属主 10001），若不加这一层
# 判断，没挂载时也会去改属主、并据此推断出错误的 PUID。
MOUNT_CANDIDATES="/opt/workbuddy"

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

# 首次启动补齐账号池服务的 config.json / auths / data。
# 只补缺，绝不覆盖已有的 api_key、线路表与定时任务。
init_upstream_conf() {  # $1 = uid, $2 = gid（空串 = 当前身份，不改属主）
    _cfg="${WB_UPSTREAM_CONFIG:-/opt/workbuddy/workbuddy2api/config.json}"
    _dir="$(dirname "$_cfg")"
    mkdir -p "$_dir/auths" "$_dir/data" 2>/dev/null || true
    # $_dir 本身也可能是刚建出来的（宿主机上原本没有这个子目录），属主会是 root。
    if [ -n "${1:-}" ] && [ -n "${2:-}" ]; then
        chown "$1:$2" "$_dir" 2>/dev/null || warn_fail "$_dir"
    fi
    if [ ! -f "$_cfg" ] && [ -f /app/upstream/config.example.json ]; then
        if python - "$_cfg" <<'PY'
import json, secrets, sys, pathlib
dst = pathlib.Path(sys.argv[1])
cfg = json.loads(pathlib.Path('/app/upstream/config.example.json').read_text(encoding='utf-8'))
cfg['api_key'] = secrets.token_hex(16)
dst.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
PY
        then
            echo "[entrypoint] 已就地生成账号池服务配置 $_cfg（api_key 随机）" >&2
        else
            echo "[entrypoint] 生成账号池服务配置失败（继续启动，可在「设置」里补）" >&2
        fi
    fi
    if [ -n "${1:-}" ] && [ -n "${2:-}" ]; then
        chown -R "$1:$2" "$_dir/auths" "$_dir/data" 2>/dev/null || true
        if [ -f "$_cfg" ]; then
            chown "$1:$2" "$_cfg" 2>/dev/null || warn_fail "$_cfg"
        fi
    fi
}

if [ "$(id -u)" = "0" ]; then
    # app 用户的 uid/gid 对齐到目标值。
    #
    # busybox 没有 usermod/groupmod，正常路径是镜像里装的 shadow 包（见 Dockerfile
    # 的 apk add）。但这里**不能**假设它一定在：工具缺失时 `groupmod: not found`
    # 的退出码是 127，在 set -e 下会直接终止入口脚本 —— 表现为容器反复重启、
    # 日志里只有一行「groupmod: not found」，7864 根本没起来。就算不退出，uid
    # 没改成目标值也会让 passwd 里查不到这个 uid（Python 的 pwd.getpwuid 报
    # KeyError），而 chown 用的是数字 PUID，于是「属主看着对、进程身份却没对上」。
    #
    # 所以：有 shadow 工具就用它；缺失或执行失败都退回直接改写 /etc/passwd、
    # /etc/group（Alpine 这两个就是纯文本，改两个字段比装包可靠，也不怕 set -e）。
    if [ "$(id -g app 2>/dev/null || echo '')" != "$PGID" ] \
        || [ "$(id -u app 2>/dev/null || echo '')" != "$PUID" ]; then
        _aligned=0
        if command -v groupmod >/dev/null 2>&1 && command -v usermod >/dev/null 2>&1; then
            # -o 允许 uid/gid 与宿主已有账号重复（群晖/Unraid 常见）。
            groupmod -o -g "$PGID" app >/dev/null 2>&1 \
                && usermod -o -u "$PUID" -g "$PGID" app >/dev/null 2>&1 \
                && _aligned=1
        fi
        if [ "$_aligned" = "0" ]; then
            echo "[entrypoint] 改用直接改写 /etc/passwd、/etc/group 对齐 app 的 uid/gid → $PUID:$PGID" >&2
            {
                awk -F: -v u="$PUID" -v g="$PGID" 'BEGIN{OFS=":"} $1=="app"{$3=u;$4=g} {print}' \
                    /etc/passwd > /tmp/.passwd.$$ && cat /tmp/.passwd.$$ > /etc/passwd
                awk -F: -v g="$PGID" 'BEGIN{OFS=":"} $1=="app"{$3=g} {print}' \
                    /etc/group > /tmp/.group.$$ && cat /tmp/.group.$$ > /etc/group
                rm -f /tmp/.passwd.$$ /tmp/.group.$$
            } || true
        fi
        if [ "$(id -u app 2>/dev/null || echo '')" != "$PUID" ]; then
            echo "[entrypoint] 警告：app 用户 uid 未能对齐（期望 $PUID，实际 $(id -u app 2>/dev/null || echo 未知)），后续按数字 $PUID:$PGID 运行" >&2
        fi
    fi

    # /app 是镜像内容，也是容器内更新与 pip --user 的落点。属主不对就整棵改一次：
    # 只改顶层的话，以新身份运行时改不动 10001 拥有的那些文件，更新功能会坏。
    if needs_fix /app; then
        echo "[entrypoint] 修正 /app 属主 → $PUID:$PGID" >&2
        chown -R "$PUID:$PGID" /app 2>/dev/null || warn_fail /app
    fi

    # 挂载根**只改顶层、不递归**：面板要在它下面建 manager/，所以它本身必须可写；
    # 但整棵数据树递归一次很贵（账号多时 auths/ 上千个文件），下面按子目录逐个处理。
    for d in /opt/workbuddy; do
        if needs_fix "$d"; then
            echo "[entrypoint] 修正 $d 属主 → $PUID:$PGID（挂载根，不递归）" >&2
            chown "$PUID:$PGID" "$d" 2>/dev/null || warn_fail "$d"
        fi
    done

    # 数据目录递归修正。必须递归、也必须逐个看一眼：老部署里这些子目录可能已由
    # 上一次运行建成了 10001，之后换成 PUID=1026 时，只看顶层会误判「已经对了」
    # 而跳过 —— 结果 manager 目录仍是 10001，业务进程照样 Permission denied。
    for d in /opt/workbuddy/manager /opt/workbuddy/workbuddy2api; do
        if needs_fix "$d"; then
            echo "[entrypoint] 修正 $d 属主 → $PUID:$PGID" >&2
            chown -R "$PUID:$PGID" "$d" 2>/dev/null || warn_fail "$d"
        fi
    done

    # 放在属主修正之后：新建的文件才不会落成 root 所有（否则面板写不动配置、
    # 账号池服务也读不了账号）。
    init_upstream_conf "$PUID" "$PGID"

    # 降权后 HOME 仍是 /root，会让部分工具往不可写的位置落盘，指回 app 的 home。
    export HOME=/app
    exec su-exec "$PUID:$PGID" "$SELF" --supervise "$@"
fi

# 已经以非 root 启动（compose 里写了 user:）：尊重它，只提示不干预。
if [ "$(id -u)" != "$PUID" ]; then
    echo "[entrypoint] 当前 uid=$(id -u)，与 PUID=$PUID 不同（由 docker 的 user: 决定）" >&2
fi
init_upstream_conf "" ""
export HOME="${HOME:-/app}"
exec "$SELF" --supervise "$@"

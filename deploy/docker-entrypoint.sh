#!/bin/sh
# WorkBuddy Manager 容器入口：把容器内进程的 uid/gid 对齐到挂载目录的属主。
#
# 为什么需要它：bind mount 进来的目录，属主由宿主机决定（群晖是 admin 1026，
# 普通 Linux 常见 1000，用 root 建目录则是 0）。镜像里固定跑 10001 时，启动
# 第一步 `DATA_DIR.mkdir()` 就会拿到
#
#     PermissionError: [Errno 13] Permission denied: '/app/data'
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
# 修完属主后立即 su-exec 降权，root 只存在于 PID 1 启动的头几毫秒。
#
# 逃生开关：WB_FIX_OWNERSHIP=0 时跳过**属主修正**，直接以当前身份执行 CMD
# （用于「我就想让容器以某个显式 user: 运行」的场景）。
#
# 它只关掉 chown 那一段，**不跳过**首启时兜底生成上游 config.json ——
# 那件事和属主无关，跳过的话上游容器会因为读不到配置反复重启，而日志里
# 只有上游的报错，看不出是管理端少走了一步。改不了属主就按当前用户建文件。
set -eu

# 可能被挂载的目录。不同部署的挂载布局不同：默认是 /app/data + /opt/workbuddy2api，
# 也有人把两者收进同一个根（/opt/workbuddy）。
# 只认**确实是挂载点**的目录：镜像里会预建同名目录（属主 10001），若不加这一层
# 判断，没挂载时也会去改属主、并据此推断出错误的 PUID。
MOUNT_CANDIDATES="/app/data /opt/workbuddy /opt/workbuddy2api"

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

# 上游配置与目录的首次初始化。$1/$2 = 目标 uid/gid（留空则不改属主）。
#
# 为什么要做：上游容器挂的是同一个数据目录下的 config.json。该文件不存在时，
# docker 会把 bind mount 的源**建成目录**，上游随后报「config.json 是目录」而
# 起不来 —— 此前只能由用户先手工备一份配置绕开（NAS 用户基本做不到）。镜像里
# 带了上游的 config.example.json（构建期从上游镜像取），于是这里就地生成：
# 只补一个随机 api_key，其余保持上游默认值。
#
# 只在这三样**不存在**时创建，绝不覆盖：config.json 里有 api_key、线路表、
# 定时任务；auths/ 与 data/ 里是账号凭据与池子状态。
init_upstream_conf() {  # $1 = uid, $2 = gid
    _cfg="${WB_UPSTREAM_CONFIG:-/opt/workbuddy2api/config.json}"
    _dir="$(dirname "$_cfg")"
    mkdir -p "$_dir/auths" "$_dir/data" 2>/dev/null || true
    if [ ! -f "$_cfg" ] && [ -f /app/upstream/config.example.json ]; then
        if python - "$_cfg" <<'PY'
import json, os, secrets, sys, pathlib
dst = pathlib.Path(sys.argv[1])
cfg = json.loads(pathlib.Path('/app/upstream/config.example.json').read_text(encoding='utf-8'))
cfg['api_key'] = secrets.token_hex(16)
dst.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
# 这个文件里有 api_key，而它落在**宿主挂载目录**里 —— 同机其它用户默认能读。
# 0600 是唯一合理权限；写失败不算致命（容器仍能用），但要让用户看到。
try:
    os.chmod(dst, 0o600)
except OSError as exc:
    sys.stderr.write('[entrypoint] 警告：无法把 %s 权限设为 0600（%s）\n' % (dst, exc))
PY
        then
            echo "[entrypoint] 已就地生成上游配置 $_cfg（api_key 随机）" >&2
        else
            echo "[entrypoint] 生成上游配置失败（继续启动，可在「设置」里补）" >&2
        fi
    fi
    if [ -n "${1:-}" ] && [ -n "${2:-}" ]; then
        chown -R "$1:$2" "$_dir/auths" "$_dir/data" 2>/dev/null || true
        if [ -f "$_cfg" ]; then
            chown "$1:$2" "$_cfg" 2>/dev/null || warn_fail "$_cfg"
        fi
    fi
}

needs_fix() {  # $1 = 路径；属主不是目标值时返回 0
    [ -e "$1" ] || return 1
    [ "$(stat -c %u:%g "$1" 2>/dev/null)" != "$PUID:$PGID" ]
}

if [ "${WB_FIX_OWNERSHIP:-1}" = "1" ] && [ "$(id -u)" = "0" ]; then
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
    if needs_fix /app; then
        echo "[entrypoint] 修正 /app 属主 → $PUID:$PGID" >&2
        chown -R "$PUID:$PGID" /app 2>/dev/null || warn_fail /app
    fi

    # 数据目录递归修正。必须递归、也必须逐个看一眼：老部署里这些子目录可能已由
    # 上一次运行建成了 10001，之后换成 PUID=1026 时，只看顶层会误判「已经对了」
    # 而跳过 —— 结果 manager 目录仍是 10001，业务进程照样 Permission denied。
    for d in /app/data /opt/workbuddy/manager /opt/workbuddy/workbuddy2api; do
        if needs_fix "$d"; then
            echo "[entrypoint] 修正 $d 属主 → $PUID:$PGID" >&2
            chown -R "$PUID:$PGID" "$d" 2>/dev/null || warn_fail "$d"
        fi
    done

    # 上游仓库目录只改顶层 + auths/ + config.json。
    # 不整棵递归：它是个 git 仓库，可能有上万个对象文件，每次启动遍历不划算；
    # 面板真正要写的就是这两处（auths 落凭据、config.json 存线路与定时任务）。
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

    # 首次启动补齐上游的 config.json / auths / data（放在属主修正之后，新建的
    # 文件才不会落成 root 所有——否则面板写不动配置、上游也读不了账号）。
    init_upstream_conf "$PUID" "$PGID"

    # 降权后 HOME 仍是 /root，会让部分工具往不可写的位置落盘，指回 app 的 home。
    export HOME=/app
    exec su-exec "$PUID:$PGID" "$@"
fi

# 走到这里的两种情况：
#   · 容器不是以 root 启动（compose 里写了 user:）—— 尊重它，只提示不干预；
#   · 设了 WB_FIX_OWNERSHIP=0 —— 用户显式接管身份，跳过属主修正。
if [ "$(id -u)" != "$PUID" ]; then
    echo "[entrypoint] 当前 uid=$(id -u)，与 PUID=$PUID 不同（由 docker 的 user: 或 WB_FIX_OWNERSHIP=0 决定）；跳过属主修正" >&2
fi
# 两条路径都仍要补配置：改不了属主就只建文件（属主自然是当前用户）。
# 这一步不能省 —— 少了它上游容器会因读不到 config.json 反复重启，而日志里
# 只有上游的报错，看不出是管理端跳过了生成。
init_upstream_conf "" ""
exec "$@"

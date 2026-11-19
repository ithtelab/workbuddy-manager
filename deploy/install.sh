#!/usr/bin/env bash
# ============================================================
# WorkBuddy Manager 一键部署脚本
#
# 管理端与上游 workbuddy2api（账号池 / OpenAI 兼容接口）作为一个安装单元：
# 上游源码由本项目维护，随管理端 Release 包分发，不再单独 clone 外部仓库。
#
# 用法：
#   sudo bash deploy/install.sh                # 完整安装（含上游）
#   sudo bash deploy/install.sh --skip-upstream  # 已自备 workbuddy2api
#   sudo bash APP_DIR=/opt/custom bash deploy/install.sh
#
# 上游源码从哪来（按优先级）：
#   1) 发布包里自带的 upstream/             官方 Release 已内嵌，离线可装
#   2) UPSTREAM_SRC=<本地目录或 tar.gz>     用你自己那份源码
#   3) 目标目录里已有完整源码               老部署复用
#
# 说明：workbuddy2api 的账号登录（扫码）是交互式的，无法自动化；
#       安装完成后通过本管理端的「添加账号」扫码即可。
# ============================================================
set -euo pipefail

# ── 可配置项（均可用环境变量覆盖）─────────────────────────
APP_DIR="${APP_DIR:-/opt/workbuddy-manager}"
UPSTREAM_DIR="${UPSTREAM_DIR:-/opt/workbuddy2api}"
UPSTREAM_SRC="${UPSTREAM_SRC:-}"
UPSTREAM_PORT="${UPSTREAM_PORT:-7863}"
MANAGER_PORT="${MANAGER_PORT:-7864}"
PY="${PY:-/usr/bin/python3}"
SKIP_UPSTREAM=0

for arg in "$@"; do
  case "$arg" in
    --skip-upstream) SKIP_UPSTREAM=1 ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

# ── 输出helpers ───────────────────────────────────────────
info()  { printf '\033[1;34m[·]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }
step()  { printf '\n\033[1;36m══ %s\033[0m\n' "$*"; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── 0. 预检 ───────────────────────────────────────────────
step "0/4 环境预检"

[ "$(id -u)" -eq 0 ] || die "请用 root 运行（sudo bash deploy/install.sh）"

# APP_DIR 会被 `chown -R` 交给服务账号（见第 2 步）。指到 /opt、/home 这类
# 公共目录会把无关文件的所有权一起改掉，所以先拒绝明显过宽的路径。
case "$APP_DIR" in
  /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
    die "APP_DIR=${APP_DIR} 太宽（chown -R 会波及整个目录）；请用 /opt/workbuddy-manager 这类专用目录"
    ;;
esac
case "$APP_DIR" in
  /*) ;;
  *) die "APP_DIR 必须是绝对路径（当前 ${APP_DIR}）" ;;
esac
case "$UPSTREAM_DIR" in
  /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
    die "UPSTREAM_DIR=${UPSTREAM_DIR} 太宽（chown -R 会波及整个目录）；请用 /opt/workbuddy2api 这类专用目录"
    ;;
esac
case "$UPSTREAM_DIR" in
  /*) ;;
  *) die "UPSTREAM_DIR 必须是绝对路径（当前 ${UPSTREAM_DIR}）" ;;
esac

command -v "$PY" >/dev/null 2>&1 || die "未找到 python3（$PY）"
PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
"$PY" -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' \
  || die "Python 版本过低（需 ≥3.9，当前 ${PYVER}）"
ok "Python ${PYVER}"

# 安装包摘要提示（不判断、不阻断）────────────────────────────
# 本脚本运行时包**已经在磁盘上**了，所以它拦不住「装了一个别处来的包」——
# 想核对就在解压前跑：bash deploy/verify-release.sh <包> <Releases 页面的 sha256>。
# 这里只把同目录里的官方包摘要打出来，方便和 Releases 页面对一眼。
for _pkg in $(find . -maxdepth 1 -name 'workbuddy-manager-*.tar.gz' -print 2>/dev/null); do
  if command -v sha256sum >/dev/null 2>&1; then
    info "$(basename "${_pkg}")  sha256=$(sha256sum "${_pkg}" | cut -d' ' -f1)"
  fi
done

HAVE_DOCKER=0
if command -v docker >/dev/null 2>&1; then
  HAVE_DOCKER=1
  DOCKER_VER="$(docker --version | awk '{print $3}' | tr -d ,)"
  ok "Docker ${DOCKER_VER}"
  # 优先 compose v2，退化到 docker-compose v1
  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    die "未找到 docker compose（请安装 docker-compose-plugin）"
  fi
  ok "Compose: ${COMPOSE}"
else
  warn "未检测到 Docker"
fi

check_port() {
  local port="$1" name="$2"
  if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${port}\b"; then
    warn "端口 ${port}（${name}）已被占用，稍后可能冲突"
  fi
}
check_port "$UPSTREAM_PORT" "上游 workbuddy2api"
check_port "$MANAGER_PORT" "管理端"

# ── 1. 安装上游 workbuddy2api ─────────────────────────────
step "1/4 上游 workbuddy2api"

if [ "$SKIP_UPSTREAM" -eq 1 ]; then
  info "按参数要求跳过（--skip-upstream）"
elif [ "$HAVE_DOCKER" -eq 0 ]; then
  warn "无 Docker，跳过上游安装。请自行部署 workbuddy2api 于 :${UPSTREAM_PORT}"
elif [ -f "${UPSTREAM_DIR}/config.json" ]; then
  ok "已存在上游部署（${UPSTREAM_DIR}），保留现有配置与账号"
  if [ -d "${UPSTREAM_DIR}/.git" ]; then
    info "如需升级上游：cd ${UPSTREAM_DIR} && git pull && ${COMPOSE} up -d --build"
  fi
  # 确保容器在运行
  ( cd "$UPSTREAM_DIR" && $COMPOSE up -d ) >/dev/null 2>&1 || \
    warn "上游容器启动失败，请检查：cd ${UPSTREAM_DIR} && ${COMPOSE} logs"
else
  info "未检测到上游部署，开始安装到 ${UPSTREAM_DIR}"

  # 上游源码的来源，按优先级：
  #   1) UPSTREAM_SRC 显式指定（目录 / .tar.gz / .zip）
  #   2) 发布包内自带的 upstream/（唯一官方来源）
  #   3) 目标目录里已有完整的上游源码（老部署复用）
  # 不再从外部 git 仓库克隆：上游已由本项目维护，随管理端 Release 分发。
  if [ -z "$UPSTREAM_SRC" ]; then
    BUNDLED="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/upstream"
    if [ -f "${BUNDLED}/docker-compose.yml" ]; then
      UPSTREAM_SRC="$BUNDLED"
      info "使用发布包内自带的上游源码：${BUNDLED}"
    elif [ -f "${UPSTREAM_DIR}/docker-compose.yml" ]; then
      info "沿用 ${UPSTREAM_DIR} 里已有的上游源码"
    else
      die "发布包里没有 upstream/，目标目录也没有上游源码。

  请使用官方 Release 包（内含 upstream/）重跑，或显式指定源码：

    sudo UPSTREAM_SRC=/path/to/upstream bash deploy/install.sh

  已经手工装好上游、只想跳过这一步时用：

    sudo bash deploy/install.sh --skip-upstream"
    fi
  fi

  if [ -n "$UPSTREAM_SRC" ]; then
    [ -e "$UPSTREAM_SRC" ] || die "UPSTREAM_SRC 指向的路径不存在：${UPSTREAM_SRC}"
    info "使用本地源码：${UPSTREAM_SRC}"
    mkdir -p "$UPSTREAM_DIR"
    if [ "$(cd "$UPSTREAM_SRC" 2>/dev/null && pwd -P)" = "$(cd "$UPSTREAM_DIR" && pwd -P)" ]; then
      # 源码本来就在目标目录（典型情形：复用已有的 /opt/workbuddy2api）
      info "源码已在 ${UPSTREAM_DIR}，无需复制"
    else
      case "$UPSTREAM_SRC" in
        *.tar.gz | *.tgz)
          tar -xzf "$UPSTREAM_SRC" -C "$UPSTREAM_DIR" --strip-components=1 ;;
        *.zip)
          command -v unzip >/dev/null 2>&1 || die "解压 .zip 需要 unzip，或改用 .tar.gz"
          tmp="$(mktemp -d)"
          unzip -q -o "$UPSTREAM_SRC" -d "$tmp"
          # 压缩包里通常有一个顶层目录，把它**连同点文件**一起搬进去
          inner="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -1)"
          src="$tmp"
          [ -n "$inner" ] && src="$inner"
          cp -a "$src"/. "$UPSTREAM_DIR"/
          rm -rf "$tmp" ;;
        *)
          [ -d "$UPSTREAM_SRC" ] || die "UPSTREAM_SRC 既不是目录也不是 .tar.gz/.zip：${UPSTREAM_SRC}"
          cp -a "$UPSTREAM_SRC"/. "$UPSTREAM_DIR"/ ;;
      esac
    fi
    [ -f "${UPSTREAM_DIR}/docker-compose.yml" ] || \
      die "本地源码里没有 docker-compose.yml，确认 UPSTREAM_SRC 指的是上游根目录"
  fi

  cd "$UPSTREAM_DIR"

  # 上游端口只绑回环：面板与它在同一台机器（或同一 docker 网络）上访问，
  # 不需要对公网暴露 7863。compose 模板写的是 "7863:7863"（所有网卡），这里
  # 统一改成 "127.0.0.1:7863:7863"，与更新器的端口收敛（enforce_local_bind）
  # 同一口径。改不动时不阻断安装，但会明说。
  if [ -f "${UPSTREAM_DIR}/docker-compose.yml" ]; then
    sed -i "s#- \"${UPSTREAM_PORT}:${UPSTREAM_PORT}\"#- \"127.0.0.1:${UPSTREAM_PORT}:${UPSTREAM_PORT}\"#" "${UPSTREAM_DIR}/docker-compose.yml" 2>/dev/null || true
    sed -i "s#- ${UPSTREAM_PORT}:${UPSTREAM_PORT}#- \"127.0.0.1:${UPSTREAM_PORT}:${UPSTREAM_PORT}\"#" "${UPSTREAM_DIR}/docker-compose.yml" 2>/dev/null || true
    if grep -q "127.0.0.1:${UPSTREAM_PORT}:${UPSTREAM_PORT}" "${UPSTREAM_DIR}/docker-compose.yml"; then
      ok "上游端口已收敛为 127.0.0.1:${UPSTREAM_PORT}（不对公网暴露）"
    else
      warn "未能自动收敛上游端口，请手工把 compose 的端口映射改为 127.0.0.1:${UPSTREAM_PORT}:${UPSTREAM_PORT}"
    fi
  fi

  # 生成配置：绝不覆盖已有 config.json（含 api_key 与账号状态）
  if [ ! -f config.json ]; then
    if [ -f config.example.json ]; then
      cp config.example.json config.json
    else
      die "缺少 config.example.json，无法生成配置"
    fi
    # 生成强随机 api_key
    API_KEY="$("$PY" -c 'import secrets;print(secrets.token_hex(16))')"
    "$PY" - "$API_KEY" <<'PYEOF'
import json, sys
p = 'config.json'
cfg = json.load(open(p, encoding='utf-8'))
cfg['api_key'] = sys.argv[1]
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
PYEOF
    ok "已生成配置（api_key 随机）"
  else
    info "已存在 config.json，保留不动"
  fi

  # 容器内以 uid 10001 运行，bind-mount 的目录需归属该 uid，否则无写权限
  mkdir -p auths data
  chown -R 10001:10001 auths data
  ok "账号目录已就绪（属主 10001）"

  info "构建并启动上游容器（首次构建需数分钟）"
  $COMPOSE up -d --build

  info "等待上游就绪…"
  for i in $(seq 1 60); do
    if curl -sf -m 3 "http://127.0.0.1:${UPSTREAM_PORT}/healthz" >/dev/null 2>&1; then
      ok "上游已就绪（http://127.0.0.1:${UPSTREAM_PORT}）"
      break
    fi
    [ "$i" -eq 60 ] && warn "等待超时，请查看：cd ${UPSTREAM_DIR} && ${COMPOSE} logs"
    sleep 2
  done
fi

# ── 2. 安装管理端 ─────────────────────────────────────────
step "2/4 管理端 WorkBuddy Manager"

mkdir -p "${APP_DIR}/data"

# 服务账号：uid 10001 与容器形态对齐（上游容器内的进程就是这个 uid，双方才能
# 共享 auths/ 与 config.json 的写权限）。uid 被占时退回 root 运行并明说，不硬凑
# 别的 uid（那会让管理端写不了上游目录，问题更隐蔽）。
RUN_USER=""
if id workbuddy >/dev/null 2>&1; then
  RUN_USER=workbuddy
  ok "服务账号已存在：workbuddy"
elif getent passwd 10001 >/dev/null 2>&1; then
  warn "uid 10001 已被用户 $(id -un 10001) 占用，管理端将继续以 root 运行（沙箱降级）"
else
  useradd --system --uid 10001 --user-group \
          --home-dir "${APP_DIR}" --shell /usr/sbin/nologin workbuddy
  RUN_USER=workbuddy
  ok "已创建服务账号 workbuddy（uid 10001）"
fi

if [ "${SRC_DIR}" != "${APP_DIR}" ]; then
  info "同步代码到 ${APP_DIR}"
  cp -r "${SRC_DIR}/server" "${APP_DIR}/"
  cp -r "${SRC_DIR}/deploy" "${APP_DIR}/"
  cp "${SRC_DIR}/.env.example" "${APP_DIR}/" 2>/dev/null || true
  # 文档：界面的「更新日志」页直接读它（server/ 内另有一份副本兜底）
  cp "${SRC_DIR}/CHANGELOG.md" "${APP_DIR}/" 2>/dev/null || true
  cp "${SRC_DIR}/README.md" "${APP_DIR}/" 2>/dev/null || true
  cp "${SRC_DIR}/README.en.md" "${APP_DIR}/" 2>/dev/null || true
fi

# 静态前端：优先用发布包内的 web/out；否则尝试本地构建
mkdir -p "${APP_DIR}/web"
if [ -f "${APP_DIR}/web/out/index.html" ]; then
  ok "已包含构建好的前端（web/out）"
elif [ -d "${SRC_DIR}/web/out" ]; then
  cp -r "${SRC_DIR}/web/out" "${APP_DIR}/web/"
  ok "已复制前端产物"
elif [ -f "${SRC_DIR}/web/package.json" ]; then
  warn "未找到前端产物，尝试本地构建"
  if command -v npm >/dev/null 2>&1; then
    ( cd "${SRC_DIR}/web" && npm ci && npm run build:export )
    cp -r "${SRC_DIR}/web/out" "${APP_DIR}/web/"
    ok "前端构建完成"
  else
    die "需要 Node.js 构建前端；或下载 Release 包（内含已构建的 web/out）"
  fi
else
  die "源码目录里既没有前端产物、也没有前端源码（${SRC_DIR}/web 不存在）——请确认源码完整，或改用 Release 包"
fi

# 写入版本标记（供界面显示当前版本）
if [ -f "${APP_DIR}/.version" ]; then
  ok "版本标记：$(cat "${APP_DIR}/.version")"
elif [ -f "${SRC_DIR}/.version" ]; then
  cp "${SRC_DIR}/.version" "${APP_DIR}/.version"
fi

info "安装 Python 依赖"
"$PY" -m venv "${APP_DIR}/venv" 2>/dev/null || true
if [ -x "${APP_DIR}/venv/bin/pip" ]; then
  "${APP_DIR}/venv/bin/pip" install -q --upgrade pip
  "${APP_DIR}/venv/bin/pip" install -q -r "${APP_DIR}/server/requirements.txt"
  RT_PY="${APP_DIR}/venv/bin/python"
  ok "依赖已装入 venv"
else
  "$PY" -m pip install -q --break-system-packages -r "${APP_DIR}/server/requirements.txt" 2>/dev/null \
    || "$PY" -m pip install -q -r "${APP_DIR}/server/requirements.txt"
  RT_PY="$PY"
  ok "依赖已装入系统 Python"
fi

info "注册 systemd 服务"
sed -e "s#/opt/workbuddy-manager#${APP_DIR}#g" \
    -e "s#--port 7864#--port ${MANAGER_PORT}#g" \
    -e "s#WB_MANAGER_PORT=7864#WB_MANAGER_PORT=${MANAGER_PORT}#g" \
    -e "s#WB2API_BASE=http://127.0.0.1:7863#WB2API_BASE=http://127.0.0.1:${UPSTREAM_PORT}#g" \
    -e "s#WB_AUTH_DIR=/opt/workbuddy2api/auths#WB_AUTH_DIR=${UPSTREAM_DIR}/auths#g" \
    -e "s#WB_UPSTREAM_CONFIG=/opt/workbuddy2api/config.json#WB_UPSTREAM_CONFIG=${UPSTREAM_DIR}/config.json#g" \
    "${APP_DIR}/deploy/workbuddy-web.service" > /etc/systemd/system/workbuddy-web.service

# 若 venv 可用，改写 ExecStart 指向 venv（监听地址维持模板里的 127.0.0.1：
# 公网访问一律经反向代理，直接暴露管理面是被打穿的常见入口）
if [ "$RT_PY" != "$PY" ]; then
  sed -i "s#^ExecStart=.*#ExecStart=${APP_DIR}/venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port ${MANAGER_PORT}#" \
    /etc/systemd/system/workbuddy-web.service
fi

# 降权与 docker 组：
#   · root 回退（uid 冲突）时，去掉 unit 里的 User/Group 行，服务才能启动；
#   · 上游为容器形态时管理端要访问 docker.sock（docker 组等同宿主 root，
#     native 形态的用户可用 WBM_NO_DOCKER_GROUP=1 跳过，install 会顺手删掉
#     unit 里的 SupplementaryGroups 行）。
if [ -z "$RUN_USER" ]; then
  sed -i '/^User=/d;/^Group=/d;/^SupplementaryGroups=/d' /etc/systemd/system/workbuddy-web.service
  warn "服务以 root 运行：建议释放 uid 10001 后重跑本脚本"
elif [ "${WBM_NO_DOCKER_GROUP:-0}" = "1" ]; then
  sed -i '/^SupplementaryGroups=/d' /etc/systemd/system/workbuddy-web.service
  info "按要求未加入 docker 组：容器形态上游的「重载 / 更新」界面功能将不可用"
elif getent group docker >/dev/null 2>&1; then
  usermod -aG docker "$RUN_USER"
  warn "已将 ${RUN_USER} 加入 docker 组（等同宿主 root，仅用于操作上游容器）"
else
  sed -i '/^SupplementaryGroups=/d' /etc/systemd/system/workbuddy-web.service
  warn "宿主无 docker 组（native 部署？），已去掉 SupplementaryGroups"
fi

# 目录属主交给服务账号：代码只读即可，data/ 要可写。
# 上游目录也要交给它：面板的「一键更新」会把新源码写进 UPSTREAM_DIR
# （auths/data 在步骤 1 已 chown 为 10001，与服务账号同一 uid，不受影响）。
if [ -n "$RUN_USER" ]; then
  chown -R "$RUN_USER:$RUN_USER" "${APP_DIR}"
  if [ "$SKIP_UPSTREAM" -eq 0 ] && [ -d "${UPSTREAM_DIR}" ]; then
    chown -R "$RUN_USER:$RUN_USER" "${UPSTREAM_DIR}"
  fi
  [ -f "${UPSTREAM_DIR}/config.json" ] && \
    chown "$RUN_USER:$RUN_USER" "${UPSTREAM_DIR}/config.json" 2>/dev/null || true
fi

# polkit 授权：面板「一键更新」在宿主形态下由服务进程执行
# `systemctl restart workbuddy-web`，非 root 需要显式授权。只放行这一个
# unit 的管理动作，不给任何其他权限。没有 polkit 的精简系统不写
# （那种环境一键更新会在重启一步提示手动执行，新代码本身已就位）。
if [ -n "$RUN_USER" ] && [ -d /etc/polkit-1/rules.d ]; then
  cat > /etc/polkit-1/rules.d/49-workbuddy-web.rules <<'POLKIT'
// WorkBuddy Manager：允许服务账号重启自己的 unit（见 deploy/install.sh）。
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        subject.user == "workbuddy") {
        var unit = action.lookup("unit");
        if (unit == "workbuddy-web.service") {
            return polkit.Result.YES;
        }
    }
});
POLKIT
  ok "已写入 polkit 规则：${RUN_USER} 可重启 workbuddy-web.service（一键更新需要）"
fi

systemctl daemon-reload
systemctl enable --now workbuddy-web >/dev/null 2>&1

# 等初始密码那几行真正出现在日志里，而不是死等固定秒数。
#
# 之前是 `sleep 3` 然后直接 grep（issue #179）。3 秒是拍的：慢 VPS 上 uvicorn
# import 完 FastAPI/httpx 常常要更久，那时bootstrap_users() 还没跑，grep 抓空
# → 安装完成页只能打印一句「见下方命令」，而那条补救命令翻几屏日志也已经
# 过去了，用户就此卡在登录页外面。
#
# 改成有上限的轮询：等到就等到，最多 ADMIN_PWD_WAIT 秒（默认 30）后放弃 ——
# 首次启动本来就该几秒内出结果，30 秒还没出说明是别的问题，不该无限等下去。
ADMIN_PWD_WAIT="${ADMIN_PWD_WAIT:-30}"
_admin_pwd_log() {
  journalctl -u workbuddy-web --no-pager --since "-${ADMIN_PWD_WAIT}s" 2>/dev/null \
    | grep -A3 '初始管理员' | tail -n +2 | head -3
}
_admin_pwd=""
for _i in $(seq 1 "$ADMIN_PWD_WAIT"); do
  _admin_pwd="$(_admin_pwd_log || true)"
  [ -n "$_admin_pwd" ] && break
  systemctl is-active --quiet workbuddy-web || break   # 服务已挂，等下去也没意义
  sleep 1
done

systemctl is-active --quiet workbuddy-web && ok "服务已启动" || {
  warn "服务未正常运行，最近日志："
  journalctl -u workbuddy-web -n 20 --no-pager || true
}

# ── 3. 验证 ───────────────────────────────────────────────
step "3/4 验证"

if curl -sf -m 5 "http://127.0.0.1:${MANAGER_PORT}/api/healthz" >/dev/null 2>&1; then
  ok "管理端 http://127.0.0.1:${MANAGER_PORT} 可访问"
else
  warn "管理端未响应，请查看：journalctl -u workbuddy-web -n 50"
fi

if curl -sf -m 5 "http://127.0.0.1:${MANAGER_PORT}/healthz" >/dev/null 2>&1; then
  ok "已连通上游（$(curl -s -m 5 http://127.0.0.1:${MANAGER_PORT}/healthz)）"
else
  warn "管理端暂时读不到上游状态；确认上游容器在运行后刷新页面即可"
fi

# ── 4. 完成 ───────────────────────────────────────────────
step "4/4 完成"

# 优先读服务端落盘的那份（0600），journal 只作备份来源。
#
# 落盘文件是服务端 bootstrap 时写的，与「日志被轮转 / grep 竞态 / 用户翻不过
# 日志」这些全都无关 —— 正是 issue #179 里用户彻底看不到密码的原因。
INITIAL_PW_FILE="${APP_DIR}/data/initial-admin-password.txt"
ADMIN_PWD_LINE="$_admin_pwd"
if [ -z "$ADMIN_PWD_LINE" ] && [ -r "$INITIAL_PW_FILE" ]; then
  _pw="$(tr -d '\r\n' < "$INITIAL_PW_FILE")"
  [ -n "$_pw" ] && ADMIN_PWD_LINE="  用户名: admin
  密码  : ${_pw}"
fi

cat <<EOF

  ┌──────────────────────────────────────────────────────┐
  │  部署完成                                            │
  └──────────────────────────────────────────────────────┘

  管理端      http://127.0.0.1:${MANAGER_PORT}   （仅监听 127.0.0.1；公网访问请走反向代理）
  上游        http://127.0.0.1:${UPSTREAM_PORT}   （workbuddy2api，仅监听 127.0.0.1）
  配置目录    ${APP_DIR}
  账号目录    ${UPSTREAM_DIR}/auths
EOF

if [ -n "$ADMIN_PWD_LINE" ]; then
  echo ""
  echo "  初始管理员密码（仅首次生成）："
  echo "$ADMIN_PWD_LINE" | sed 's/^/    /'
else
  echo ""
  echo "  登录账号：admin"
  echo "  初始密码由服务端随机生成，只打印一次。三个地方都能取回："
  echo "    cat ${INITIAL_PW_FILE}"
  echo "    journalctl -u workbuddy-web | grep -A3 '初始管理员'"
  echo "  登录后请立即在「设置 → 管理用户」里改掉它（改完这份明文会自动删除）。"
fi

cat <<EOF

  下一步：
    1. 浏览器打开管理端（公网访问请先配置 HTTPS 反向代理，见 deploy/README.md）
    2. 登录后进入「账号」→「添加账号」，用微信 / QQ 扫码授权
    3. 授权后自动签到并纳管，即可在「密钥」页创建调用密钥

  常用命令：
    journalctl -u workbuddy-web -f          # 管理端日志
    cd ${UPSTREAM_DIR} && ${COMPOSE} logs -f  # 上游日志
    systemctl restart workbuddy-web         # 重启管理端
    cd ${UPSTREAM_DIR} && ${COMPOSE} restart  # 重启上游

EOF

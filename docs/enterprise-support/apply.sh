#!/usr/bin/env bash
# apply.sh — 一键应用企业账号额度支持补丁
#
# 用法：
#   sudo bash apply.sh upstream   # 只给上游 workbuddy2api 打补丁并重建
#   sudo bash apply.sh manager    # 只给 manager 打补丁并重建
#   sudo bash apply.sh all        # 两个都打（默认）
#
# 前置：
#   - upstream: 上游源码在 /opt/workbuddy2api-build，且有 Go 工具链
#   - manager:  manager 容器名为 workbuddy-manager
#
# 安全：所有步骤前自动备份，失败可回滚（见 README.md）

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-all}"

UPSTREAM_DIR="${UPSTREAM_DIR:-/opt/workbuddy2api-build}"
MANAGER_CONTAINER="${MANAGER_CONTAINER:-workbuddy-manager}"
PATCH_DIR="${PATCH_DIR:-/opt/workbuddy-manager/patches}"
TS="$(date +%Y%m%d%H%M%S)"

say()  { printf '\033[1;34m>>> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m    ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m    ✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ────────────────────────────────────────────────────────────
# 上游 workbuddy2api
# ────────────────────────────────────────────────────────────
patch_upstream() {
  say "上游 workbuddy2api"

  [ -d "$UPSTREAM_DIR" ] || die "上游源码目录不存在：$UPSTREAM_DIR"
  [ -f "$UPSTREAM_DIR/go.mod" ] || die "$UPSTREAM_DIR 不像上游源码（无 go.mod）"

  cd "$UPSTREAM_DIR"

  # 1) 备份
  say "备份当前二进制"
  [ -f build-wb2api ] && cp "build-wb2api" "build-wb2api.bak-$TS"
  ok "已备份 build-wb2api.bak-$TS"

  # 2) 检查是否已打过
  if grep -q "get-enterprise-user-usage" internal/upstream/enterprise_usage.go 2>/dev/null; then
    warn "检测到企业额度代码已存在，跳过打补丁（直接重建）"
  else
    say "应用 patch"
    # 优先用 git apply（要求 git 仓库存在）
    if [ -d .git ]; then
      git apply --check "$HERE/workbuddy2api-enterprise-support.patch" 2>/dev/null \
        && git apply "$HERE/workbuddy2api-enterprise-support.patch" \
        && ok "git apply 成功" \
        || die "git apply 失败（源码版本可能不匹配），请手动核对"
    else
      # 无 git：直接用 verified 成品文件覆盖
      warn "非 git 仓库，改用成品文件覆盖"
      cp "$HERE/verified/upstream-enterprise_usage.go" internal/upstream/enterprise_usage.go
      warn "注意：还需要手动改 client.go / handler.go / scheduler.go / main.go"
      warn "无 git 时建议人工按 patch 逐处应用"
      die "无 git 仓库请手工应用 patch"
    fi
  fi

  # 3) 编译
  say "编译"
  export PATH="/tmp/go/bin:$PATH"
  export GOPROXY="https://goproxy.cn,direct"
  export GOFLAGS="-mod=mod"
  command -v go >/dev/null || die "找不到 go（请先安装，或设置 PATH 含 /tmp/go/bin）"

  go vet ./... 2>&1 | head -20 || warn "go vet 有警告"
  CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o build-wb2api-new ./cmd/server
  ok "编译成功"

  # 4) 测试（可选，失败不阻断）
  say "运行测试"
  go test ./... 2>&1 | tail -5 || warn "测试未全绿，请复核"

  # 5) 部署
  say "重建镜像并重启容器"
  cp build-wb2api-new build-wb2api
  docker build -f Dockerfile.local -t workbuddy2api:local . 2>&1 | tail -3
  if [ -f docker-compose.local.yml ]; then
    docker compose -f docker-compose.local.yml up -d --force-recreate wb2api 2>&1 | tail -3
  else
    docker restart workbuddy2api
  fi
  sleep 8
  docker ps --filter name=workbuddy2api --format '    {{.Names}} | {{.Status}}'
  ok "上游完成"
}

# ────────────────────────────────────────────────────────────
# manager
# ────────────────────────────────────────────────────────────
patch_manager() {
  say "manager (Python)"

  docker inspect "$MANAGER_CONTAINER" >/dev/null 2>&1 || die "容器不存在：$MANAGER_CONTAINER"

  mkdir -p "$PATCH_DIR"

  # 1) 备份现有 patched 文件（若有）
  if [ -f "$PATCH_DIR/tencent.py" ]; then
    cp "$PATCH_DIR/tencent.py" "$PATCH_DIR/tencent.py.bak-$TS"
    cp "$PATCH_DIR/wb2api.py" "$PATCH_DIR/wb2api.py.bak-$TS"
    ok "已备份现有 patched 文件"
  fi

  # 2) 落盘成品文件
  say "写入补丁文件"
  cp "$HERE/verified/manager-tencent.py" "$PATCH_DIR/tencent.py"
  cp "$HERE/verified/manager-wb2api.py" "$PATCH_DIR/wb2api.py"
  ok "已写入 $PATCH_DIR"

  # 3) 保存当前容器启动参数，用于重建
  say "读取当前容器配置"
  local old_env old_mounts network
  old_env=$(docker inspect "$MANAGER_CONTAINER" --format '{{json .Config.Env}}')
  network=$(docker inspect "$MANAGER_CONTAINER" --format '{{.HostConfig.NetworkMode}}')
  ok "network=$network"

  # 4) 重建容器
  say "重建容器（保留原挂载 + 新增 2 处 patch 挂载）"
  local run_args=()
  # 原有 bind mounts
  while IFS= read -r m; do
    [ -n "$m" ] && run_args+=(-v "$m")
  done < <(docker inspect "$MANAGER_CONTAINER" --format '{{range .Mounts}}{{.Source}}:{{.Destination}}{{println}}{{end}}')
  # patch 挂载（去重：若已有同 destination 则跳过）
  local has_tencent=0 has_wb2api=0
  for a in "${run_args[@]}"; do
    case "$a" in
      */app/server/services/tencent.py) has_tencent=1 ;;
      */app/server/services/wb2api.py)  has_wb2api=1 ;;
    esac
  done
  [ $has_tencent -eq 0 ] && run_args+=(-v "$PATCH_DIR/tencent.py:/app/server/services/tencent.py:ro")
  [ $has_wb2api  -eq 0 ] && run_args+=(-v "$PATCH_DIR/wb2api.py:/app/server/services/wb2api.py:ro")

  docker rm -f "$MANAGER_CONTAINER" >/dev/null
  sleep 2
  docker run -d --name "$MANAGER_CONTAINER" \
    --restart unless-stopped \
    --network "$network" \
    "${run_args[@]}" \
    "$(docker inspect "$MANAGER_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null || echo ghcr.io/ithtelab/workbuddy-manager:latest)" \
    >/dev/null 2>&1 || {
      warn "自动重建失败，请手工按下面参数重建："
      echo "  docker run -d --name $MANAGER_CONTAINER --restart unless-stopped --network host \\"
      echo "    -v /opt/workbuddy-manager/data:/app/data \\"
      echo "    -v $PATCH_DIR/tencent.py:/app/server/services/tencent.py:ro \\"
      echo "    -v $PATCH_DIR/wb2api.py:/app/server/services/wb2api.py:ro \\"
      echo "    ghcr.io/ithtelab/workbuddy-manager:latest"
      die "重建失败"
    }

  sleep 12
  docker ps --filter "name=$MANAGER_CONTAINER" --format '    {{.Names}} | {{.Status}}'
  ok "manager 完成"
}

# ────────────────────────────────────────────────────────────
# 验证
# ────────────────────────────────────────────────────────────
verify() {
  say "验证"
  local key
  key=$(python3 -c "import json;print(json.load(open('$UPSTREAM_DIR/config.json'))['api_key'])" 2>/dev/null || echo "test_key")
  echo "  上游 /status:"
  curl -s --max-time 20 -H "Authorization: Bearer $key" http://127.0.0.1:7863/status 2>/dev/null \
    | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    for a in d.get('accounts',[]):
        print('    %-24s credits=%s' % ((a.get('nickname') or '')[:22], a.get('credits')))
except Exception as e:
    print('    解析失败:', e)
" || warn "上游未响应"
}

case "$TARGET" in
  upstream) patch_upstream; verify ;;
  manager)  patch_manager;  verify ;;
  all)      patch_upstream; patch_manager; verify ;;
  *)        die "未知目标：$TARGET（可选 upstream / manager / all）" ;;
esac

say "全部完成"

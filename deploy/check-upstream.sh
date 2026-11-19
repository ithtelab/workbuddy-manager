#!/usr/bin/env bash
# ============================================================
# 上游检查（保留命令，但不再访问外部仓库）
#
# 上游 workbuddy2api 已由本项目维护，源码随管理端 Release 分发；没有独立的
# 远端分支/提交可比较。发版前应验证的是补丁与 Go 测试，而不是 git 提交：
#
#   python3 dev/apply_upstream_patches.py <解包后的 upstream>
#   (cd <解包后的 upstream> && go build ./... && go test ./...)
#   python3 dev/pack_fixed_upstream_src.py <解包后的 upstream> -o <输出目录>
#   gh release upload upstream-src <输出目录>/workbuddy2api-src.tar.gz --clobber
#
# 退出码：0 = 无需外部检查（可以继续发版流程）
# ============================================================
set -euo pipefail

echo "[—] 上游已由本项目维护，随管理端 Release 分发；不再检查外部仓库提交。"
echo "    发版前请运行补丁与 Go 测试（见 docs/release-process.md）。"
exit 0

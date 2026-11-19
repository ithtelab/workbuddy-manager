#!/usr/bin/env bash
# ============================================================
# 校验发布包摘要（sha256）
#
# 发布页（GitHub Releases）的发布说明里附了 tar.gz 的 sha256；一键更新
# 会自动核对它，这里是**手动安装**时自己核对用的：
#
#   bash deploy/verify-release.sh workbuddy-manager-v1.0.86.tar.gz
#   bash deploy/verify-release.sh workbuddy-manager-v1.0.86.tar.gz <已知 sha256>
#
# 不给第二个参数时会打印本地摘要，让你和 Releases 页面上的值对一眼。
#
# 退出码：0 = 摘要一致（或仅打印了摘要）；1 = 不一致（**不要解压安装**）
#
# 说明：2026-10-14 起本项目不再对发布包做离线私钥签名（原来的 ssh-keygen
# 验签已取消），原因与信任边界见 docs/release-signing.md。
# ============================================================
set -euo pipefail

ARCHIVE="${1:-}"
EXPECTED="${2:-}"

if [ -z "$ARCHIVE" ]; then
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi

[ -f "$ARCHIVE" ] || { echo "✗ 找不到文件：$ARCHIVE" >&2; exit 2; }
command -v sha256sum >/dev/null 2>&1 || { echo "✗ 需要 sha256sum（coreutils）" >&2; exit 2; }

ACTUAL="$(sha256sum "$ARCHIVE" | cut -d' ' -f1)"

if [ -z "$EXPECTED" ]; then
  echo "本地 sha256：$ACTUAL"
  echo "把它与 Releases 页面「产物校验（sha256）」里的值比一眼："
  echo "  https://github.com/ithtelab/workbuddy-manager/releases"
  exit 0
fi

EXPECTED="$(printf '%s' "$EXPECTED" | tr 'A-F' 'a-f' | tr -d '[:space:]')"
if [ "$ACTUAL" = "$EXPECTED" ]; then
  echo "✓ sha256 一致，可以解压安装"
  exit 0
fi

cat >&2 <<EOF
✗ sha256 不一致 —— 请勿解压安装！

  期望：$EXPECTED
  实际：$ACTUAL

  可能原因：
    1. 下载不完整（最常见，重下一次再试）
    2. 文件来自别的版本（版本号对不上）
    3. 产物在传输途中被替换（这正是核对要拦的情况）
EOF
exit 1

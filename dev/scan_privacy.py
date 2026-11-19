"""扫描仓库里可能携带的隐私信息。

在**提交/推送之前**跑一次：确认没有 token、私人邮箱、本机绝对路径、真实密钥。

用法：
    python3 dev/scan_privacy.py            # 扫当前仓库
    python3 dev/scan_privacy.py <目录>     # 扫另一个目录

判据分两类：
  · **硬命中**（一定有问题）：GitHub token、面板管理员口令、网关 API key、
    私钥块、真实的 wbk_ 密钥、Bearer 长串；
  · **需人工确认**：私人邮箱（上游贡献者的 qq.com 邮箱属正常，出现在
    Author 行里）、本机绝对路径（.next/ 这类构建产物里的不算）。

只扫**已跟踪的文件**（git ls-files）—— 构建产物与本地备份不入库，
扫它们只会误报。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# 硬命中：出现即视为泄露
HARD_PATTERNS = [
    (r'ghp_[A-Za-z0-9]{20,}', 'GitHub personal access token'),
    (r'github_pat_[A-Za-z0-9_]{20,}', 'GitHub fine-grained token'),
    (r'-----BEGIN [A-Z ]*PRIVATE KEY-----', '私钥块'),
    (r'\bwbk_[A-Za-z0-9]{30,}\b', '面板 API key（完整）'),
    (r'\b[0-9a-f]{32}\b', '32 位十六进制串（疑似口令/密钥）'),
    (r'Bearer\s+[A-Za-z0-9]{32,}', 'Bearer 长令牌'),
    (r'"api_key"\s*:\s*"[A-Za-z0-9]{24,}"', '正文的 api_key'),
    (r'(?i)\b(admin[_-]?password|api[_-]?key|secret|token)\s*[=:]\s*["\']?[A-Za-z0-9]{24,}',
     '配置里写死的凭证'),
]

# 扫描器自己会命中上面这些模式（它把模式写在源码里）。跳过自己。
SELF = Path(__file__).name

# 需人工确认：本机路径 / 私人邮箱。路径示例里的 `me` 这类用户名是文档占位符
# （.env.example 的注释里就有 C:/Users/me/... 示例），放过它们，不然扫描永远
# 带着 3 条噪音跑，真正要人工看的那几条反而被淹没。
_PLACEHOLDER_USERS = {'me', 'yourname', 'username', 'user', 'example'}

SOFT_PATTERNS = [
    (r'/Users/[a-z][a-z0-9_-]*/', '本机绝对路径'),
    (r'[A-Za-z0-9._%+-]+@(gmail|qq|163|126|outlook|hotmail|icloud)\.[a-z]{2,}', '私人邮箱'),
]

TEXT_SUFFIX = {'.py', '.ts', '.tsx', '.js', '.mjs', '.cjs', '.md', '.txt',
                '.json', '.yml', '.yaml', '.sh', '.go', '.css', '.html',
                '.example', '.cfg', '.ini', '.toml'}


def tracked_files(root: Path) -> list[Path]:
    r = subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return [root / p for p in r.stdout.split('\0') if p]


def _hit(pattern: str, line: str) -> bool:
    """正则命中，但**放过占位符**。

    文档与探针里常见 `wbk_xxxxxxxxxxxx`、`your-token-here` 这类占位符 ——
    它们不是真密钥，不该报成泄露。判据：命中段里**全是 x / 0 / 1** 之类
    明显的占位字符，就放过。
    """
    m = re.search(pattern, line)
    if not m:
        return False
    token = m.group(0)
    # 占位符：把「前缀 + 分隔符」去掉后，剩下的全是 x 之类
    body = re.sub(r'^[a-z]+[_-]', '', token, flags=re.I)
    if body and set(body.lower()) <= set('x-_0123456789'):
        return False
    return True


def scan(root: Path) -> int:
    files = tracked_files(root)
    print(f'扫描 {root}：{len(files)} 个已跟踪文件')
    hard_hits: list[tuple[str, str, int]] = []
    soft_hits: list[tuple[str, str, int]] = []
    for f in files:
        if not f.is_file():
            continue
        # 扫描器自己：模式就写在它的源码里，必然命中自己
        if f.name == SELF:
            continue
        if f.suffix.lower() not in TEXT_SUFFIX and f.name not in {
                'Dockerfile', 'Makefile', '.env.example', '.gitignore'}:
            continue
        try:
            text = f.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.split('\n'), 1):
            # 测试文件里的假密钥（占位符、请求追踪号）不该当成泄露 ——
            # 但仍要报出来让人看一眼，所以降级成「需人工确认」。
            in_test = '_test.' in f.name or f.name.endswith('.test.mjs') or '/tests/' in str(f)
            for pat, label in HARD_PATTERNS:
                if _hit(pat, line):
                    (soft_hits if in_test else hard_hits).append(
                        (str(f.relative_to(root)), label + ('（测试文件）' if in_test else ''), i))
            for pat, label in SOFT_PATTERNS:
                if _hit(pat, line):
                    # 路径里的用户名是文档占位符（me / username / ...）就放过
                    user = next((seg for seg in line.split('/') if seg.lower()
                                 in _PLACEHOLDER_USERS), None)
                    if user and label == '本机绝对路径':
                        continue
                    soft_hits.append((str(f.relative_to(root)), label, i))

    print()
    if hard_hits:
        print(f'❌ 硬命中 {len(hard_hits)} 处（必须处理）：')
        for path, label, line in hard_hits[:40]:
            print(f'   {path}:{line}  {label}')
    else:
        print('✅ 硬命中 0 处（无 token / 口令 / 完整密钥）')

    if soft_hits:
        print(f'\n⚠️  需人工确认 {len(soft_hits)} 处：')
        for path, label, line in soft_hits[:40]:
            print(f'   {path}:{line}  {label}')
    else:
        print('✅ 需人工确认 0 处（无本机路径 / 私人邮箱）')

    return 1 if hard_hits else 0


if __name__ == '__main__':
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    sys.exit(scan(target))

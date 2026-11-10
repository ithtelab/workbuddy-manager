"""把提交作者改成 GitHub 用户名（不暴露真实姓名与邮箱）。

为什么要改：提交里的 `zemiao.zhang <zemiao.zhang@…>` 会把真实姓名与公司邮箱
暴露在公开仓库里 —— 即使是 fork，PR 页面的「Commits」列表也会原样显示。

改成 GitHub 的 **noreply 形式**：`<用户 id>+<用户名>@users.noreply.github.com`。
这是 GitHub 官方认的形式 —— 提交会正确归属到那个账号，而页面上不再出现真实
姓名或邮箱。

用法：
    python3 dev/rewrite_authors.py <分支> <用户名> [用户 id]

只改**作者与提交者**（author / committer），不动提交内容、不改提交顺序、
不动提交信息 —— 树与历史结构完全保持。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def run(*args: str) -> str:
    r = subprocess.run(['git', *args], cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f'git {args} failed:\n{r.stdout}\n{r.stderr}')
    return r.stdout


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    branch = sys.argv[1]
    login = sys.argv[2]
    uid = sys.argv[3] if len(sys.argv) > 3 else ''

    email = f'{uid}+{login}@users.noreply.github.com' if uid else \
            f'{login}@users.noreply.github.com'

    base = run('merge-base', branch, 'origin/main').strip()
    print(f'分支 {branch} 基于 {base[:7]}')

    # 逐个提交重放：作者与提交者都改成目标身份
    commits = run('rev-list', '--reverse', f'{base}..{branch}').split()
    if not commits:
        print('没有要改的提交')
        return 0
    print(f'重写 {len(commits)} 个提交 → {login} <{email}>')

    run('checkout', '-q', '-B', 'rewrite/authors', base)
    for sha in commits:
        run('cherry-pick', '--allow-empty', '-x', '--no-commit', sha)
        # 提交者身份走环境变量（-c 与 -C 不能同时用）；作者用 --author。
        # GIT_COMMITTER_* 决定 committer，GIT_AUTHOR_* 决定 author。
        env = {
            **os.environ,
            'GIT_AUTHOR_NAME': login,
            'GIT_AUTHOR_EMAIL': email,
            'GIT_COMMITTER_NAME': login,
            'GIT_COMMITTER_EMAIL': email,
        }
        subprocess.run(
            ['git', 'commit', '-q', '--no-verify', '--reset-author', '-C', sha],
            cwd=REPO, env=env, check=True)

    # 校验：内容与顺序不变，只有作者变了
    old_tree = run('rev-parse', f'{branch}^{{tree}}').strip()
    new_tree = run('rev-parse', 'rewrite/authors^{tree}').strip()
    old_list = run('rev-list', '--reverse', f'{base}..{branch}').split()
    new_list = run('rev-list', '--reverse', f'{base}..rewrite/authors').split()
    same_msgs = all(
        run('log', '-1', '--format=%s', a).strip() == run('log', '-1', '--format=%s', b).strip()
        for a, b in zip(old_list, new_list)
    )
    print(f'树一致: {old_tree == new_tree} | 提交数一致: {len(old_list) == len(new_list)} | '
          f'提交信息一致: {same_msgs}')
    if old_tree != new_tree or len(old_list) != len(new_list) or not same_msgs:
        print('❌ 重写后内容不一致，放弃（未改动原分支）')
        return 1

    run('branch', '-f', branch, 'rewrite/authors')
    run('checkout', '-q', branch)
    run('branch', '-D', 'rewrite/authors')
    print(f'✅ {branch} 已重写')
    return 0


if __name__ == '__main__':
    sys.exit(main())

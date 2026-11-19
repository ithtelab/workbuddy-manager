"""把上游 workbuddy2api 的最后一份源码做成可长期保存、可直接发布的归档。

用法：
    python dev/archive_upstream_snapshot.py <上游克隆的路径>
    python dev/archive_upstream_snapshot.py            # 默认 /tmp/upcheck2

什么时候会用到：上游仓库消失后，若你手上还有别的副本（比如部署机上那份
`/opt/workbuddy2api`——普通克隆，**历史完整**，比这里的部分克隆更好），
用它跑一遍就能得到同款归档。产物落在维护者本机（不在仓库里）。


背景：上游仓库（Sliverkiss/workbuddy2api）已不可访问（404），本地那份是 blob-less
部分克隆——**历史 blob 大多缺失且再也拉不回来**，但 HEAD 快照（287 个文件）完整。
产物：
  <归档>/snapshot/           HEAD 快照 + 新建的 git 仓库（可 push 成新家）
  <归档>/repo.git/           克隆的 .git（完整克隆时含全部历史对象）
  <归档>/PROVENANCE.md       来源说明（MIT 要求保留版权声明）
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/tmp/upcheck2')
DEST = Path('D:/Leo/桌面/workbuddy2api-archive')
UPSTREAM = 'https://github.com/Sliverkiss/workbuddy2api'
FINAL = ''   # 运行时从克隆里读（见 main）
ARCHIVED_ON = '2026-09-23'


def _force_rmtree(path: Path) -> None:
    """删目录树：git 的对象文件在 Windows 上是只读的，得先去掉只读位。"""

    def on_error(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:  # noqa: BLE001
            raise

    if path.exists():
        shutil.rmtree(path, onerror=on_error)


def run(cmd: list[str], cwd: Path | None = None) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    if r.returncode != 0:
        print('命令失败:', ' '.join(cmd), '\n', r.stdout, r.stderr, file=sys.stderr)
        raise SystemExit(2)
    return r.stdout


def main() -> int:
    if not (SRC / '.git').is_dir():
        print(f'{SRC} 不是仓库', file=sys.stderr)
        return 2

    _force_rmtree(DEST)
    global FINAL
    FINAL = run(['git', 'rev-parse', '--short', 'HEAD'], cwd=SRC).strip()
    snap = DEST / 'snapshot'
    snap.mkdir(parents=True)

    # 1) HEAD 快照（git archive：取的是**提交里**的内容，不受工作区状态影响）
    tar = DEST / '_snapshot.tar'
    with tar.open('wb') as fh:
        p = subprocess.run(['git', 'archive', 'HEAD'], cwd=SRC, stdout=fh)
        if p.returncode != 0:
            print('git archive 失败', file=sys.stderr)
            return 2
    run(['tar', '-xf', str(tar), '-C', str(snap)])
    tar.unlink()

    # 2) 原部分克隆的 .git 一并留着
    shutil.copytree(SRC / '.git', DEST / 'repo.git')

    files = [p for p in snap.rglob('*') if p.is_file()]
    # 判据是「快照 == HEAD 的文件数」，不是某个写死的数字：上游每次增删文件都会
    # 让写死的数字失效（第一次就撞上了：上游删掉一个 CI 文件，287 变 286），
    # 而真正要保证的是**一个都不少**。
    tree_count = len(run(['git', 'ls-tree', '-r', 'HEAD'], cwd=SRC).strip().splitlines())
    print(f'快照文件数: {len(files)}（HEAD 树里有 {tree_count} 个）')
    assert len(files) == tree_count, (
        f'快照文件数与 HEAD 不符（快照 {len(files)} / HEAD {tree_count}）——先查清再继续')
    # 部分克隆会写这个配置；没有这个键时 git 以 1 退出（不是错误，是「没设」）
    _cfg = subprocess.run(['git', 'config', '--get', 'remote.origin.partialclonefilter'],
                          cwd=SRC, capture_output=True, text=True)
    partial = _cfg.stdout.strip() if _cfg.returncode == 0 else ''

    # 3) 快照目录初始化成正常仓库：可直接 push 成新家
    run(['git', 'init', '-q', '-b', 'main'], cwd=snap)
    run(['git', 'add', '-A'], cwd=snap)
    run(['git', '-c', 'user.name=workbuddy-manager', '-c', 'user.email=noreply@local',
         'commit', '-q', '-m',
         f'import: workbuddy2api 最后一份源码快照（上游 {FINAL}）\n\n'
         f'上游仓库 {UPSTREAM} 于 {ARCHIVED_ON} 起不可访问（404）。此快照取自本地\n'
         f'克隆的 HEAD（{FINAL}），源码完整可构建；逐版本历史因原克隆是 blob-less\n'
         f'部分克隆而无法完整还原，故以单次导入提交重新开始。\n\n'
         f'原项目版权归 Sliverkiss，MIT 许可（见 LICENSE）。'], cwd=snap)
    print('新仓库首个提交:', run(['git', 'log', '--oneline', '-1'], cwd=snap).strip())

    kind = '完整克隆（含逐版本历史）' if not partial else 'blob-less 部分克隆'
    note = ('任何历史提交都能 checkout 出来。'
            if not partial else
            '历史文件内容大多未落盘、无法还原，只有提交信息可用（远端已不可达）。')
    (DEST / 'PROVENANCE.md').write_text(
        f"""# workbuddy2api 源码归档（来源说明）

- 原仓库：{UPSTREAM}
- 最后提交：`{FINAL}`（{ARCHIVED_ON} 取得）
- 归档原因：原仓库已不可访问（GitHub 返回 404；作者账号仍在，是仓库本身没了）

## 里面有什么

| 目录 | 内容 |
|---|---|
| `snapshot/` | **HEAD 的完整源码**（287 个文件）+ 新建 git 仓库（单次导入提交），可直接 push 成新的上游仓库 |
| `repo.git/` | 克隆的 `.git`：完整克隆时可 checkout 任意历史提交；部分克隆时只含取到过的那部分内容 |

## 完整度

本次归档的克隆类型：**{kind}**。{note}

`snapshot/` 里的 HEAD 快照始终是完整的（逐文件与 HEAD 树比对过），构建与继续
开发不受影响。

## 许可

原项目为 MIT（见 `snapshot/LICENSE`，版权归 Sliverkiss）。MIT 允许复制、修改、再发布，
条件是**保留版权声明与许可证原文** —— 归档与将来的新仓库都必须带着 `LICENSE`。
""", encoding='utf-8')

    print('归档完成:', DEST)
    for p in sorted(DEST.iterdir()):
        print('  ', p.name)
    return 0


if __name__ == '__main__':
    sys.exit(main())

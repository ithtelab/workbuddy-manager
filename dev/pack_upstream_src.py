"""把上游 workbuddy2api 的源码打成「随 Release 分发」的压缩包。

## 为什么有这个脚本

上游源码不再有公开的 git 地址可拉。本项目的公开仓库
**不放上游代码**（它不属于我们），但用户装的时候需要它——所以做法是：

    dev/ 里的这个脚本  →  打成 workbuddy2api-src.tar.gz
                       →  上传到一个**固定 tag 的 Release**（upstream-src）
                       →  每次面板发版时，CI 从那里取回来，塞进 Release 包里的 upstream/
                       →  install.sh 见到 upstream/ 就直接用（离线可装）
                       →  update.py 同步进 /opt/workbuddy2api（保留 config/auths/data）

于是「上游源码更新」与「面板发版」是一条通道：维护者把新代码放进源码目录、
重跑本脚本、覆盖那个 Release 附件，下一次面板发版就会把新代码带给所有用户。

## 用法

    python dev/pack_upstream_src.py <源码目录> [-o 输出目录] [--overlay 补丁目录]
                                   [--stamp 说明文字]

    # 例：把归档里的快照打包，并叠加一份新的签到脚本
    python dev/pack_upstream_src.py "D:/Leo/桌面/workbuddy2api-archive/snapshot" \
        --overlay ./new-scripts --stamp "更新签到脚本：<一句话>"

输出 `workbuddy2api-src.tar.gz`，内含顶层目录 `workbuddy2api/`（解压时配
`--strip-components=1` 即可，与 install.sh 的约定一致）。

## 打包时做的两件事

1. **排除 `.git`**：源码目录若是 git 仓库（比如归档里那份），不要把版本库带进去
   ——用户不需要，而它会让包大一截、还会让人误以为能从它 `git pull` 更新。
2. **归一化时间戳与顺序**：同名同内容的两次打包产出**逐字节相同**，便于比对
   「这次到底换了什么」。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

TOP = 'workbuddy2api'
# 打包进公开 Release 的东西必须**只含源码**：部署树的 .env、数据库、日志、Python
# 虚拟环境里全是凭据与运行时垃圾（2026-10-18 审计：旧清单只排除了 config.json /
# auths / data，`.env`、`*.db`、`logs/` 会被原样打进公开资产）。
EXCLUDE_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', '.runtime',
                'logs', 'tmp', 'data', 'auths'}
EXCLUDE_SUFFIX = {'.pyc', '.pyo', '.log', '.db', '.db-wal', '.db-shm',
                  '.sqlite', '.sqlite3'}
EXCLUDE_NAMES = {'config.json', 'auths', 'data', 'users.json',
                 'update-status.json', 'version-check.json', 'upstream-ref.txt'}


def _lf_text(raw: bytes) -> bytes:
    """文本内容 CRLF → LF；二进制原样返回（issue #202）。

    判据：**不含 NUL 且能按 UTF-8 解码**的当文本，其余（图片、压缩包等）一个字节都
    不动。猜错方向的代价不对称 —— 把一个二进制改坏比留一处行尾问题严重得多。
    与 `deploy/update.py::_normalize_eol` 同一口径（那边是同步时的兜底；这边治源头，
    让下出去的资产本身就是干净的）。
    """
    if b'\x00' in raw:
        return raw
    try:
        raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw
    return raw.replace(b'\r\n', b'\n')


def _iter_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(root.rglob('*')):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.suffix in EXCLUDE_SUFFIX:
            continue
        # 只排除**顶层**的运行时文件/目录：源码树里若有 scripts/config.json 之类的
        # 示例文件，那是要保留的。`.env*` 一律排除（含 .env.local 这类备份）。
        if len(rel.parts) == 1 and (rel.parts[0] in EXCLUDE_NAMES
                                    or rel.parts[0].startswith('.env')):
            continue
        out.append(p)
    return out


def tree_id(root: Path) -> str:
    """源码树的稳定标识：路径 + 内容哈希（排除了会随时间变化的说明文件）。

    写进包内 `.upstream-id`，面板用它判断「这次 Release 带的上游与本机是否
    同一份」——没有它就只能比较版本号，而版本号在纯上游修复时不会变。
    """
    h = hashlib.sha256()
    for f in _iter_files(root):
        rel = f.relative_to(root).as_posix()
        if rel in ('UPSTREAM-SRC.txt', '.upstream-id'):
            continue
        h.update(rel.encode('utf-8')); h.update(b'\0')
        h.update(f.read_bytes()); h.update(b'\0')
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description='打上游源码包（随 Release 分发）')
    ap.add_argument('src', type=Path, help='上游源码目录')
    ap.add_argument('-o', '--out', type=Path, default=Path('.'),
                    help='输出目录（默认当前目录）')
    ap.add_argument('--overlay', type=Path, action='append', default=[],
                    help='要先叠加进来的补丁目录（可多次）；同路径文件以补丁为准')
    ap.add_argument('--stamp', default='', help='写进包内 UPSTREAM-SRC.txt 的说明')
    args = ap.parse_args()

    src: Path = args.src
    if not (src / 'docker-compose.yml').is_file():
        print(f'[✗] {src} 看起来不是上游源码根目录（没有 docker-compose.yml）',
              file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / TOP
        shutil.copytree(src, work, ignore=shutil.ignore_patterns(*EXCLUDE_DIRS))
        for rel in sorted(EXCLUDE_NAMES):
            p = work / rel
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                p.unlink()
        overlaid: list[str] = []
        for ov in args.overlay:
            if not ov.is_dir():
                print(f'[✗] 补丁目录不存在：{ov}', file=sys.stderr)
                return 2
            for f in sorted(ov.rglob('*')):
                if not f.is_file():
                    continue
                rel = f.relative_to(ov)
                dest = work / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dest)
                overlaid.append(str(rel))
        print(f'叠加补丁 {len(overlaid)} 个文件' if overlaid else '无补丁')

        (work / 'UPSTREAM-SRC.txt').write_text(
            f"""本目录是上游 workbuddy2api 的源码快照，随本项目的 Release 一同分发。

上游原仓库（github.com/Sliverkiss/workbuddy2api）已被其作者删除；源码由本项目
维护者留存并继续维护。许可为 MIT（见 LICENSE，版权归原作者）——再分发请保留
LICENSE 与版权声明。

打包说明：{args.stamp or '（未填写）'}
打包时间：{time.strftime('%Y-%m-%d %H:%M:%S')}
""", encoding='utf-8')

        (work / '.upstream-id').write_text(tree_id(work) + '\n', encoding='utf-8')

        files = _iter_files(work)
        out_path: Path = args.out / 'workbuddy2api-src.tar.gz'
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 归一化：固定 mtime / uid / gid / 排序 → 同内容两次打包逐字节相同
        with tarfile.open(out_path, 'w:gz', format=tarfile.GNU_FORMAT) as tf:
            for f in files:
                rel = f.relative_to(work.parent)
                info = tf.gettarinfo(str(f), arcname=str(rel))
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mode = 0o755 if (f.stat().st_mode & 0o111) else 0o644
                # 行尾归一（issue #202）：源码目录可能是在 Windows 上检出的（git 的
                # `core.autocrlf` 会把文本文件写成 CRLF），而打包是字节拷贝 —— 包内
                # 那份 CRLF 会让容器里的脚本首行变成 `#!/usr/bin/env bash\r`，解释器
                # 路径对不上，一键登录 / 签到 / 积分这些依赖脚本的功能整体失效。
                # 归一放在**打包这一步**、而不是指望源码目录干净：谁的工作区干净
                # 不该决定发出去的资产长什么样。判据见 `_lf_text`。
                data = _lf_text(f.read_bytes())
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        size = out_path.stat().st_size
        digest = hashlib.sha256(out_path.read_bytes()).hexdigest()[:16]
        print(f'已打出 {out_path}（{len(files)} 个文件，{size / 1024:.0f} KB，sha256:{digest}…）')
        print('上传到固定 tag 的 Release（让 CI 取得到）：')
        print(f'  gh release upload upstream-src {out_path} --clobber '
              f'--repo ithtelab/workbuddy-manager')
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""Regenerate the wb2api source asset with the reviewed manager patches."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if __package__:
    from .apply_upstream_patches import apply_patches
    from .pack_upstream_src import _lf_text
else:
    from apply_upstream_patches import apply_patches
    from pack_upstream_src import _lf_text


def _normalize_tree(root: Path) -> int:
    """把源码树里的**文本**文件行尾归一成 LF（issue #202）。

    补丁文件现在是 LF；若源码树是 CRLF（Windows 上检出/解包出来的都可能是），
    `git apply` 会因为上下文行尾对不上而拒掉 —— 归一这一步让「源码树从哪来」不再
    影响能否打补丁。二进制（含 NUL / 非法 UTF-8）一律不动，判据见 `_lf_text`。
    """
    changed = 0
    for f in root.rglob('*'):
        if not f.is_file() or '.git' in f.parts:
            continue
        raw = f.read_bytes()
        fixed = _lf_text(raw)
        if fixed != raw:
            f.write_bytes(fixed)
            changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('-o', '--out', type=Path, default=Path('.'))
    parser.add_argument('--stamp', default='Fix tool-result notices and request-level 11148 handling')
    args = parser.parse_args()
    source = args.source.resolve()
    packer = Path(__file__).with_name('pack_upstream_src.py')
    if not packer.is_file():
        parser.exit(1, 'Run this from a manager checkout containing dev/pack_upstream_src.py.\n')

    def ignore(directory: str, names: list[str]) -> set[str]:
        omitted = {'.git', '__pycache__', 'node_modules'}
        if Path(directory).resolve() == source:
            omitted |= {'config.json', 'auths', 'data'}
        return set(names) & omitted

    try:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / 'workbuddy2api'
            shutil.copytree(source, work, ignore=ignore)
            normalized = _normalize_tree(work)
            if normalized:
                print(f'行尾归一：{normalized} 个文件 CRLF → LF', flush=True)
            for result in apply_patches(work, Path(__file__).parent / 'upstream-patches'):
                print(result, flush=True)
            return subprocess.run([
                sys.executable, str(packer), str(work), '-o', str(args.out.resolve()),
                '--stamp', args.stamp,
            ]).returncode
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())

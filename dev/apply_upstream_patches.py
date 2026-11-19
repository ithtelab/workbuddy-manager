"""Apply reviewed wb2api patches before bundling the upstream Release snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
import re
import subprocess

_HUNK = re.compile(r'^@@')
_FILE = re.compile(r'^\+\+\+ b/(.+)$')


def _added_lines(patch: Path) -> dict[str, list[str]]:
    """解析补丁，返回 {目标文件: [新增行内容]}（不含 `+` 前缀）。"""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for raw in patch.read_text(encoding='utf-8').splitlines():
        m = _FILE.match(raw)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
            continue
        if _HUNK.match(raw):
            continue
        if current is None or not raw.startswith('+') or raw.startswith('+++'):
            continue
        out[current].append(raw[1:])
    return out


def _already_applied(patch: Path, source: Path) -> bool:
    """补丁的新增行是否**都已在源码里**（= 这个补丁已经打过了）。

    为什么不沿用 `git apply --reverse --check`：后面新增的补丁一旦动到前面补丁的
    上下文邻域（哪怕只是同一小段里的另一处改动），反向校验就会失败，于是整条流水线
    在「重入」这一步报「patch does not match this snapshot」—— 而内容其实完全正确。
    本仓的补丁是**顺序应用于同一份资产**的切片，这种上下文漂移是常态。

    判据只看新增行（上下文行不参与）：对「已经应用」这个判断足够，且不受其它补丁
    漂移影响。空行不判（到处都是）。新增行全为空的新文件补丁按「未应用」处理 ——
    那种情况下真正该走的是正常 apply。

    **按整行、按出现次数比较**，不做子串匹配：补丁里常见的 `}`、`return nil`
    在文件里到处都有，子串判定会让「只打了一半的补丁」被误认成已应用，随后
    `git apply` 又被跳过，产物里就少了一段逻辑。次数比较还覆盖了同一个补丁在
    不同 hunk 插入相同行的情况。
    """
    files = _added_lines(patch)
    if not files:
        return False
    checked = 0
    for rel, lines in files.items():
        target = source / rel
        if not target.is_file():
            return False
        existing = Counter(target.read_text(encoding='utf-8', errors='replace').splitlines())
        for line in lines:
            if not line.strip():
                continue
            checked += 1
            if existing[line] <= 0:
                return False
            existing[line] -= 1
    return checked > 0


# 补丁应用清单：{补丁文件名: 补丁 sha256}，写在源码树根。
#
# 为什么需要它：按「新增行内容」判定（_already_applied）解决不了**后一个补丁改写
# 前一个补丁新增行**的情形 —— 0009 引入的兜底块被 0010 重排后，0009 的新增行就
# 不在树里了，重入时会被误判成「没打」，整条流水线随即报 patch does not match。
# 清单把「这份树打过哪些补丁、打的是哪一版字节」记成事实，重入时按哈希精确跳过；
# 补丁文件被修改（哈希变了）则退回内容/反向判定，避免陈旧清单掩盖真实改动。
#
# 清单会随资产一起打包，所以每个发布出去的 upstream-src 都自带它的补丁 ancestry。
MANIFEST_NAME = '.wb2a-patches.json'


def _patch_sha(patch: Path) -> str:
    return hashlib.sha256(patch.read_bytes()).hexdigest()


def _load_manifest(source: Path) -> dict[str, str]:
    path = source / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _save_manifest(source: Path, manifest: dict[str, str]) -> None:
    path = source / MANIFEST_NAME
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                   encoding='utf-8', newline='\n')
    os.replace(tmp, path)


def apply_patches(source: Path, patch_dir: Path) -> list[str]:
    source = source.resolve()
    patch_dir = patch_dir.resolve()
    if not (source / 'go.mod').is_file() or not (source / 'docker-compose.yml').is_file():
        raise ValueError(f'Not a workbuddy2api source root: {source}')
    patches = sorted(patch_dir.glob('*.patch'))
    if not patches:
        raise ValueError(f'No upstream patches found in {patch_dir}')
    # 历史遗留清理：0040 补丁曾把外层 diff 写成「新增一个 0040.patch」，于是源码根
    # 多出一份补丁副本、还随载体发布了出去。上游根本没有这种文件，这里在应用前删掉
    # 根目录的 `NNNN.patch` 残留，让下次重打包自动收敛（不碰任何真实源文件）。
    for stray in source.glob('[0-9][0-9][0-9][0-9].patch'):
        stray.unlink()
    results = []
    manifest = _load_manifest(source)
    manifest_dirty = False
    env = os.environ.copy()
    # Release staging lives inside the manager checkout. Do not let git discover
    # that parent repository and silently skip paths belonging to the upstream.
    env['GIT_CEILING_DIRECTORIES'] = str(source.parent)
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
        env.pop(key, None)
    for patch in patches:
        sha = _patch_sha(patch)
        if manifest.get(patch.name) == sha:
            # 清单命中：这份树打的就是这一版补丁字节。即使后续补丁改写了它新增的
            # 行（0009→0010 的情况），也仍然算「已应用」。
            results.append(f'already applied: {patch.name}')
            continue

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            # core.autocrlf 必须显式关掉：Windows 上的默认值会把补丁里的 LF 在
            # 工作树上写成 CRLF，于是「打完补丁的树」与资产基线行尾不一致 ——
            # 后续补丁的上下文一旦跨到这些行就再也对不上（issue #202 的同一类
            # 事故）。资产最终由 pack 脚本归一，但中途的树也应当是干净的。
            return subprocess.run(['git', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
                                   'apply', *args, str(patch)], cwd=source,
                                  env=env, capture_output=True, text=True)
        check = run('--check')
        if check.returncode:
            # 内容判定优先：只要新增行都在，就是「已应用」（见 _already_applied 的说明）。
            if _already_applied(patch, source) or run('--reverse', '--check').returncode == 0:
                results.append(f'already applied: {patch.name}')
                manifest[patch.name] = sha
                manifest_dirty = True
                continue
            raise RuntimeError(f'Upstream patch does not match this snapshot: {patch.name}\n'
                               f'{check.stderr.strip()}\nReview/rebase the patch before publishing.')
        applied = run()
        if applied.returncode:
            raise RuntimeError(f'Could not apply {patch.name}: {applied.stderr.strip()}')
        results.append(f'applied: {patch.name}')
        manifest[patch.name] = sha
        manifest_dirty = True
    if manifest_dirty:
        _save_manifest(source, manifest)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--patch-dir', type=Path,
                        default=Path(__file__).resolve().parent / 'upstream-patches')
    args = parser.parse_args()
    try:
        for result in apply_patches(args.source, args.patch_dir):
            print(result)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f'{exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

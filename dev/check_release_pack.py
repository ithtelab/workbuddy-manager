"""把 release.yml 的「组装发布目录」步骤**真跑一遍**。

为什么值得单独跑：那段 shell 里既有文件搬运，又有「从载体 Release 取上游源码并
塞进包内」的逻辑 —— 它是用户拿到上游源码的唯一常规渠道。写错了不会报错，只会让
包里的 `upstream/` 悄悄少掉（新装用户于是装不上）。

本机没有 `zip`，所以只跑到 `tar czf` 那一步。脚本会**按实际结果分两种模式验**：

  · 拉取成功（CI 的环境）：包内必须有 upstream/、无 .git、无运行时数据；
  · 拉取失败（本机直连 GitHub 被墙时就是这样）：必须给出 ::warning::、不留空的
    upstream/ 目录、发布包仍完整产出 —— 一次网络抖动不该让整个发布失败。

要跑「拉取成功」那种模式，本机需要代理，例如：

    https_proxy=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890 \
        python dev/check_release_pack.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    import yaml

    wf_path = ROOT / '.github' / 'workflows' / 'release.yml'
    data = yaml.safe_load(wf_path.read_text(encoding='utf-8'))
    job = next(iter(data['jobs'].values()))
    step = next(s for s in job['steps'] if s.get('id') == 'pack')
    script = step['run']
    # 去掉 GitHub 表达式与本机没有的 zip，其余**原样执行**
    script = script.replace('${{ steps.vars.outputs.tag }}', 'v9.9.9-test')
    script = script.replace('${{ github.repository }}', 'ithtelab/workbuddy-manager')
    script = '\n'.join(l for l in script.splitlines() if 'zip -qr' not in l)

    work = ROOT / 'dev' / '.pack-test'
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    # 搭一个「checkout 现场」：照 CI 的 checkout 结果复制需要的路径。
    # 不用 git archive + tar 解：本机 tar 对中文文件名会报 Invalid empty pathname
    # （Windows 上的编码怪癖，CI 跑在 Linux 没这问题）。
    repo = work / 'repo'
    repo.mkdir()
    for rel in ('server', 'deploy', 'docs'):
        shutil.copytree(ROOT / rel, repo / rel,
                        ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    shutil.copytree(ROOT / 'web' / 'out', repo / 'web' / 'out')
    for name in ('.env.example', 'README.md', 'README.en.md', 'CHANGELOG.md', 'LICENSE',
                 'Dockerfile', 'docker-compose.yml'):
        src = ROOT / name
        if src.is_file():
            shutil.copyfile(src, repo / name)

    print('=== 执行组装步骤（zip 那行跳过）===')
    r = subprocess.run(['bash', '-c', script], cwd=repo, capture_output=True,
                       text=True, encoding='utf-8', errors='replace')
    out = r.stdout or ''
    print('\n'.join(out.splitlines()[-12:]))
    if r.stderr:
        print(r.stderr[-1200:], file=sys.stderr)
    if r.returncode != 0:
        print(f'组装步骤退出码 {r.returncode}', file=sys.stderr)
        return 1

    problems: list[str] = []

    def check(ok: bool, label: str, detail: str = '') -> None:
        print(f'  {"✓" if ok else "✗"}  {label}' + (f'\n       {detail}' if detail else ''))
        if not ok:
            problems.append(label)

    fetched = '取不到上游源码包' not in out
    print(f'  [i] 拉取载体附件：{"成功（CI 的环境）" if fetched else "失败（本机需代理；CI 直连）"}')

    stage = repo / 'workbuddy-manager-v9.9.9-test'
    up = stage / 'upstream'
    check(stage.is_dir(), '组装出发布目录', stage.name)

    if fetched:
        files = sorted(p for p in up.rglob('*') if p.is_file()) if up.is_dir() else []
        check(up.is_dir() and len(files) > 200, 'upstream/ 已内嵌（来自载体 Release）',
              f'文件数={len(files)}')
        for name in ('docker-compose.yml', 'Dockerfile', 'LICENSE', 'scripts/task_runner.py'):
            check((up / name).is_file(), f'upstream/{name} 就位')
        check(not (up / '.git').exists(), '内嵌的 upstream/ 不含 .git')
        check(not (up / 'config.json').exists() and not (up / 'auths').exists()
              and not (up / 'data').exists(), '内嵌的 upstream/ 不含运行时数据与凭据')
    else:
        # 拉不到时的**正确行为**：告警、不内嵌、发布包照常产出
        check('::warning::' in out, '拉取失败时给出了 ::warning:: 告警')
        check(not up.exists(), '拉取失败时不留空的 upstream/ 目录')

    check((stage / '.version').is_file(), '.version 已写入')
    check((stage / 'server' / 'main.py').is_file(), 'server/ 已打包')
    check((stage / 'deploy' / 'install.sh').is_file(), 'deploy/install.sh 已打包')

    # 打成 tar 再验一次（发布产物就是这个）。用 tarfile 读清单：本机 tar 的输出
    # 含非 UTF-8 文件名时会让 text 解码失败（harness 的坑，不是包的问题）。
    subprocess.run(['tar', 'czf', f'{stage.name}.tar.gz', stage.name], cwd=repo, check=True)
    import tarfile
    with tarfile.open(repo / f'{stage.name}.tar.gz', 'r:gz') as tf:
        names = set(tf.getnames())
    if fetched:
        check(f'{stage.name}/upstream/scripts/task_runner.py' in names,
              '发布 tar 包内含上游源码')

    size = (repo / f'{stage.name}.tar.gz').stat().st_size
    print(f'\n发布包大小：{size / 1024 / 1024:.1f} MB')
    print('=== 结果 ===')
    if problems:
        print(f'✗ {len(problems)} 项未通过：' + '、'.join(problems))
        return 1
    print('ALL CHECKS PASSED' + ('（含内嵌上游源码）' if fetched else '（回退路径）'))
    return 0


if __name__ == '__main__':
    sys.exit(main())

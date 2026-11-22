"""Dockerfile 结构自检（无 docker 环境下的等效校验）。

为什么需要：本地与 CI 都不一定随时能跑 docker build（本机就没装），而这个
Dockerfile 的改动（多阶段、条件分支）出错的代价是**用户装不上**。至少要把
机械可查的性质钉住：指令拼写、阶段引用可解析、shell 引号配平、续行完整。

    python dev/check_dockerfile.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DF = ROOT / 'Dockerfile'

KNOWN = ('FROM', 'RUN', 'CMD', 'LABEL', 'EXPOSE', 'ENV', 'ADD', 'COPY',
         'ENTRYPOINT', 'VOLUME', 'USER', 'WORKDIR', 'ARG', 'ONBUILD',
         'STOPSIGNAL', 'HEALTHCHECK', 'SHELL')


def main() -> int:
    df = DF.read_text(encoding='utf-8')
    lines = df.splitlines()
    problems: list[str] = []

    # 1. 指令拼写
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if not s or s.startswith('#'):
            continue
        first = s.split()[0].upper()
        if first in KNOWN:
            continue
        if i >= 2 and lines[i - 2].rstrip().endswith('\\'):
            continue   # 续行内容
        problems.append(f'{i}: 非法指令 {l[:70]!r}')

    # 2. 阶段引用可解析
    stages = [m.group(1) for m in
              re.finditer(r'(?m)^FROM\s+\S+\s+AS\s+(\S+)', df, re.I)]
    refs = set(re.findall(r'--from=(\S+)', df))
    for r in refs:
        if r not in stages and not r.isdigit():
            problems.append(f'COPY --from={r} 引用了不存在的阶段（已定义：{stages}）')

    # 3. shell 引号配平（逐 RUN 块统计，用于发现明显漏引号）
    run_buf: list[str] = []
    for i, l in enumerate(lines, 1):
        if l.strip().startswith('RUN '):
            run_buf = [l]
        elif run_buf and lines[i - 2].rstrip().endswith('\\'):
            run_buf.append(l)
        else:
            if run_buf:
                blk = '\n'.join(run_buf)
                if blk.count("'") % 2 != 0:
                    problems.append(f'RUN 块（约第 {i - len(run_buf)} 行）单引号数为奇数')
            run_buf = []
    if run_buf and '\n'.join(run_buf).count("'") % 2 != 0:
        problems.append('最后一个 RUN 块单引号数为奇数')

    # 4. 续行不能后面跟着空行
    for i, l in enumerate(lines, 1):
        if l.rstrip().endswith('\\') and i < len(lines) and not lines[i].strip():
            problems.append(f'{i}: 以 \\ 结尾但下一行为空')

    # 5. 关键契约（与 issue #38 直接相关）
    if not re.search(r'(?m)^FROM\s+node:', df):
        problems.append('缺少 Node 构建阶段（git clone 场景无法构建前端）')
    if 'NEXT_OUTPUT_EXPORT' not in df:
        problems.append('缺少 NEXT_OUTPUT_EXPORT（会构建出非静态导出产物）')
    if not re.search(r'(?m)^COPY\s+--from=\S+\s+\S+\s+/app/web/out\s*$', df):
        problems.append('没有把前端产物拷到 /app/web/out 的 COPY 指令')

    # 6. 入口接线（issue #473 / PR #471 的教训）
    #
    # 入口脚本负责三件事：按 PUID/PGID 对齐挂载目录属主、su-exec 降权、首次启动
    # 生成上游 config.json。它被打进镜像却没人调用时，这些全部静默失效 ——
    # 容器照样能起（CMD 直接跑 uvicorn），只是 NAS 上会 Permission denied、
    # 或者属性对齐没做。PR #471 就是这样：脚本 COPY 进来了、chmod +x 也做了，
    # 但 ENTRYPOINT 被删，103 个单测全绿。
    if not re.search(r'(?m)^ENTRYPOINT\s+\[.*docker-entrypoint\.sh.*\]', df):
        problems.append('缺少 ENTRYPOINT 指向 deploy/docker-entrypoint.sh —— '
                        '属主对齐与降权脚本不会被调用')
    if not re.search(r'(?m)^COPY\s+deploy\s+/app/deploy\s*$', df):
        problems.append('没有把 deploy/ 拷进镜像 —— ENTRYPOINT 指向的脚本不存在')

    # 入口脚本以 root 起、改完属主再 su-exec 降权。写死 USER 会让那段 root
    # 分支整段不可达（`if [ "$(id -u)" = "0" ]` 恒假），PUID/PGID 探测与 chown
    # 全成死代码。最终阶段因此不能有 USER。
    last_stage = df.rsplit('\nFROM ', 1)[-1]
    if re.search(r'(?m)^USER\s+\S', last_stage):
        problems.append('最终阶段写了 USER —— 入口脚本的 root 分支（属主对齐）'
                        '将不可达；降权应由脚本自己用 su-exec 完成')

    if problems:
        print('Dockerfile 自检发现问题：')
        for p in problems:
            print('  -', p)
        return 1
    print(f'Dockerfile 自检通过（阶段：{stages}）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

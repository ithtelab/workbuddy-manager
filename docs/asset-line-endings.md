# 随包上游源码的行尾（CRLF → LF）

随包分发的上游源码（`upstream-src` 资产 → 发布包里的 `upstream/`）**曾是 CRLF**。
后果不是「不好看」而是**跑不起来**：容器里 `login.sh` 这类脚本的首行成了
`#!/usr/bin/env bash\r`，解释器路径对不上，一键登录 / 签到 / 积分这些依赖脚本的功能
整体失效。用户报告与实测数据见 issue #202。

## 两处成因，两处修

行尾是**两端**的问题，只修一头都还会复发：

1. **打包端（治源头）**：`dev/pack_upstream_src.py` 是字节拷贝，源码目录若是在
   Windows 上检出/解包出来的（git 的 `core.autocrlf` 会把文本写成 CRLF），CRLF 就原样
   进了包 —— 历史资产实测 293/298 个文件带 CR。现在打包时对**文本**做 LF 归一
   （判据：不含 NUL 且能按 UTF-8 解码；二进制一个字节都不动）。
   同时 `dev/upstream-patches/*.patch` 也统一成 LF —— 补丁与源码树行尾必须一致，
   否则 `git apply` 会因为上下文行尾对不上而拒掉（0001–0003 原本是 CRLF，是更早的
   Windows 工作区产物）。
2. **同步端（兜底 + 自愈）**：`deploy/update.py::_sync_bundled_upstream` 也是字节拷贝，
   所以包内的 CRLF 会一路进宿主源码树、进而进网关镜像。现在写入前归一，并且**比归一后
   的内容**：
   - 已经带着 CRLF 更新过的机器，本地脚本是坏的；只比原始字节会两边都是 CRLF、被判成
     「没变」，于是永远修不回来 —— 所以「内容一致但本地带 CR」也要重写一次（计一次改动，
     触发一次容器重建，之后就真的「没变」了）。
   - 顺带解决白等一次构建：报告方实测不归一时 295 个文件全算「改动」，归一后只报
     7 改写 + 2 新增。
   - 落盘改成「临时文件 + 原子替换」：中断时不会留下半个 `.go`（网关构建会直接失败）。

## 当前资产

```text
size:   874638
sha256: 29718acd31a5e9aa41b0dfb2829a70cfa2852e5410836e76248a32ae76371f56
```

包内 **0 个文件带 CR**（此前 293 个），`login.sh` 首行结尾是 `\nn`。补丁系列在该资产上
仍报「已应用」（条数 = `dev/upstream-patches/*.patch` 的数量；**不要写死**，补丁会继续增加）。
仍报「已应用」×5（重打包路径健康）。

## 维护者复现

```sh
python3 dev/apply_upstream_patches.py <解包后的 upstream-src>   # 期望：与补丁文件数相同的 already applied
# 打包（内部会先把源码树归一，再打）
python3 dev/pack_fixed_upstream_src.py <源码树> -o /tmp/out --stamp '...'
# 验收：包内文本文件不该有 CR
mkdir /tmp/chk && tar xzf /tmp/out/workbuddy2api-src.tar.gz -C /tmp/chk
grep -rlU $'\r$' /tmp/chk/workbuddy2api | wc -l        # 期望 0
head -c 22 /tmp/chk/workbuddy2api/login.sh | od -c     # 期望 …b a s h \n
```

# DSH system prompt 指纹（11-128）与句式级净化

DeepSeek Harness（DSH）桌面版把 system 首句写成

```text
You are an AI agent powered by DeepSeek Harness.
You are a coding agent powered by the deepseek-v4.1-flash model.
```

上游对这两句**逐字匹配**，命中即回 400 `{"code":11-128,"msg":"Illegal API invocation
from an unapproved channel"}`（与请求规模、tools、stream、max_tokens 全无关，issue #246
逐项实测排除）。`sanitize.go` 原先没有任何 DSH 指纹，于是这两句原样出站。

## 为什么后果是「整池 503」而不只是一条 400

1. `/v2/chat/completions` 返 11-128 → 网关按 0006 的兜底换到 `/console/chat/completions`；
2. `/console` 本来就不提供 `deepseek-v4.1-flash`，返 400 `{"code":11102,...}`；
3. `handler.go` 把这次 11102 当 `ErrModelBlocked`，写 6 小时 (账号,模型) 负缓存；
4. 逐个账号重复，16 个 cn 号被打满 → 对外 503 `no_healthy_account`。

补丁只解决第 1 步（指纹本身）；第 2–4 步是**放大器**，由 `0018` 修掉——回退路径上的
11102 只说明「这条路径没有这个模型」，不构成「该账号被禁」。两者独立，也都要有：
指纹是黑名单，永远补不完，只要放大器还在，别的句式照样能把整池打穿。见
`docs/console-fallback-model-missing.md`。

## 修法：按句式匹配，不按品牌

品牌/模型名是变量（`DeepSeek Harness`、`the deepseek-v4.1-flash model`），逐字替换表
漏一个变体就静默失效——这正是 issue #246 里 hub 版补丁踩过的坑：`sanitizeFeatures`
的快速路径是**大小写敏感的** `strings.Contains`，而原文是 `AI agent`（大写），补进去的
小写特征串永远命中不到，`hasFingerprint` 提前 return，改写分支根本不执行。

所以改为一条 `(?i)` 正则，识别句式并整句替换：

```text
You are [a|an] [AI] [coding] agent|assistant (powered by|built on) … .
                    ↓
You are a helpful AI assistant.
```

三个必须保留的边界：

- **句尾按「点 + 空白/行尾」收口**。模型名自带点（`deepseek-v4.1-flash`），懒匹配到
  「第一个点」只会吃掉 `…v4.`、把 `1-flash model.` 留在正文里，比不替换更难查。
- **句号后的内容原样保留**（工具说明是客户端行为契约），且句尾空白要接回去，
  否则 `assistant.Then answer briefly.` 会把两句指令粘成一句。
- **预检也要认**（`hasFingerprint`），否则改写层永远到不了。正则自带 `(?i)`，
  不依赖原文字母大小写。

作用域是全部 `messages`，与文件里既有的 Claude Code / Codex 身份句一致：这些句式
出现在 user 消息里（例如用户把 system prompt 粘进对话）同样会 400，不改写必然失败。
代价只是把一句自我介绍换个说法。

## 验证

- `go build ./...`、`go test ./...`（Go 1.22.5，与 `go.mod` / 发布 CI 同版本）全过；
  新增 7 组用例覆盖：两句真实原文、`built on` 变体、全大写、模型名带点、后文保留、
  幂等、反例（普通自我介绍不得改写）、`PrepareBody` 出站边界。
- 补丁在 `upstream-src` 资产基线上 `apply_upstream_patches.py` 报 `applied`，
  重跑报 `already applied`；打完后重新打包的资产再验一遍，16 条补丁全部 `already applied`。
- **对上游真实接口的 200 无法在本仓复现**（没有可用账号与出网条件）。句式与改写目标
  来自 issue #246 与 workbuddy2api-hub#533 的实测结论（后者以「整句换成通用自我介绍」
  在生产环境验证过）。

## 补丁与资产

实现位于 `dev/upstream-patches/0017-dsh-system-prompt-fingerprint.patch`，基线
（现行的 `upstream-src` 资产）：

```text
size:   902658
sha256: c477fc58286bfe05d0e6570227910b614222b8009f472b8c28955f3425fe2b0c
```

重打包后的资产（含 `0018`；312 个文件，**待维护者上传**）：

```text
size:   907152
sha256: b16c081dc70bb1608846190640401f4fac375f4a9f78e2643d062790abf1da8a
```

> 合并本补丁后**必须**先重打包并覆盖上传 `upstream-src` 载体 Release，再发面板版本：
> 发布门禁会校验「随包上游里所有补丁都是 already applied」，资产没更新会直接报
> 「仍有补丁需要现场应用」并阻断发布。

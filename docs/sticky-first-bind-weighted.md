# 会话粘性首绑按三因子加权

Issue #265 的落地形态。

## 问题：粘性一开，「快过期积分优先」就轮不到

上游把「快过期积分优先消耗」写进选号权重（`internal/pool/pick.go` 的 `weightOf`）：

```
权重 = 余额比例 × 10 + 快过期积分占比 × 8 + 闲置补偿（上限见配置）
```

但粘性（`session_sticky.enabled`，默认开）在**首绑**这一步走的是另一条路：
`internal/session/session.go` 的慢路径 `pool2[hashIndex(key, len(pool2))]` —— FNV-1a
取模，只看会话键的字节，看不到积分余额、快过期积分、闲置时长中的任何一项。而带会话标识
的请求（conversation 四键 / `prompt_cache_key` / 首条 user 消息 sha256 兜底）在第一轮就
完成绑定并滚动续期，**永远走不到 `Pool.pick`**。

结果就是权重表里那条「快过期积分 ×8」在默认配置下基本不起作用。实测（三个 cn 账号在线，
`pool.expiring_soon=168h`）：

| 账号 | credits | `credits_expiring` | 占比 | 某 69 分钟内的请求数 |
|------|---------|--------------------|------|----------------------|
| A | 5219 | 1272 | 24.4% | 0 |
| B | 4582 | 1111 | 24.2% | 0 |
| C | 5253 | **0** | 0% | **56（连续）** |

C 没有任何快过期积分，却独占了一整段会话流量。会话持续活跃期间（每 30 分钟内一次请求即
续期）这种独占会一直持续，直到会话结束或绑定号失效。

## 语义变更

**只改首绑**：会话第一次分配到账号时，在既有的候选段内按三因子权重抽取，而不是哈希取模。

```
命中已有绑定  → 照旧（命中即定，不重新加权；这是粘性的本义）
慢路径分配    → 候选段（优先"未被任何会话绑定的空闲号"，其次全池）
                └─ 注入 PickForModel 时：按 credits ×10 + 快过期积分 ×8 + 闲置 加权抽签
                └─ 未注入 / 回调返回空串或候选外 uid：回落 hashIndex（旧行为）
```

## 边界与取舍

- **命中不改**。每次命中都重新加权等于把粘性关掉（上游 prompt cache 会碎）。
- **不做成本分层**。成本分层回答「本次请求走谁更便宜」，是即时决策；首绑回答「这段会话长期
  挂谁」。按单价硬过滤会让全部会话收敛到最便宜的号，反而加剧单一账号垄断 —— 正是本 issue
  要消解的现象。
- **不做 Top-5 截断**。截断必须先排序，而等权重时的排序会系统性偏向候选集里靠前的 uid
  （候选来自 `sort.Strings`），把「加权」悄悄退化成「字典序」。首绑对每个会话只发生一次，
  没有惊群压力，不需要截断。
- **不写 `lastUsed` / `usedSeq`**。绑定不等于消耗：在途租约与使用序号由随后的
  `PickByUIDForModel` / `Acquire` 推进。若在首绑就写 `lastUsed`，一次绑定会把闲置补偿清零，
  而粘性会持续命中这个号，等于整个 TTL 都背着这个惩罚。
- **回调在 Router 写锁内执行**，实现侧不得回调 Router（死锁风险），也不应长时间阻塞。
- **未注入回调 = 旧行为逐字不变**：`session.Config.PickForModel` 为 nil 时仍走
  `hashIndex`，无 pool 依赖的调用方与既有用例零回归。

## 实现位置

- `dev/upstream-patches/0021-sticky-first-bind-weighted.patch`
  - `internal/pool/weightedbind.go`（新增 `Pool.WeightedPickAmong`）
  - `internal/session/session.go`（`Config.PickForModel` + 慢路径分配）
  - `cmd/server/wiring.go`、`cmd/server/main.go`（realm 感知注入）
  - `internal/pool/weightedbind_test.go`、`internal/session/session_pick_test.go`（用例）
  - `README.md`「选号语义」补一句首绑口径
- 面板侧无需改动：它是按发布包随附的上游源码构建的。

## 补丁组成

- `0021-sticky-first-bind-weighted.patch`：功能改动。
- `0022-sticky-gofmt.patch`：仅修正补丁动过文件的 gofmt 对齐（发布门禁会检查）。

## 验证

在已应用 0001–0019 的基线快照上跑 `dev/apply_upstream_patches.py`：

- 首次：`applied: 0021-sticky-first-bind-weighted.patch`；重跑：`already applied`（幂等）
- `go build ./...` 通过
- `go test ./internal/pool/ ./internal/session/ ./cmd/server/` 全通过
- 全量 `go test ./...` 仅 `cmd/stats` 的 `TestBuildFrameStructure` 失败 —— 该用例在
  **未打本补丁的基线快照**上同样失败（既有问题，与本改动无关）

# 模型清单的能力字段（面板侧兜底）

Issue #211 的第一半。上游从 #156 起在自己 `/v1/models` 的条目里补了
`modalities` 与 `supported_reasoning_levels`（第三方模型目录客户端 Magpie 读这两个
字段决定是否展示多模态与推理档位）。

但**面板与上游是两个进程**，而且随包分发的上游版本可能比面板旧 —— 补丁只打在上游时，
下游经面板拿到的清单仍然缺字段，客户端会整条跳过模型。所以面板在自己的
`/v1/models` 上再补一遍。

## 字段口径（只增不改）

| 字段 | 规则 |
|---|---|
| `modalities` | 缺字段时补 `{"input": ["text"]}`；`supports_images == true` 时再追加 `"image"` |
| `supported_reasoning_levels` | 缺字段且有 `reasoning_supported_efforts` 时派生成 `[{"effort": e}]` |
| `supports_reasoning` | 有档位而字段缺失时补 `true` |

三条约束：

1. **只增不改**：上游已经给了的字段原样保留（新上游自己会补，它的口径更权威）；
2. **不编造**：没有推理档位就不写 `supported_reasoning_levels`；模型条目缺 `id`
   （认不出是什么）时整条原样透传；
3. 结构不是 `{data: [...]}` 时原样返回 —— 不在看不懂的响应上动手脚。

## 为什么同一套逻辑要写两遍

选号、模型探测、能力字段的**权威来源**都在上游；面板侧这份是**展示层兜底**，不是第二个
事实来源。两处必须同步修改的点只有一个：字段名与派生规则。改这里时请同时看
`internal/server/handler.go` 的 `addListingCompat`。

## 实现位置

- 面板：`server/routers/gateway.py` 的 `_listing_compat_models`（在 `_scope_models`
  之后调用，别名补录的条目也会一并补字段）；
- 用例：`server/tests/test_models_listing_compat.py`（多模态 / 纯文本 / 上游已给 /
  认不出结构四种形态）；
- 上游：`internal/server/handler.go`（#156 起，随包资产已含）。

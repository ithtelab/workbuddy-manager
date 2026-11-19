# 账号权重（手写乘数）

Issue #187 的落地形态。用户原话是「希望能给账号加权重，权重高的优先使用该账号」，
同时提到「或者优化一下功能，有积分快过期的账号优先使用」。第二半上游**早就在做**
（选号权重里有「快过期积分占比」一项），这里补的是第一半。

## 语义：乘数，不是优先级

上游选号（`internal/pool/pick.go` 的 `weightOf`）本来就是三因子加权随机：

```
权重 = 余额比例 × 10 + 快过期积分占比 × 8 + 闲置补偿（上限见配置）
```

手写权重把上面这个结果**再乘一遍**：

```
最终权重 = 三因子权重 × 手写权重（未设置 = 1.0）
```

**为什么不做成「权重高的恒定优先」**：那会让低权重的号一直闲置，积分用不完就过期 ——
而「别让快过期的积分浪费」正是同一条 issue 的另一半诉求。乘数既能表达「这个号多用点」，
又保留了三因子的兜底（久置的号权重会自然涨上来）。

它**不改变可用性**：冷却、熔断、上游禁用、本面板停用、在途占满、成本分层（优先免费号）
全都在选号之前过滤，调大权重也不能把不可用的号塞进候选。

范围 `0.1–100`，越界按边界钳制（`auth.PickWeight`），非法值（负数 / NaN）按「未设置」
处理。`1` 是默认值，写 `0` 等价于删掉这个键。

## 存在哪

上游 auth 文件**顶层** `weight`（与 `proxy`、`device_token` 同级）：

```json
{
  "auth": {"accessToken": "...", "refreshToken": "...", "expiresAt": 0, "domain": "...", "realm": "cn"},
  "account": {"uid": "...", "nickname": "..."},
  "weight": 2
}
```

两个必须注意的点：

1. **刷新 token 会整份重写文件**。不把 `weight` 写回就等于静默清零 —— 与
   `docs/account-egress-routes.md` 里 `proxy` 踩过的是同一个坑，所以 `SaveAtomic`
   也保留它（非零才写，未设置的文件不会凭空多出 `weight: 1`）。
2. **改完要重载上游**：权重是加载时 `auth.Parse` 解析进内存的。面板保存后会请求重载，
   与「出口线路」同一套机制。

## 面板里怎么用

账号页每个账号的操作条 → 天平图标（权重）。填 2 = 同等条件下被抽中的机会约为默认的
两倍；填 1 或留空 = 恢复默认。设过非 1 的权重时图标染色，便于一眼看出哪些号被特殊对待。

多分组账号池下，权重改的是**该分组**目录里的那个账号文件。

## 实现位置

- 上游：`dev/upstream-patches/0008-account-manual-weight.patch`（`internal/auth` 解析与
  保留、`internal/pool/pick.go` 乘数、Go 用例），基线是本版上一份 `upstream-src` 资产；
- 面板：`server/services/wb2api.py`（`set_account_weight` / 列表回显 `weight`）、
  `PUT /api/accounts/{filename}/weight`、`web/components/common/accounts/AccountWeightDialog.tsx`。

## 资产基线 → 产出

- 基线：v2.0.0 的 `upstream-src` 资产（886,460 字节，
  `sha256 d14033388f11fd8e03f9b318fbf9e252360bfd1713524d651dcf03b0efe853ca`）；
- 产出：应用 0008 后重打的资产（889,722 字节，
  `sha256 5feccd84e00d3ebc9afa98eb973a0a7977d397f60a087c7b5ed1fc9aff6d1623`，
  305 个文件、0 个 CRLF）；
- 重入验证：对产出再跑一次 `apply_upstream_patches.py`，全部报 `already applied:`。

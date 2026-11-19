# 渠道校验（11-128 家族）与 /console 兜底

上游 2026-10-17 起给 `/v2/chat/completions` 加了**渠道强校验**：被校验到的请求会回

```json
{"code":"11-128","msg":"request illegal","displayTips":{
  "zh":"这次调用没通过渠道校验，可能不是官方客户端发出的。请用官方版本重新发送。",
  "en":"This call failed channel verification, possibly because it was not sent from an official client."}}
```

而官方客户端走的 `/console/chat/completions` **正常放行**（报告方实测：换过去连通性测试与
对话全部恢复）。用户报告见 issue #193（会话头族）、#201（分类）与本次的现场：
**一批账号在面板上显示「已禁用（需重新登录）」，但账号本身完全正常**。

## 三个缺陷与修法

1. **同一个码、不同 msg，被按 msg 分野误判**
   渠道校验的封套里可能带历史遗留的 `msg: request illegal` —— 而 `request illegal` 这条
   历史语义是「账号被封，需重登」，会被**硬禁用**账号。于是被校验到的号在面板上变成
   「已禁用（需重新登录）」，用户重扫也没用（凭证从来没问题）。
   → 新增 `upstream.IsChannelCheck`：**业务码（`11-128` 与报告方另见的无横线写法，两种都认）
   且文案含「渠道校验 / channel verification / official client」**同时成立才算渠道校验。
   判在 `request illegal` 之前 —— 渠道校验**永不禁用**。
   2026-10-28 起连软冷却也去掉了（issue #324）：渠道校验拒的是**这次请求的报文**，
   不是账号；给它写 10 分钟冷却会让一个坏请求把整池账号挨个冷却一遍。现在只做
   一次请求级抢救（把 system 文本搬进首条 user 后重试），仍被拦就直接把上游原文
   透传给客户端（错误码 `channel_check_rejected`），不冷却、不禁用、不换号。

2. **不换路径就整池被拦**
   `chatPaths` 加 `/console` 兜底：**主路径仍是 `/v2`**（#119 实测 `/console` 挂腾讯云 WAF
   的 body 内容规则，反引号命令特征会 403），只在 `/v2` 被判渠道校验时换过去。
   触发条件**只有渠道校验**，不含 404/405 —— 那两条不回落是 #119 的刻意取舍，契约由
   `TestGlobalChatNoFallbackOn404` 钉着。

3. **面板探测只试 `/v2`，于是好号也报红**
   `realm.chat_paths` 与 `probe_account` 同步加上兜底与同样的触发条件；两条都被拦时
   明说「账号本身没问题」，不把用户往「重扫登录」引。

## 已经误禁用的账号怎么恢复

面板账号行上的「启用」按钮在后端会一并解除上游的系统禁用位（`revive`）。若提示需要
先开启上游管理接口，到「设置 → 账号管理接口」打开后重试即可。

## 上游源码发布条件

实现位于 `dev/upstream-patches/0006-channel-check-console-fallback.patch`
（请求级语义见后面的 `0039-channel-check-request-scope.patch`），基于

```text
size:   874638
sha256: 29718acd31a5e9aa41b0dfb2829a70cfa2852e5410836e76248a32ae76371f56
```

应用后重打包得到的资产（**已上传**）：

```text
size:   878890
sha256: 587b0abd7b112de18fef4b40d2b08cea3dbc94575f2136e4508f0c2724688447
```

## 补丁流水线的一处加固

加第 6 个补丁时踩到：新补丁改动前一个补丁的**上下文邻域**，`git apply --reverse --check`
失败，于是重打包报「patch does not match this snapshot」——而内容其实完全正确。
`dev/apply_upstream_patches.py` 的「已应用」判定改为**按新增行内容**判定（反向 apply 仍
作兜底），并用 `server/tests/test_apply_upstream_patches.py` 钉住这个语义（含上下文漂移
的用例）。顺序切片的补丁天然会有这种漂移，判定不该依赖上下文精确匹配。

# 积分耗尽冷却（cooldown.credit_exhausted）

上游原来对「积分耗尽」（`ErrHardCredit`）一律把账号**硬冷却到次日 04:00**，理由是等
签到（09:00 / 21:00）把积分补回来。问题是这一整天里那个账号完全不出池 —— 用户中途
充值、或上游提前刷新了额度，也用不上它。

这是 workbuddy2api 上游的可选配置，键为 `cooldown.credit_exhausted`；**留空 = 保持
原行为**，填了才改变。

## 配置

在「设置 → 限流与冷却」的「积分耗尽冷却」填写，或在上游 config.json 中设置：

```json
{
  "cooldown": {
    "credit_exhausted": "10m"
  }
}
```

- **留空（默认）**：硬冷却到次日 04:00（`CoolHard`）。升级后行为**逐字不变**——
  这是这项功能唯一的兼容承诺，`internal/pool/credit_exhausted_test.go` 用测试钉住。
- **填正时长**：改用**软冷却**（`CoolSoft`），时长就是配置值，到期即回到候选池。
- **不能填 `0` / `0s`**：补丁里判 `d <= 0` 时直接报错。Go 的 `time.ParseDuration`
  认 `0`，所以「填 0 = 关掉」这个习惯在这里会让上游**起不来**（面板与后端都会拦）。
- 上限 8760h（一年）：再长与「不启用」无区别，超限同样报错。
- 软冷却不做指数堆加（见该配置的用法说明），所以连续触发**不会**被逐次拉长。

环境变量 `WB2A_CREDIT_EXHAUSTED` 可覆盖同一项（与其它 cooldown 项一致）。

## 上游源码发布条件

实现位于 `dev/upstream-patches/0004-credit-exhausted-cooldown.patch`。它基于下面这份
`upstream-src` 资产（含 0001–0003）生成并验证：

```text
size:   871671
sha256: b9946da7d53afc14eb585bbe3a31af7aac2c74361dc6ffeacb146a5ee5dd8454
```

应用该补丁后重打包得到的资产（**已上传**，即用户实际拿到的源码）：

```text
size:   873660
sha256: ef292dddcf7a5f5df73e7a91f19da1880836aee12b53a9d07d3ce3e02c90b34c
```

**只合并面板字段不会让旧 Go 服务支持这一项。** 旧上游会忽略未知字段
（`cooldown.credit_exhausted` 不会报错），所以「保存成功」不等于功能已生效 ——
面板文案已按此写明，发版说明也注明了需要配套上游。

## 维护者复现步骤

```sh
python3 dev/apply_upstream_patches.py /path/to/upstream-src-extract   # 0001-0003 already applied, 0004 applied
(cd /path/to/upstream-src-extract && go build ./...)
(cd /path/to/upstream-src-extract && go test ./...)
(cd /path/to/upstream-src-extract && go test -race ./internal/pool ./cmd/server)
python3 dev/pack_fixed_upstream_src.py /path/to/upstream-src-extract -o /path/to/output \
  --stamp 'Add cooldown.credit_exhausted: shorten credit-exhausted cooldown instead of cooling until 04:00'
gh release upload upstream-src /path/to/output/workbuddy2api-src.tar.gz --clobber
```

已验证：补丁在资产基线上**首次应用**成功、再次应用被识别为「已应用」、重打包后的包
可构建、`go test ./...` 全绿（含 `-race` 下的 `internal/pool` 与 `cmd/server`）、包内
`scripts/` 的 Python 用例通过、上传后的资产大小与 sha256 与本地一致。

补丁含配置校验、池内冷却语义与回归用例。源码漂移时沿用现有失败门禁：`apply_upstream_patches.py`
会明确报「does not match this snapshot」，需复核并重做补丁，不要跳过。

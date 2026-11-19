# 会话头族：出站补齐 X-Conversation-ID（11-128 渠道校验）

上游（腾讯侧）2026-10-16 起对**部分账号灰度启用**了渠道校验：官方客户端出站带一整套
会话头族，网关转发的请求不带，于是被判「非官方客户端」并返回：

```json
{"code":"11-128","msg":"conversation id missing",
 "displayMsg":{"zh":"请求被拦截，请重新发送"},
 "displayTips":{"zh":"这次调用没通过渠道校验，可能不是官方客户端发出的。请用官方版本重新发送。"}}
```

被灰度到的账号在池里**每次被选中都失败**（实测同一账号必拦、未灰度账号零交叉），
表现是「账号在官方客户端能正常用、经网关必失败」。用户报告见 issue #193，错误分类
层面的问题见 issue #201。

## 改了什么

1. **出站不再跳过 `X-Conversation-ID`**（`internal/session/ids.go` +
   `internal/server/handler.go`）：客户端带了 conversationId 就原样透传；**没带就派生
   一个同一会话稳定的值**（锚点 = 首条 user 消息 → 会话键 → 请求级随机）。

   原行为是「客户端没给就不发，避免误导后台建错会话」—— 灰度推开后这条等于把被灰度
   的账号判了死刑。派生值同为 32 hex，与 `X-Conversation-Request-ID`（轮级）语义区分。

   **只补头、不往 body 里塞 `conversationId`**：报告方的剥离实测（issue #193 评论）表明
   校验对象是头族，`全头族 + 不带 body conversationId` 就是 200。

2. **错误分类**（`internal/server/handler.go` + `internal/upstream/client.go`）：

   - `11-128 conversation id missing` 归入**账号级**（此前落通用 4xx「只换号不罚号」，
     坏号留在池里"显示健康、每次被选中都失败"——报告里 14 个号中 7 个被拦死却只有 2 个
     被禁用）。处理为**软冷却**而非硬禁用：灰度随时可能放开，禁用会让用户白重新登录。
   - 禁用/冷却的理由里**带上响应自己的业务码与状态码**（新增 `upstream.CodeOf`，
     容忍上游偶发的非法 JSON 形态 `"code":11-128`）：此前理由里写死 `11140`，
     而真实响应是 `http 400 + code 11-128`，排障按 11140 去搜方向全错（#201 缺陷 1）。

3. **面板的账号探测**（`server/services/tencent.py`）：探测请求同样补齐会话头族 ——
   否则被灰度到的账号在「账号测试」里必红，而那是**探测形态**的问题、不是账号坏了。

## 上游源码发布条件

实现位于 `dev/upstream-patches/0005-conversation-id-header-family.patch`。它基于下面这份
`upstream-src` 资产（含 0001–0004）生成并验证：

```text
size:   873660
sha256: ef292dddcf7a5f5df73e7a91f19da1880836aee12b53a9d07d3ce3e02c90b34c
```

应用该补丁后重打包得到的资产（**已上传**，即用户实际拿到的源码）：

```text
size:   879966
sha256: d05ee0a069e48e55fbf45a70bc599e00add7b9fab49d9c2a85be1cf87dd4f7a9
```

**只改面板不会让旧上游补上这个头族**：必须随包更新上游源码（本补丁），
或让面板走一次「更新」把新上游装上。

## 维护者复现步骤

```sh
python3 dev/apply_upstream_patches.py /path/to/upstream-src-extract   # 0001-0004 already applied, 0005 applied
(cd /path/to/upstream-src-extract && go build ./... && go test ./...)
(cd /path/to/upstream-src-extract && go test -race ./internal/session ./internal/upstream ./internal/server)
python3 dev/pack_fixed_upstream_src.py /path/to/upstream-src-extract -o /path/to/output \
  --stamp 'Fill conversation header family (X-Conversation-ID derived when absent) and classify 11-128 account-level'
gh release upload upstream-src /path/to/output/workbuddy2api-src.tar.gz --clobber
```

已验证：首次应用成功、**再次应用全部识别为「已应用」**（重打包脚本走的就是这条路径）、
重打包后源码可构建且 Go 全量通过、包内含新增的两个测试文件、上传后 size/sha256 与本地一致。

> 打补丁时注意：**不要在既有文件上跑 `gofmt -w`**。它会顺带把资产里的历史缩进/注释空格
> 一起格式化，那些白空格改动会破坏**其它补丁的反向校验**（0002 的上下文正落在
> `handler.go` 的 `out = append(out, entry)` 那一行），于是 `apply_upstream_patches.py`
> 在已打补丁的资产上重跑时报「patch does not apply」——重打包直接失败。
> 只格式化新文件，或在 diff 里逐条剔除这类改动。

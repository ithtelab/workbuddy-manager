# 企业账号额度支持补丁包

让 `ithtelab/workbuddy-manager` + `workbuddy2api` 支持**腾讯 CodeBuddy/WorkBuddy 企业版账号**的额度显示与选号。

> 上游（原版）不支持企业账号额度：企业额度不在个人「资源包」体系内，
> 个人接口 `get-user-resource` 对企业账号恒返回空 `Accounts`，导致面板显示 0，
> 且因 `credits=0` 该账号在加权随机选号中权重极低、几乎不被选中。

---

## 目录结构

```
enterprise-support/
├── README.md                                本文件
├── apply.sh                                 一键应用脚本
├── workbuddy2api-enterprise-support.patch   上游 Go 补丁（6 文件 / 307 行）
├── manager-tencent.py.patch                 manager 额度查询补丁（103 行）
├── manager-wb2api.py.patch                  manager 签到判定补丁（14 行）
└── verified/                                已验证的成品文件（可直接覆盖）
    ├── manager-tencent.py
    ├── manager-wb2api.py
    └── upstream-enterprise_usage.go
```

---

## 快速应用

```bash
cd /opt/enterprise-support

# 全部应用（上游 + manager）
sudo bash apply.sh all

# 只应用其中一个
sudo bash apply.sh upstream
sudo bash apply.sh manager
```

脚本会自动：
1. 备份现有二进制 / patched 文件
2. 应用补丁（上游用 `git apply`，manager 用文件覆盖）
3. 编译 + 测试（上游）
4. 重建镜像 / 容器
5. 打印验证结果

---

## 手工应用（脚本失败时）

### 上游 workbuddy2api

```bash
cd /opt/workbuddy2api-build

# 1. 备份
cp build-wb2api build-wb2api.bak-$(date +%Y%m%d%H%M%S)

# 2. 应用 patch
git apply /opt/enterprise-support/workbuddy2api-enterprise-support.patch
#   若 git apply 报冲突：用 git apply --3way 或手动按 patch 逐处改

# 3. 编译
export PATH=/tmp/go/bin:$PATH GOPROXY=https://goproxy.cn,direct GOFLAGS=-mod=mod
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o build-wb2api ./cmd/server

# 4. 测试
go test ./...

# 5. 重建镜像并重启
docker build -f Dockerfile.local -t workbuddy2api:local .
docker compose -f docker-compose.local.yml up -d --force-recreate wb2api
```

### manager

```bash
# 1. 写入补丁文件
mkdir -p /opt/workbuddy-manager/patches
cp /opt/enterprise-support/verified/manager-tencent.py /opt/workbuddy-manager/patches/tencent.py
cp /opt/enterprise-support/verified/manager-wb2api.py /opt/workbuddy-manager/patches/wb2api.py

# 2. 重建容器（必须带 volume 挂载）
docker rm -f workbuddy-manager
docker run -d --name workbuddy-manager \
  --restart unless-stopped --network host \
  -e WB_MANAGER_HOST=0.0.0.0 -e WB_MANAGER_PORT=7864 \
  -e WB2API_BASE=http://127.0.0.1:7863 \
  -e WB2API_KEY=test_key \
  -e WB_UPSTREAM_CONFIG=/opt/workbuddy2api/config.json \
  -e WB_AUTH_DIR=/opt/workbuddy2api/auths \
  -v /opt/workbuddy-manager/data:/app/data \
  -v /opt/workbuddy2api-build/auths:/opt/workbuddy2api/auths \
  -v /opt/workbuddy2api-build/config.json:/opt/workbuddy2api/config.json:ro \
  -v /opt/workbuddy-manager/patches/tencent.py:/app/server/services/tencent.py:ro \
  -v /opt/workbuddy-manager/patches/wb2api.py:/app/server/services/wb2api.py:ro \
  ghcr.io/ithtelab/workbuddy-manager:latest
```

---

## 补丁内容说明

### 上游 workbuddy2api（Go）

| 文件 | 改动 |
|---|---|
| `internal/upstream/enterprise_usage.go` | **新增**。`IsEnterprise()` 判定 + `enterpriseUsage()` 走企业端点 + 时区解析 |
| `internal/upstream/client.go` | `UserResourceDetailed()` 增加企业分支（`+24` 行） |
| `internal/scheduler/scheduler.go` | **新增** `RefreshBalances()`：只查余额、不签到、不改 token（`+51` 行） |
| `internal/server/handler.go` | **新增** `POST /v1/balance/refresh` 路由与实现（`+42` 行） |
| `cmd/server/main.go` | 注入 `Scheduler` 到 Handler Config（`+2` 行） |
| `internal/upstream/enterprise_usage_test.go` | **新增** 4 个单元测试（`+93` 行） |

**核心口径**：
```
个人接口: remain = CapacityRemain
企业接口: remain = limitNum - credit    ← credit 是本周期「已用」，与个人相反
          limitNum == -1 → 不限量
```

**⚠️ 关键坑（务必注意）**：
`billingJSON → doJSON` **已经解过信封**并返回 `env.Data`。
解析时不能再套 `{code, msg, data}` 结构，否则 `LimitNum`/`Credit` 全落零值、
额度恒为 0 且不报错。正确做法是直接解析 data 本体：

```go
var resp enterpriseUsageData   // ← 只有业务字段，无 code/msg
json.Unmarshal(data, &resp)
```

### manager（Python）

| 文件 | 改动 |
|---|---|
| `services/tencent.py` | `fetch_credits()` 增加企业分支 → `_fetch_enterprise_credits()`（走企业端点、UTC+8 时区解析周期） |
| `services/wb2api.py` | `checkin_supported` 按 `enterpriseId` 判定为 `False` |

**为什么签到要禁用**：企业账号没有个人成长体系，签到端点恒返回
`400 code=10001「企业账号不支持该操作」`。禁用后面板不渲染签到按钮、
定时任务也不会发起无谓请求。

---

## 使用

### 手动刷新余额（不签到）

```bash
curl -X POST http://127.0.0.1:7863/v1/balance/refresh \
  -H "Authorization: Bearer <上游 api_key>"
```

返回：
```json
{
  "total": 3,
  "succeeded": 3,
  "accounts": [
    {"uid": "...", "nickname": "1501483050822569239", "credits": 1658, "expiring": 0},
    {"uid": "...", "nickname": "LzZ", "credits": 899, "expiring": 0}
  ]
}
```

用途：上游 `credits` 只在签到定时任务（9/21 点）刷新，两次签到之间面板看到的是旧值。
调这个端点可以立刻刷新。

---

## 验证结果（2026-10-18 实测）

| 账号 | enterpriseId | 面板 credits | checkin_supported |
|---|---|---|---|
| 1501483050822569239 | `g12ad6q2qwao` | **1658** ✓ | **False** ✓ |
| LzZ（个人） | — | 899 | True |
| 阿白（个人） | — | 600 | True |

企业额度核对：
- 腾讯端点返回 `credit=342, limitNum=2000`
- `2000 − 342 = 1658` ✓ 与面板一致

---

## 回滚

### 上游

```bash
cd /opt/workbuddy2api-build
# 方式一：git 回退
git checkout 33d1414 -- .
git clean -fd internal/upstream/enterprise_usage.go internal/upstream/enterprise_usage_test.go

# 方式二：直接用备份二进制
cp build-wb2api.bak-* build-wb2api
docker build -f Dockerfile.local -t workbuddy2api:local .
docker compose -f docker-compose.local.yml up -d --force-recreate wb2api
```

### manager

只需移除 volume 挂载重建容器（原镜像文件未被修改）：

```bash
docker rm -f workbuddy-manager
# 重建时去掉两行 -v .../patches/... 挂载
```

---

## 升级注意事项

- **上游升级会覆盖补丁**：`workbuddy2api:local` 是本地构建的镜像。重装或升级后
  需重新应用 patch。源码 git 仓库保留了全部 commit，可用
  `git cherry-pick a80939d c4a3a48 3f91f42 b2b3a49`。
- **manager 补丁不受镜像升级影响**：它是 volume 挂载，容器重建后照常生效；
  但如果 manager 升级后 `tencent.py` / `wb2api.py` 接口有变动，补丁可能失效，
  需要重新生成。
- **企业账号不支持签到是腾讯侧限制**，无法绕过。

---

## 参考

- 企业额度端点：`POST /v2/billing/meter/get-enterprise-user-usage`
- 请求头：`X-Enterprise-Id` / `X-Tenant-Id`（上游 `BillingHeaders` 已自动带上）
- 响应：`{code, msg, data: {credit, limitNum, cycleStartTime, cycleEndTime, cycleResetTime}}`

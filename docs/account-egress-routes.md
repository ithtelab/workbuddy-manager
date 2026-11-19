# 账号级出口线路（上游侧真正生效）

面板从 1.0.85 起就能给账号绑命名线路（账号页 → 线路），但**上游此前完全不读**这个字段：
出站只有 `ProxyFromEnvironment`（进程级），于是所有账号共用宿主出口 IP —— 界面上看起来
已经隔离，实际没有。多号同 IP 正是平台最容易关联的特征。用户报告见 issue #210。

## 怎么配

1. 上游 `config.json` 增加 `proxies` 段（线路名 → 代理地址）：

   ```json
   {
     "proxies": {
       "hk-1": "socks5h://user:pass@hk1.example:1080",
       "us-2": "http://us2.example:8080"
     }
   }
   ```

   支持 `http` / `https` / `socks5` / `socks5h`（`socks5h` 会归一成 `socks5`：Go 的
   socks5 拨号本身把域名交给代理解析）。地址写错时**上游启动就报错**（`proxies[name]: …`），
   不会带病运行。

   也可以用环境变量 `WB2A_PROXIES` 传同一份 JSON（面板部署里就是 upstream 容器的环境变量）。

2. 面板账号页给账号选线路（写进 `auths/*.json` 顶层的 `proxy`，值是**线路名**）。

3. 重启上游。启动日志会打一行：

   ```
   出口线路已加载：2 条（hk-1, us-2）—— 账号绑定的线路名按它解析
   ```

## 生效范围

| 出站 | 走账号线路 |
|---|---|
| 对话（`/v2`、`/console` 兜底） | ✅ |
| 签到 / 积分 / trial / 活跃上报 / 猫猫旅行（billing、growth 域） | ✅ |
| 令牌刷新 | ✅ |
| 模型目录探测 | ❌（进程级默认出口；它不带账号身份） |
| `scripts/` 里的 Python 任务脚本 | ❌（独立进程，尚未接线路；面板侧的签到/查积分等直连动作本来就按账号走） |

**连接池按线路分开缓存**：共用一份会把 A 线路的空闲连接复用给 B 线路，出口会串。

## 行为约定

- 账号**没绑**线路 → 完全沿用原来的共享 client（进程级环境代理），行为一字不变。
- 账号绑了**不存在的**线路名 → 该账号的请求**明确报错**（错误里带账号标签与线路名），
  不会静默直连 —— 静默直连会给出「以为隔离了其实没隔离」这个最坏结果。
- 账号绑了线路但上游一条都没配 → 错误提示会直接说「上游没有配置任何线路（config.json
  的 proxies 段）」。

## 维护者：这一改动在上游补丁里

`dev/upstream-patches/0007-account-egress-and-request-level-errors.patch`（与
#203 空消息 400、#207 日志列宽同一补丁）。涉及 `internal/auth`（解析并**在刷新
token 时保留** `proxy` 字段）、`internal/upstream/transport.go`（线路表与按线路缓存）、
`internal/upstream/client.go`（按账号取 client）、`cmd/server/config.go`（校验与启动日志）。

当前资产与验证流程见 `docs/asset-line-endings.md` 末尾的复现步骤（补丁系列、重打包、
`grep -rlU $'\r$'` 检查）。

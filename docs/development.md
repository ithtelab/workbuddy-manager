# 开发与部署细节

> 从 README 拆出来的三节（本地开发 / 架构 / 目录结构），内容不变。

### 一、本地开发

```bash
# 0) 可选：本机没有真实的 workbuddy2api 时，起一个模拟上游
#    自带模型列表与示例账号，便于查看完整界面
python dev/mock_upstream.py         # 监听 127.0.0.1:7863

# 1) 后端（终端 A）
python -m pip install -r server/requirements.txt
WB_ADMIN_PASSWORD=admin123 \
WB_DATA_DIR=./data \
WB_AUTH_DIR=/opt/workbuddy2api/auths \
WB2API_BASE=http://127.0.0.1:7863 \
python -m uvicorn server.main:app --reload --port 7864

# 2) 前端（终端 B）—— next dev 会把 /api、/v1 反代到 :7864
cd web
npm install
npm run dev                          # http://localhost:3000
```

> **要预览「打包后的界面」时必须用 `npm run build:export`**：静态导出只在
> `NEXT_OUTPUT_EXPORT=1` 时开启，直接跑 `next build` 只会更新 `.next/`，而
> `web/out/` 会**保持上一次的旧产物** —— 界面看起来像「改动没生效」，其实是被过期
> 的产物骗了（浏览器验收脚本读的就是 `web/out/`，踩过一次）。

首次启动会自动生成 `users.json` 与随机签名密钥。若未设置 `WB_ADMIN_PASSWORD`，会在日志中打印一次随机管理员密码。

> **代理注意事项**
> 若本机装有代理软件（Clash / V2Ray 等），**尤其是 TUN 模式**，访问 `127.0.0.1:7863` 可能被代理劫持，表现为接口长时间无响应。
> 本项目对内部请求默认 `trust_env=False`（不读取系统代理）；确需走代理时设置 `WB_HTTP_PROXY`。
> TUN 模式下请在代理软件中把 `127.0.0.1` 加入直连 / 绕过列表。

#### 运行测试

```bash
# 配置读写的回归测试：时刻数组 / 时长字符串 / 部分提交不覆盖同段其他键
python -m unittest discover -s server/tests -t . -v
```

> 测试用临时目录模拟上游 `config.json`，不触碰真实配置，可安全反复运行。
> 这一组测试专门守住两个曾经写坏配置的坑：把整点数组当成数字间隔、
> 把时长字符串当成秒数。

# 架构

```
   下游客户端 / sub2api（OpenAI SDK）
              │  Authorization: Bearer wbk_xxx
              ▼
   ┌──────────────────────────────────────────────┐
   │  WorkBuddy Manager                     :7864 │
   │  ┌────────────────────────────────────────┐  │
   │  │ 反代网关  /v1  /v2  /healthz           │  │
   │  │  密钥鉴权 → IP 管控 → 模型映射          │  │
   │  │  → 流式转发 → 日志与用量落库(SQLite)    │  │
   │  ├────────────────────────────────────────┤  │
   │  │ 管理接口  /api/*                       │  │
   │  │  登录 / 账号 / 密钥 / 日志 / 用量        │  │
   │  │  / 安全 / 设置                          │  │
   │  ├────────────────────────────────────────┤  │
   │  │ Web 前端（Next.js 静态导出）            │  │
   │  └────────────────────────────────────────┘  │
   └───────────────┬──────────────────────────────┘
                   │ 复用 auths/*.json  调用 /status /v1/models
                   ▼
   ┌──────────────────────────────────────────────┐
   │  workbuddy2api（Go，不改动）            :7863 │
   │  账号轮询 · 并发调度 · 熔断 · 令牌刷新          │
   └───────────────┬──────────────────────────────┘
                   ▼
        腾讯 CodeBuddy / copilot.tencent.com
```

**单进程单端口**：前端由 `next build` 静态导出，交由 FastAPI 托管，`/api` 与 `/v1` 同源，无需 CORS。

**技术栈**

| 层 | 选型 |
|---|---|
| 前端 | Next.js 15（App Router）· React 19 · TypeScript · shadcn/ui · Tailwind CSS v4 · motion · recharts · sonner |
| 后端 | Python 3.11+ · FastAPI · uvicorn · httpx · SQLite（标准库，无重依赖） |
| 部署 | systemd 常驻 + 1Panel 反向代理 + Let's Encrypt HTTPS |

---

# 目录结构

```
workbuddy-manager/
├─ server/                       # FastAPI 后端
│  ├─ main.py                    # 入口：路由注册 + 静态托管
│  ├─ config.py                  # 全部环境变量与 http_client 工厂
│  ├─ db.py                      # SQLite（密钥/日志/用量/IP/设置）
│  ├─ security.py                # PBKDF2 + 签名 Cookie + 防爆破
│  ├─ keysvc.py                  # 密钥生成、校验、限额判定
│  ├─ iputil.py                  # 真实 IP 解析 + CIDR 匹配
│  ├─ services/
│  │  ├─ tencent.py              # 腾讯登录 / 签到 / 探测协议
│  │  └─ wb2api.py               # workbuddy2api 交互（含配置写入校验）
│  ├─ tests/                     # 配置读写回归测试
│  └─ routers/                   # auth accounts keys logs stats security settings gateway
├─ web/                          # Next.js 15 前端
│  ├─ app/(main)/                # dashboard accounts keys logs stats security settings
│  ├─ app/(auth)/login/          # 登录页
│  ├─ components/ui/             # shadcn 原语（含 floating-dock）
│  └─ components/common/         # 浮动底栏、统计卡、各业务组件
├─ dev/mock_upstream.py          # 本地联用的模拟上游
├─ deploy/                       # systemd unit + 一键部署脚本
└─ docs/                         # 设计与实现文档 + 界面截图
```

---

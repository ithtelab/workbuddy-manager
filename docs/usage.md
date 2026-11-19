# 使用指南

> 从 README 拆出来的一节，内容不变。日常三步：纳管账号 → 分发密钥 → 下游接入；
> 另外还有密钥自查用量（第 3 节）——下游自己就能查，不用来问你。

### 1. 纳管账号

登录后进入「账号」页，点右上角 **添加账号** → 用微信 / QQ 扫码 → 授权成功后自动签到、落盘并重载上游容器。

### 2. 分发密钥

进入「密钥」页点 **新建密钥**，按需设置：

- **限定版本** —— 国内版密钥只能调国内版模型，国际版密钥只能调 `global:` 开头的
  国际版模型，跨版本调用会被拒绝（`/v1/models` 也只返回对应版本的模型）。
  默认跟随当前所在版本；选「不限制」则两版都能调
- **绑定上游** —— 在「设置 → 上游」里配了多个上游之后，这里可以选一个：这把密钥的
  请求只走那个上游的账号池（不选 = 默认上游，即 `WB2API_BASE` 那一套）。
  把密钥分给不同的人 / 业务时，用它做账号池隔离
- **有效期** —— 留空或 0 表示永不过期
- **最大 IP 数** —— 限制同一密钥可使用的来源 IP 数量
- **IP 白名单** —— 更严格，仅允许指定 IP / CIDR 调用
- **模型白名单** —— 在所选版本之内再限制到具体模型
- **Token 配额** —— Token 用尽后自动拒绝（按 prompt + completion 累计）
- **积分配额** —— 按**真实扣费**累计的额度，用尽后同样拒绝（429）。

  为什么除了 Token 还要有积分：**同样 1M Token，便宜模型与贵模型的扣费能差
  几十倍**，拿 Token 数当预算估不出实际花了多少。数据来自上游每次响应末帧
  `usage.credit`（真实扣费，不是估算），与「用量统计」页看到的是同一份。

  两个额度**各自独立**，任一超限即拒绝，都留 0 = 不限。上游没返回该字段的调用
  **不计入**（而不是按 0 记）：那代表「不知道扣了多少」，按 0 记等于把它当免费，
  数字会假装准确。代价是老版本上游（2026-09-13 之前）下这个额度**不会增长**，
  界面上用量一直是 0 —— 那种情况下请用 Token 配额。

密钥明文**只在创建时展示一次**，请立即保存。

### 3. 密钥自查用量

下游拿到密钥后可以**自己查还剩多少**，不用登录面板：

```bash
curl https://wb.example.com/v1/usage -H "Authorization: Bearer wbk_xxxxxxxx"
```

返回这把密钥的已用 / 上限 / 剩余（Token 与积分各一组，上限为 `null` 表示不限额）、
有效期与是否已过期、绑定的模型范围。它读的是**面板已经记下的数字**，查询本身
不会向腾讯发起任何请求、也不算一次调用 —— 所以额度用尽、已过期或被停用时
**照样能查**，正好用来确认「调不动」是哪种原因。适合写进自己的监控脚本。

### 4. 下游接入

完全兼容 OpenAI 协议，Base URL 指向本服务的 `/v1`：

```bash
curl https://wb.example.com/v1/chat/completions \
  -H "Authorization: Bearer wbk_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.2",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

Python 示例：

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://wb.example.com/v1",
    api_key="wbk_xxxxxxxx",
)

resp = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

> 流式请求会自动注入 `stream_options.include_usage=true`，以便精确统计 Token 消耗。

<details>
<summary><b>OpenAI Responses API（Codex / DeepSeek Harness 等）</b></summary>

本服务同时提供 Responses 协议（`/v1/responses`，SDK 的 `baseURL` 不含 `/v1`
时也支持 `/responses`）。请求体用 `input` 而非 `messages`，`instructions` 承载
system 文本：

```python
from openai import OpenAI

client = OpenAI(base_url="https://wb.example.com/v1", api_key="wbk_xxxxxxxx")

resp = client.responses.create(
    model="glm-5.2",
    instructions="你是一个简洁的助手",
    input="你好",
    stream=True,
)
for event in resp:
    if event.type == "response.output_text.delta":
        print(event.delta, end="")
```

工具调用同样支持：客户端发扁平形状的 `tools`（`{type:"function", name, parameters}`），
返回的是 `function_call` 输出项与 `response.function_call_arguments.delta` 事件。

> 只发 `openai-responses` 协议的客户端（如 DeepSeek Harness 的自定义提供方）
> 把「API 协议」选成 `openai-responses` 即可；它谈的协议与
> `openai-completions` 不是同一个，需要单独建一个提供方。

</details>

<details>
<summary><b>Anthropic Messages API（Claude Code / Cursor / Cline 等）</b></summary>

只认 Anthropic 协议的客户端可直接把 Base URL 指向本服务——`ANTHROPIC_BASE_URL`
配到根路径即可（协议层面 `/v1/messages` 与官方一致）：

```bash
export ANTHROPIC_BASE_URL=https://wb.example.com
export ANTHROPIC_AUTH_TOKEN=wbk_xxxxxxxx   # 也接受 x-api-key 头
export ANTHROPIC_MODEL=glm-5.2
```

模型名与 OpenAI 侧**同一套**（含 `global:` 前缀的版本规则与密钥版本隔离），
`/v1/messages/count_tokens` 亦可用。

</details>

<details>
<summary><b>可用模型</b></summary>

以「设置 → 可用模型」实时拉取结果为准，常见如下（上下文均为 131072）：

`glm-5.2` · `glm-5.1` · `glm-5v-turbo` · `kimi-k2.7` · `minimax-m3` · `hy3` · `hy3-preview`

</details>

---

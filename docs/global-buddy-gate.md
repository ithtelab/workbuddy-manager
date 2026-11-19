# 国际版账号不再尝试领养 Buddy

issue #264：国际版（realm=global）的成长任务中心没有 `first_buddy` / `chat_5`，
但 10:00 活跃上报末尾的 `travelAdoptForce` 没有 realm 门控，global 账号每天都会
请求 `buddy/info` → `buddy/first`，固定收到 400「first_buddy task not completed」，
日志里表现为「门槛未达，明天再试」。

旅行巡检本身有 `IsGlobal()` 门控（global 没有猫猫旅行体系），这里漏的是活跃上报
之后的强制领养路径。补丁 `0023-global-buddy-gate.patch` 在 `travelAdoptForce` 开头
加同样的门控：global 账号直接返回，不再发起 buddy 查询或领养请求。

验证：`internal/scheduler` 新增用例，global 账号跑一轮 `RunActivityNow` 后
`/activity/growth/buddy/*` 调用次数为 0；CN 账号的领养路径不变。

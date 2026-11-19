# 请求日志显示思考强度（issue #267）

## 背景

日志页能看到模型、耗时、token 与缓存命中，但看不出这次请求用的是什么推理档位。
排查「同一模型为什么这次明显更慢/更贵」时，`reasoning_effort` 往往是第一现场证据。

## 取值口径

- 只记录**客户端在请求体里显式传的** `reasoning_effort`（`low` / `medium` / `high` / `max`…，
  不同模型档位不同）；
- 客户端没传 → `NULL`，界面显示「—」，**不按模型默认档推断**：
  同一模型不同版本的默认档可能不同，写死会把「没传」误读成「要了某个档位」；
- **不改写请求体**：档位归一化与降级由上游网关负责，面板只做旁路记录。

## 改动

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 数据库 | `server/db.py` | `request_logs` 新增 `effort TEXT`（`_MIGRATIONS` 幂等迁移）+ 写入列 |
| 采集 | `server/routers/gateway.py` | `_record()` 增加 `effort` 参数；chat 三条路径（非流式 / 流式成功 / 流式建连失败）传入 |
| 接口 | `server/routers/logs.py` | `/api/logs` 返回 `effort` |
| 前端 | `web/app/(main)/logs/page.tsx` | 「模型」后新增「思考强度」列，Badge 显示档位，未传显示 `—` |
| 类型/文案 | `web/lib/types.ts`、`web/lib/i18n/locales/*.json` | `RequestLog.effort` + 5 语言 `logs.colEffort` |

## 验证

- 面板实发三条请求（`low` / `high` / `max`）→ 均 200，`request_logs.effort` 分别落库；
- 不带该字段的请求 → `effort` 为 `NULL`；
- `/api/logs` 返回体带 `effort`；
- `npm run build`（静态导出）通过，日志页 chunk 含新列。

Closes #267
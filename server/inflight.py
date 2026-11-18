"""网关请求的「客户端在途」计数。

为什么需要和上游 `in_flight` 分开两个数：它们量的是两件事。

  · **上游槽位占用**（`/status` 的 `accounts[].in_flight`）：真正在跟上游通信的条数。
    排队等槽位的不在里面，上游已经答完、尾巴还在写回客户端的也不在里面。
  · **客户端在途**（本模块）：面板已经接手、但还没把响应写完的条数。包含排队，也
    包含流式传输的全过程——一个对话只要还在往外吐 chunk，就算 1。

两者大多只差 0–2 条；差得多时说明池子在排队（槽位打满），那正是需要看见的时刻。

实现是**进程内计数**：面板默认单 worker，数字就是准的；将来若上多 worker，这里只
反映当前进程，届时应换成共享计数（Redis/文件锁），不要假装它是全量。
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_COUNT = 0


def enter() -> None:
    global _COUNT
    with _LOCK:
        _COUNT += 1


def leave() -> None:
    global _COUNT
    with _LOCK:
        if _COUNT > 0:
            _COUNT -= 1


def current() -> int:
    return _COUNT

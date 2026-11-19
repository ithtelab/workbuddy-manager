"""按文件路径串行化「读—改—写」（进程内锁）。

为什么需要：账号文件的写者不止一个 —— 令牌续期、面板改线路/权重、导入、重新登录。
它们都是「读整份 JSON → 改几个键 → 原子替换」。原子替换只保证**文件不会写坏**，
不保证**更新不会丢**：两个写者交错时，后写者会用自己手里的旧快照覆盖前一个刚写的
字段。典型症状是两个，都很难查：

  · 「续期成功但账号仍然过期」——续期写回 token 的同时，另一条路径用旧快照把
    新 token 盖回去了；
  · 「刚设的线路/权重被清回」——导入/重登按登录响应重建整份文件，没读旧值。

这正是 issue #210（proxy 被刷新冲掉）与 #187（weight 同样）踩过的坑。锁的粒度是
**单个文件**（不同账号可以并行），跨进程不覆盖：上游自己的写回走它自己的
SaveAtomic，与面板的冲突窗口和以前一致（两边都是原子替换，坏不了，最多丢一次
并发更新）。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

# 锁表按路径字符串索引；进程生命周期内不清（账号数量级最多几百，可接受）。
_LOCKS: dict[str, threading.RLock] = {}
_GUARD = threading.Lock()


@contextmanager
def file_lock(path: Path):
    """进入某个路径的互斥区。同一路径可重入（RLock）。"""
    key = str(Path(path).absolute())
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield

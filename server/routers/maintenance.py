"""数据库维护接口（issue #492 第二步）。

两个端点都只允许管理员（`security.require_admin`）：

  · `GET  /api/maintenance/database`         当前库/WAL 大小、主要表行数、最近一次结果
  · `POST /api/maintenance/database/compact` 执行 `VACUUM INTO` 压缩并原子替换

压缩是敏感操作：会短暂暂停数据库访问、占用约等于当前库大小的临时磁盘空间，
成功后永久改写 `manager.db`。因此每次尝试都写一条 `audit_logs`，失败也留痕。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import security
from ..iputil import client_ip
from ..services import dbmaintenance

router = APIRouter(prefix='/api/maintenance', tags=['maintenance'])


@router.get('/database')
def get_database(user: dict = Depends(security.require_admin)) -> dict:
    """数据库概况。不放任何 query 参数：前端只发一次裸 GET。"""
    return dbmaintenance.database_status()


@router.post('/database/compact')
def compact_database(request: Request,
                     user: dict = Depends(security.require_admin)) -> dict:
    """压缩数据库。

    不声明 body / query 参数：前端 `post()` 会带一个空 JSON 对象，FastAPI 对
    未声明的多余 body 字段不会报错，也就不会踩上「参数按 query 解析、静默失效」
    那类问题（见 server/tests/test_request_binding.py）。
    """
    ip = client_ip(request)
    try:
        result = dbmaintenance.compact_database()
    except dbmaintenance.MaintenanceBusy as exc:
        # 没有发生任何改动，不必写审计（此时维护任务正持有数据库锁，
        # 审计写入反而会排队等锁）。
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except dbmaintenance.InsufficientDiskSpace as exc:
        security.audit(user, 'database.compact', 'manager.db',
                       f'压缩未开始：{exc}', ip=ip)
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except dbmaintenance.CompactError as exc:
        security.audit(user, 'database.compact', 'manager.db',
                       f'压缩失败（原库未改动）：{exc}', ip=ip)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 替换阶段异常也要留痕
        security.audit(user, 'database.compact', 'manager.db',
                       f'压缩异常（原库未改动）：{exc}', ip=ip)
        raise HTTPException(status_code=500, detail=f'压缩失败：{exc}') from exc

    security.audit(
        user, 'database.compact', 'manager.db',
        f'{result["before_bytes"]} → {result["after_bytes"]} 字节，'
        f'耗时 {result["duration_ms"]} ms',
        ip=ip,
    )
    return {'ok': True, **result}

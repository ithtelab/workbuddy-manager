"""PostgreSQL 异地备份的配置、连通性探测与迁移/恢复任务接口。

鉴权分两档，和「系统更新」保持一致：

  · **读**（配置、进度）—— 登录即可（`current_user`）。进度条是只读的，
    让所有管理用户都能看到「备份在跑」比藏起来更有用。
  · **写**（存配置、探测、迁移、恢复）—— 必须是**会话管理员**
    （`require_session_admin`）。用会话而不是 API Token：这两个动作一个会把
    数据推出去、一个会把数据覆盖回来，都该是「人坐在面板前点的」。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import security
from ..services import pgsync

router = APIRouter(prefix='/api/settings/pg-sync', tags=['pgsync'])


@router.get('')
def get_config(user: dict = Depends(security.current_user)) -> dict:
    return {'config': pgsync.public_config(), 'status': pgsync.read_status()}


@router.post('')
def save_config(body: dict, user: dict = Depends(security.require_session_admin)) -> dict:
    try:
        cfg = pgsync.save_config(body or {})
    except ValueError as exc:
        # 配置项本身不合法（如 SSL 模式）属于用户可改的输入错误，
        # 回 400 + 具体可选值，而不是 500 或等到任务失败才暴露。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    security.audit(user, 'pgsync.config', 'pg-sync',
                   f'自动备份={"开" if cfg.get("enabled") else "关"}'
                   f'；间隔={cfg.get("interval_minutes")} 分钟')
    return {'config': pgsync.public_config()}


@router.post('/test')
def test_connection(body: dict, user: dict = Depends(security.require_session_admin)) -> dict:
    """探测连通性。表单里刚填的值优先，没填的字段回落到已保存的配置 ——
    这样「先点测试、通过了再保存」和「保存完再测试」两条路都走得通。"""
    cfg = pgsync.merge_form(pgsync.get_config(), body or {})
    return pgsync.test_connection(cfg)


@router.post('/export')
def start_export(user: dict = Depends(security.require_session_admin)) -> dict:
    ok, message = pgsync.start_job('export')
    if ok:
        security.audit(user, 'pgsync.export', 'pg-sync', '开始导出到 PostgreSQL')
    return {'ok': ok, 'message': message, 'status': pgsync.read_status()}


@router.post('/import')
def start_import(user: dict = Depends(security.require_session_admin)) -> dict:
    """从 PostgreSQL 恢复。

    **会覆盖本地数据** —— 前端必须弹二次确认（见 PgSyncPanel）。这里不做
    额外的服务端确认位：接口本身就是「执行恢复」的语义，加一个 `confirm=true`
    只是把同一件事换个地方表达，挡不住真正会误点的人。
    """
    ok, message = pgsync.start_job('import')
    if ok:
        security.audit(user, 'pgsync.import', 'pg-sync', '开始从 PostgreSQL 恢复数据')
    return {'ok': ok, 'message': message, 'status': pgsync.read_status()}


@router.get('/status')
def get_status(user: dict = Depends(security.current_user)) -> dict:
    return pgsync.read_status()

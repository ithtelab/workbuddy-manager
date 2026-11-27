from fastapi import APIRouter, Depends, HTTPException
from .. import security
from ..services import accountrisk

router = APIRouter(prefix='/api/account-risk', tags=['accounts'])


@router.get('')
async def warnings(upstream_id: int | None = None, all_groups: bool = False,
                   realm: str | None = None, include_acknowledged: bool = False,
                   active_only: bool = False, user: dict = Depends(security.current_user)) -> dict:
    return await accountrisk.warnings(group_id=upstream_id, all_groups=all_groups, realm=realm,
                                      include_acknowledged=include_acknowledged, active_only=active_only)


@router.post('/{ident}/acknowledge')
def acknowledge(ident: str, user: dict = Depends(security.require_admin)) -> dict:
    if not accountrisk.acknowledge(ident):
        raise HTTPException(404, '预警不存在')
    return {'ok': True}


@router.post('/{ident}/resume')
async def resume(ident: str, user: dict = Depends(security.require_admin)) -> dict:
    ok, message = await accountrisk.resume(ident)
    if not ok:
        raise HTTPException(409, message)
    return {'ok': True}

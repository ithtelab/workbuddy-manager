"""系统维护：一键更新与进度查询。

安全说明：仅管理员可用；更新目标限定为固定枚举，路径全部取自服务端环境，
不接受客户端传入的命令或路径，避免变成任意命令执行入口。离线升级包同理：
客户端只上传**文件内容**，落盘位置与文件名都由服务端固定。
"""
from __future__ import annotations

import hashlib
import os
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from .. import config, security
from ..iputil import client_ip
from ..services import changelog, updater, wb2api

router = APIRouter(prefix='/api/system', tags=['system'])


class UpdateIn(BaseModel):
    target: str = Field(pattern='^(manager|upstream|both)$')
    # 包从哪来：GitHub 最新发布（默认），还是面板里已上传的离线升级包
    source: str = Field('release', pattern='^(release|uploaded)$')


@router.get('/update-status')
def update_status(user: dict = Depends(security.current_user)) -> dict:
    """更新进度与当前版本（含运行中的日志）。"""
    status = updater.read_status()
    is_admin = str((user or {}).get('role') or '') == 'admin'
    # 日志里可能有代理地址、绝对路径与命令输出：只给管理员。只读账号仍能看到
    # 版本与「是否在更新」，但拿不到实现细节（旧版对 viewer 是全量返回的）。
    status['log_tail'] = updater.tail_log(80) if is_admin else ''
    if not is_admin:
        status['logs'] = []
    return status


@router.post('/update')
def start_update(body: UpdateIn, user: dict = Depends(security.require_session_admin)) -> dict:
    """启动一键更新。

    更新会在后台执行并可能重启本服务，因此立即返回，由前端轮询
    `/api/system/update-status` 查看进度。
    """
    ok, message = updater.start_update(body.target, source=body.source)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {'ok': True, 'message': message}


# ── 离线升级包（issue #153）──────────────────────────────────
# 内网 / 拿不到 GitHub 的机器：在别处下载发布包，拿到这里上传，再走同一套更新流程。
# 这些端点只碰服务端固定路径（DATA_DIR/update-package.tar.gz），不吃客户端给的路径。

_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
_CHUNK = 1024 * 1024


@router.get('/update-package')
def get_update_package(user: dict = Depends(security.current_user)) -> dict:
    """已上传升级包的元信息（文件名 / 大小 / 摘要 / 包内版本 / 上传时间）。"""
    return updater.package_meta()


@router.post('/update-package')
async def upload_update_package(
    request: Request,
    file: UploadFile = File(...),
    expected_sha256: str = Form(''),
    user: dict = Depends(security.require_session_admin),
) -> dict:
    """上传离线升级包（发布页下载的那个 .tar.gz）。

    先写 `.part`、校验通过后再原子替换到最终位置：中途失败不会留下一个「看起来
    能用」的半截包。校验四件事——大小上限、能不能当 tar.gz 解开、包内有没有
    `.version`（不是本项目的包当场拒掉）、发布页 sha256。

    **摘要默认必填**：安装离线包会让包内的代码与依赖在本机执行（见
    updater.allow_unverified_package 的说明），不核对摘要等于把「面板会话」
    直接升级成「主机代码执行」。确实拿不到摘要的离线环境请显式设
    `WB_ALLOW_UNVERIFIED_PACKAGE=1`。
    """
    want = (expected_sha256 or '').strip().lower()
    if want and not _SHA256_RE.match(want):
        raise HTTPException(status_code=400, detail='sha256 要填发布页上那串 64 位十六进制')
    if not want and not updater.allow_unverified_package():
        raise HTTPException(
            status_code=400,
            detail='请填写发布页上的 sha256：安装升级包会在本机执行包内代码，'
                   '没有摘要就无法确认包没被替换。确实拿不到摘要的离线环境，'
                   '可在服务端设 WB_ALLOW_UNVERIFIED_PACKAGE=1 后重试。',
        )

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = updater.PACKAGE_FILE.with_suffix('.part')
    digest = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, 'wb') as fh:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > updater.PACKAGE_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f'升级包超过 {updater.PACKAGE_MAX_BYTES // 1024 // 1024} MB 上限',
                    )
                digest.update(chunk)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f'写入升级包失败：{exc}') from exc

    actual = digest.hexdigest()
    try:
        if size < 100_000:
            raise HTTPException(status_code=400, detail='文件太小，不像是发布包（下载可能中断了）')
        if want and actual != want:
            raise HTTPException(
                status_code=400,
                detail=f'sha256 与填的不一致：包是 {actual}，你填的是 {want}'
                       '（多半是下载不完整，重新下载一次再传）',
            )
        # 结构不对（不是 gzip / 不是 tar）时它抛 ValueError —— 那是「用户传错了文件」，
        # 该当场 400 说清楚，而不是漏成 500。
        try:
            version = updater._read_package_version(tmp_path)   # noqa: SLF001 —— 同模块服务层
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not version:
            raise HTTPException(
                status_code=400,
                detail='包里没有 .version —— 这不是本项目的发布包（发布页下载的是 '
                       'workbuddy-manager-vX.Y.Z.tar.gz）',
            )
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise

    os.replace(tmp_path, updater.PACKAGE_FILE)
    try:
        os.chmod(updater.PACKAGE_FILE, 0o600)
    except OSError:
        pass
    meta = updater.record_package(
        name=os.path.basename(file.filename or 'update-package.tar.gz'),
        size=size, sha256=actual, version=version, expected_sha256=want,
        uploaded_by=str(user.get('username') or ''),
    )
    security.audit(user, 'upload_update_package', version,
                   f'{size // 1024 // 1024} MB，sha256 {actual[:12]}…，'
                   f'{"已核对发布页摘要" if want else "未填发布页摘要"}；'
                   f'来源 {client_ip(request)}')
    return meta


@router.delete('/update-package')
def delete_update_package(user: dict = Depends(security.require_session_admin)) -> dict:
    """删掉已上传的升级包（更新完成后界面会提示清理）。"""
    updater.clear_package()
    security.audit(user, 'delete_update_package', '', '清掉离线升级包')
    return {'ok': True}


@router.delete('/update-status')
def clear_update_status(user: dict = Depends(security.require_session_admin)) -> dict:
    """清除上次更新的结果与日志（issue #105：失败记录此前在面板里消除不掉）。"""
    ok, message = updater.clear_status()
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {'ok': True, 'message': message}


@router.get('/check-update')
def check_update(force: bool = False, user: dict = Depends(security.current_user)) -> dict:
    """检测是否有新版本（管理端与上游）。

    结果缓存 6 小时以免触发 GitHub 限流；`force=true` 可强制刷新，
    但会打 GitHub API（未认证配额有限），因此只允许管理员强制刷新。
    """
    if force and user.get('role') != 'admin':
        force = False
    return updater.check_updates(force=force)


@router.get('/versions')
async def versions(user: dict = Depends(security.current_user)) -> dict:
    """展示当前版本，便于确认更新是否生效。"""
    try:
        status = await wb2api.get_status()
        upstream_ok = bool(status.get('connected'))
        upstream_total = status.get('total')
    except Exception:  # noqa: BLE001
        upstream_ok, upstream_total = False, None

    return {
        'manager': updater.current_version(),
        'upstream_connected': upstream_ok,
        'upstream_accounts': upstream_total,
        'upstream_dir': str(updater._upstream_dir()),
    }


@router.get('/changelog')
def get_changelog(user: dict = Depends(security.current_user)) -> dict:
    """更新日志（解析仓库根目录的 CHANGELOG.md）。

    离线环境同样可用：文件随发布包一起分发，无需访问 GitHub。
    """
    data = changelog.load_changelog()
    data['current'] = updater.current_version()
    return data

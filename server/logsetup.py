"""容器日志降噪：启动失败精简呈现 + 逐请求 access log 开关。

为什么要单独做这件事（issue #30 的日志现场）：
    lifespan 里任何一步启动失败，Starlette 都会把**完整异常链**打到 stderr。
    实测一次数据库权限失败会刷出 350 行，其中 96 行是 `fastapi/routing.py`
    的 `merged_lifespan`、`contextlib.py` 的 `__aenter__` 等框架内部帧 ——
    同一个名字重复几十次，而真正有用的 `RuntimeError: 无法打开数据库文件 ...`
    （自带两种修法）被埋在最后。用户盯着满屏 `^^^^^^` 根本找不到重点。

这里把输出压成三段：醒目的失败标题 → 异常链每层的「类型: 说明」（修法说明
原样保留）→ 只保留项目自己的栈帧。随后抛 SystemExit(1) 让进程干净退出，
框架就不会再补一遍几十层的框架栈。
"""
from __future__ import annotations

import logging
import traceback

from . import config

logger = logging.getLogger('workbuddy.startup')

_SEPARATOR = '=' * 72


def configure_access_log() -> None:
    """按配置决定是否静默 uvicorn 的逐请求 access log。

    必须在 uvicorn 完成 logging 配置**之后**调用（即 lifespan 里）——否则会被
    dictConfig 重置。用 `disabled` 而不是 setLevel：前者对该 logger 的所有调用
    立即生效，不依赖子 logger 与 handler 的级别。

    正常请求的去向并没有因此丢失：request_logs（计量）与 ip_access_logs（安全
    审计）两张表在界面上可按条件过滤，比 stdout 逐行文本有用得多。
    """
    if not config.ACCESS_LOG:
        logging.getLogger('uvicorn.access').disabled = True


def _is_project_frame(filename: str) -> bool:
    """只保留本项目栈帧；fastapi / starlette / contextlib 的都属于噪音。"""
    normalized = filename.replace('\\', '/')
    return '/server/' in normalized or normalized.endswith('/server')


def _exception_chain(exc: BaseException):
    """按 Python 标准顺序遍历异常链：外层在前，__cause__ / __context__ 在后。

    不能只 `while exc.__cause__`：隐式上下文（raise 里另一个异常正在处理）也
    要带上，否则根因可能整个消失。用 id 去重防止异常自引用导致死循环。
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def format_startup_failure(exc: BaseException, *, include_stack: bool = True) -> str:
    """把启动异常压成「标题 + 异常说明 + 项目栈帧」，不丢原始修法文案。"""
    lines = [_SEPARATOR, '启动失败：服务无法继续运行。', _SEPARATOR]

    chain = list(_exception_chain(exc))
    for item in chain:
        name = type(item).__name__
        message = str(item).strip()
        lines.append(f'{name}: {message}' if message else name)

    if include_stack:
        # 沿异常链顺序展开，天然形成「调用点 → 真正出错那一行」的阅读顺序。
        frames = [
            frame
            for item in chain
            for frame in traceback.extract_tb(item.__traceback__)
            if _is_project_frame(frame.filename)
        ]
        if frames:
            lines.append('--- 出错位置（已省略框架内部栈）---')
            for frame in frames:
                lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
                if frame.line:
                    lines.append(f'    {frame.line.strip()}')

    lines.append(_SEPARATOR)
    return '\n'.join(lines)


def report_startup_failure(exc: BaseException) -> None:
    """打一条精简的启动失败说明。"""
    logger.error('%s', format_startup_failure(
        exc, include_stack=config.STARTUP_FAILURE_STACK))

"""管理端启动入口：负责把「监听 ::」变成真正的**双栈**监听。

为什么需要它：uvicorn 的 `--host ::` 会直接让 asyncio 建 socket，而 Windows 的
`IPV6_V6ONLY` 默认是 **1**（v6only）——绑 `::` 只收 IPv6，IPv4 完全不可达：本机
虚拟网卡（如 26.105.249.1）、127.0.0.1 都进不来，启动器会一直停在「启动中…」等
健康检查。Linux 默认是双栈，所以这个问题只在 Windows 显现。

这里在绑 `::` 时显式关掉 v6only，再把 socket 交给 uvicorn；其它地址（具体 IPv4、
具体 IPv6、localhost）保持 uvicorn 原有行为不变。

用法（由 windows\\*.ps1 调用）：
    python -m server.serve --host :: --port 7864 --env-file .env
"""
from __future__ import annotations

import argparse
import socket
import sys

import uvicorn

# `::` 的各种写法都表示「IPv6 通配」，不能只看字面量
_IPV6_WILDCARD = frozenset(('::', '[::]', '::0'))


def is_ipv6_wildcard(host: str) -> bool:
    """是否 IPv6 通配地址（只有它需要双栈；具体地址绑它本身即可）。"""
    return (host or '').strip().lower() in _IPV6_WILDCARD


def make_dual_stack_socket(port: int) -> socket.socket:
    """绑定 `::` 并同时接受 IPv4 连接（关闭 IPV6_V6ONLY）。

    关闭 v6only 后，同一个 socket 既收 IPv6 也收 IPv4（地址按 v4-mapped 呈现），
    于是 127.0.0.1、局域网 IP、本机虚拟网卡都能访问 —— 与 Linux / Bun 的默认一致。
    """
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, 'IPV6_V6ONLY'):
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(('::', port))
    return sock


def main() -> int:
    parser = argparse.ArgumentParser(description='启动 WorkBuddy Manager 管理端')
    # 默认值只作兜底：实际 host/port 由启动脚本从 .env 解析后传入
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=7864)
    parser.add_argument('--env-file', default='.env')
    args = parser.parse_args()

    if is_ipv6_wildcard(args.host):
        # 自己建 socket 再交给 uvicorn：uvicorn 的 --host 路径无法表达双栈
        sock = make_dual_stack_socket(args.port)
        config = uvicorn.Config('server.main:app', env_file=args.env_file)
        uvicorn.Server(config).run(sockets=[sock])
        return 0

    uvicorn.run('server.main:app', host=args.host, port=args.port,
                env_file=args.env_file)
    return 0


if __name__ == '__main__':
    sys.exit(main())

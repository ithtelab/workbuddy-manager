"""自建 Redis 的「测试连接」端到端验收（issue #125）。

报障：上游设置里填 `redis://<内网IP>:9736`，上游用得好好的，但面板的「测试连接」
必报「不允许探测的内部地址」—— 旧实现只认 Upstash REST，把地址归一化成
`https://<host>`（丢端口）再按私网拒掉，且根本没有 RESP 探测能力。

这个脚本用一个真说 RESP 的桩服务，把**用户点的那条路**走一遍（真面板 HTTP 接口，
不是直接调函数）：登录 → POST 测试接口 → 看返回的那句话。

    python dev/verify_redis_probe_e2e.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.redis-probe'
MANAGER_PORT = 8061
ADMIN_PW = 'redis-probe-pass'
USERNAME, PASSWORD = 'wbuser', 's3cret'


class RespStub:
    """最小 RESP 服务：AUTH / PING，其余回 -ERR。"""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.commands: list[list[str]] = []
        self._stop = False

    def _client(self, conn: socket.socket) -> None:
        f = conn.makefile('rb')
        authed = False
        try:
            while True:
                line = f.readline()
                if not line:
                    return
                if not line.startswith(b'*'):
                    conn.sendall(b'-ERR expected array\r\n')
                    continue
                count = int(line[1:].strip())
                args: list[str] = []
                for _ in range(count):
                    f.readline()                       # $len
                    args.append(f.readline().decode().rstrip('\r\n'))
                self.commands.append(args)
                cmd = args[0].upper()
                if cmd == 'AUTH':
                    ok = args[1:] in ([PASSWORD], [USERNAME, PASSWORD])
                    authed = ok
                    conn.sendall(b'+OK\r\n' if ok else b'-WRONGPASS bad pair\r\n')
                elif cmd == 'PING':
                    conn.sendall(b'+PONG\r\n')
                else:
                    conn.sendall(b'-ERR unknown\r\n')
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def close(self) -> None:
        self._stop = True
        self.sock.close()


def main() -> int:
    try:
        sys.stdout.reconfigure(errors='replace')
    except Exception:  # noqa: BLE001
        pass

    import shutil
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    (DATA / 'data').mkdir(parents=True)
    (DATA / 'auths').mkdir(parents=True)

    stub = RespStub()
    threading.Thread(target=stub.serve, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': 'http://127.0.0.1:7899',
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_DATA_DIR': str(DATA / 'data'),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_UPSTREAM_CONFIG': str(DATA / 'upstream-config.json'),
        'WB_STATIC_DIR': str(REPO / 'web' / 'out'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'PYTHONUTF8': '1',
    }
    (DATA / 'upstream-config.json').write_text(json.dumps({'api_key': 'k'}), encoding='utf-8')
    proc = subprocess.Popen(
        [sys.executable, '-c',
         'import uvicorn, server.main;'
         f'uvicorn.run(server.main.app, host="127.0.0.1", port={MANAGER_PORT}, log_level="warning")'],
        cwd=str(REPO), env=env)
    base = f'http://127.0.0.1:{MANAGER_PORT}'
    findings: list[str] = []

    def step(ok: bool, label: str, detail: str = '') -> None:
        print(f'  {"✓ " if ok else "✗ "} {label}' + (f'\n       {detail}' if detail else ''))
        if not ok:
            findings.append(label + (f': {detail}' if detail else ''))

    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f'{base}/api/healthz', timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)

        # 登录拿会话 cookie（测试接口要管理员会话）
        cookie = ''
        req = urllib.request.Request(
            f'{base}/api/login', method='POST',
            data=json.dumps({'username': 'admin', 'password': ADMIN_PW}).encode(),
            headers={'content-type': 'application/json'})
        with urllib.request.urlopen(req) as resp:
            cookie = resp.headers.get('set-cookie', '').split(';')[0]
        step(bool(cookie), '登录拿到会话', cookie[:20])

        def probe(url: str) -> dict:
            r = urllib.request.Request(
                f'{base}/api/settings/upstash/test', method='POST',
                data=json.dumps({'url': url}).encode(),
                headers={'content-type': 'application/json', 'cookie': cookie})
            try:
                with urllib.request.urlopen(r, timeout=20) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return {'ok': False, 'message': f'HTTP {e.code}'}

        # ① 报障场景：回环上的自建 Redis（上游就是这么配的）
        out = probe(f'redis://127.0.0.1:{stub.port}')
        step(out.get('ok') is True and 'PONG' in out.get('message', ''),
             '自建 Redis（回环地址）测试连接通过', str(out))
        step([c[0].upper() for c in stub.commands if c] == ['PING'],
             '桩服务收到的正是 RESP PING', str(stub.commands))

        # ② 带密码：密码写在地址里，Token 留空
        stub.commands.clear()
        out = probe(f'redis://{USERNAME}:{PASSWORD}@127.0.0.1:{stub.port}')
        step(out.get('ok') is True, '带用户名密码的自建 Redis 通过', str(out))
        step(stub.commands and stub.commands[0][:1] == ['AUTH'],
             '先 AUTH 再 PING', str(stub.commands[:2]))

        # ③ 密码错：如实报认证失败，而不是含糊其辞
        out = probe(f'redis://:wrongpass@127.0.0.1:{stub.port}')
        step(out.get('ok') is False and '认证失败' in out.get('message', ''),
             '密码错时报「认证失败」', str(out))

        # ④ 私网不可达：不能再出现「不允许探测」（那是这次报障的原话）
        out = probe('redis://10.255.255.1:6379')
        step('不允许探测' not in out.get('message', ''),
             '私网地址不再被判为「不允许探测」', str(out))
        step(out.get('ok') is False and '无法连接' in out.get('message', ''),
             '连不上就如实说连不上', str(out))

        # ⑤ 元数据主机名仍然拒绝（这条防线不能在改动里丢掉）
        out = probe('redis://metadata.tencentyun.com:6379')
        step(out.get('ok') is False and '不允许探测' in out.get('message', ''),
             '元数据主机名仍然拒绝', str(out))

        # ⑥ Upstash REST 那支不受影响：私网 https 仍拒
        out = probe('https://10.0.0.5')
        step(out.get('ok') is False and '不允许探测' in out.get('message', ''),
             'Upstash REST 分支的私网拦截原样保留', str(out))

        print('\n=== 结果 ===')
        if findings:
            print('FAILED:\n  - ' + '\n  - '.join(findings))
            return 1
        print('ALL CHECKS PASSED')
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        stub.close()


if __name__ == '__main__':
    sys.exit(main())

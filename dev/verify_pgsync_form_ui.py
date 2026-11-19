"""「设置 → 备份」表单不被轮询打断的界面验收（issue #122）。

报障：填 PG 同步配置时，输入框每约 15 秒被服务端旧值覆盖一次 —— 空闲轮询走的
是**完整重载**（顺带 `setForm(服务端配置)`），正在敲的字被打回去，页面像在刷新。

这个脚本按报障路径走一遍，只看界面：

  1. 在主机输入框里打字，等两个轮询周期（35 秒）—— 字必须还在；
  2. 期间统计请求：配置接口只该被拉一次（首次挂载），进度接口照常轮询；
  3. 把进度接口桩成「任务运行中」，1.5 秒轮询期间字仍必须还在；
  4. 任务结束后（桩切成已完成），字也得还在（这条路径也要过脏检查）；
  5. 点保存：值真的落库（脚本读 sqlite 核对），且保存后表单与服务端一致。

第 1 条正是修前会红的那条：修前每 15 秒一次 `setForm(服务端配置)`，打字必然丢。

    python dev/verify_pgsync_form_ui.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.pg-form'
SHOTS = REPO / 'dev' / '.shots-pg-form'
MANAGER_PORT = 8051
ADMIN_PW = 'pg-form-pass'
TYPED_HOST = 'pg.infra.internal'


def _playwright_entry() -> str | None:
    root = Path(os.environ.get('LOCALAPPDATA', '')) / 'ms-playwright'
    if not root.is_dir():
        return None
    for d in sorted(root.iterdir(), reverse=True):
        if d.name.startswith('chromium-') and 'headless_shell' not in d.name:
            cand = d / 'chrome-win64' / 'chrome.exe'
            if cand.is_file():
                return str(cand)
    return None


def main() -> int:
    try:
        sys.stdout.reconfigure(errors='replace')
    except Exception:  # noqa: BLE001
        pass

    for d in (DATA, SHOTS):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    (DATA / 'auths').mkdir(parents=True)
    (DATA / 'data').mkdir(parents=True)
    SHOTS.mkdir(parents=True)

    env = {
        **os.environ,
        'WB2API_BASE': 'http://127.0.0.1:7899',   # 本页用不到上游，给个不可达地址即可
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_DATA_DIR': str(DATA / 'data'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_STATIC_DIR': str(REPO / 'web' / 'out'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'PYTHONUTF8': '1',
    }
    proc = subprocess.Popen(
        [sys.executable, '-c',
         'import uvicorn, server.main;'
         f'uvicorn.run(server.main.app, host="127.0.0.1", port={MANAGER_PORT}, log_level="warning")'],
        cwd=str(REPO), env=env)
    base = f'http://127.0.0.1:{MANAGER_PORT}'
    print(f'管理端: {base}（静态产物：web/out/）')
    try:
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(f'{base}/api/healthz', timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)

        node_env = {
            **os.environ,
            'WB_BASE': base,
            'WB_PASS': ADMIN_PW,
            'WB_TYPED': TYPED_HOST,
            'WB_SHOTS': str(SHOTS),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_pgsync_form.mjs'], cwd=str(REPO),
                           env=node_env, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        print(r.stdout or '')
        if r.stderr:
            print(r.stderr[-2000:], file=sys.stderr)
        if r.returncode != 0 or 'ALL CHECKS PASSED' not in (r.stdout or ''):
            print('✗ 浏览器侧没有报 ALL CHECKS PASSED —— 不能当作通过', file=sys.stderr)
            return 1

        # 落库核对：「保存」那一下真的写进了设置表（不是只改了内存里的表单）
        conn = sqlite3.connect(DATA / 'manager.db')
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='pg_sync_config'").fetchone()
        finally:
            conn.close()
        if not row:
            print('✗ 设置表里没有 pg_sync_config —— 保存没落库', file=sys.stderr)
            return 1
        saved = json.loads(row[0])
        if saved.get('host') != TYPED_HOST:
            print(f'✗ 落库的主机是 {saved.get("host")!r}，应为 {TYPED_HOST!r}', file=sys.stderr)
            return 1
        print(f'  ✓  保存后设置表里的主机 = {saved["host"]}（浏览器那一下真的落库了）')
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == '__main__':
    sys.exit(main())

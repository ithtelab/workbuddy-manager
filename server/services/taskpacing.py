"""Account pacing shared by manager scheduled actions and task subprocesses."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import random

from .. import config

logger = logging.getLogger(__name__)
_scheduled = ContextVar('scheduled_account_batch', default=False)
_batch_pacers = ContextVar('scheduled_account_pacers', default=None)
_bounds = ContextVar('scheduled_account_bounds', default=None)


@contextmanager
def scheduled_execution():
    bounds_token = _bounds.set(delay_bounds())
    token = _scheduled.set(True)
    pacers_token = _batch_pacers.set({})
    try:
        yield
    finally:
        _batch_pacers.reset(pacers_token)
        _scheduled.reset(token)
        _bounds.reset(bounds_token)


def is_scheduled() -> bool:
    return _scheduled.get()


def delay_bounds() -> tuple[int, int]:
    """Use the same account interval as the upstream, without changing the zero/zero default."""
    if _bounds.get() is not None:
        return _bounds.get()
    try:
        schedule = json.loads(config.UPSTREAM_CONFIG.read_text(encoding='utf-8')).get('schedule', {})
        lo = schedule.get('account_delay_min_seconds', 0)
        hi = schedule.get('account_delay_max_seconds', 0)
        if type(lo) is int and type(hi) is int and 0 <= lo <= hi <= 600:
            return lo, hi
    except (OSError, ValueError, AttributeError):
        pass
    return 0, 0


class AccountPacer:
    def __init__(self, task: str):
        self.task = task
        self.bounds = delay_bounds() if is_scheduled() else (0, 0)
        self.enabled = self.bounds[1] > 0
        self.started = False

    async def wait(self):
        if not self.enabled:
            return
        if self.started:
            delay = random.randint(*self.bounds)
            logger.info('scheduled %s: next account in %ss', self.task, delay)
            if delay:
                await asyncio.sleep(delay)
        self.started = True


async def run_accounts(task: str, accounts: list, action, needs_request=None) -> list:
    if not is_scheduled():
        return list(await asyncio.gather(*(action(a) for a in accounts)))
    # One task can span several account groups; keep the gap at group boundaries.
    pacers = _batch_pacers.get()
    if task not in pacers:
        pacers[task] = AccountPacer(task)
    pacer = pacers[task]
    if not pacer.enabled:
        return list(await asyncio.gather(*(action(a) for a in accounts)))
    results = []
    for account in accounts:
        if needs_request is None or needs_request(account):
            await pacer.wait()
        results.append(await action(account))
    return results


def script_args() -> list[str]:
    if not is_scheduled():
        return []
    lo, hi = delay_bounds()
    if hi == 0:
        return []
    return ['--account-delay-min', str(lo), '--account-delay-max', str(hi)]

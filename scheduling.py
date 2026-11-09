"""Shared pacing, global cooldown and coalesced in-memory metadata reads."""

import asyncio
import copy
import math
import time
from collections import OrderedDict
from datetime import timezone
from email.utils import parsedate_to_datetime

from .audit import RUN_ID, NullAudit


def retry_after(value: str, now=None) -> float:
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, date.timestamp() - (time.time() if now is None else now))
        except (ValueError, TypeError, OverflowError):
            return 0.0


class RequestGate:
    def __init__(self, settings, audit=None, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.audit = audit or NullAudit()
        self.clock = clock
        self.sleep = sleep
        self.intervals = {
            "api": settings.api_interval_ms / 1000,
            "image": settings.image_interval_ms / 1000,
        }
        self.next_start = {"api": 0.0, "image": 0.0}
        self.blocked_until = 0.0
        self.lock = asyncio.Lock()

    @property
    def remaining(self):
        return max(0.0, self.blocked_until - self.clock())

    async def pause(self, seconds: float):
        async with self.lock:
            self.blocked_until = max(self.blocked_until, self.clock() + seconds)
            self.audit.emit("global_cooldown", seconds=round(self.remaining, 3))

    async def wait(self, lane: str):
        while True:
            async with self.lock:
                now = self.clock()
                delay = max(self.next_start[lane], self.blocked_until) - now
                if delay <= 0:
                    self.next_start[lane] = now + self.intervals[lane]
                    return
            self.audit.emit(
                "request_wait", lane=lane, seconds=round(delay, 3), limited=self.remaining > 0
            )
            await self.sleep(delay)


class MetadataCache:
    def __init__(self, minutes: int, audit=None, *, clock=time.monotonic):
        self.ttl = minutes * 60
        self.clock = clock
        self.audit = audit or NullAudit()
        self.values = OrderedDict()
        self.inflight = {}
        self.origins = {}

    async def get(self, key, loader):
        cached = self.values.get(key)
        if cached and cached[0] > self.clock():
            self.values.move_to_end(key)
            self.audit.emit("metadata_cache_hit")
            return copy.deepcopy(cached[1])
        self.values.pop(key, None)
        task = self.inflight.get(key)
        if task is None:

            async def load():
                value = await loader()
                self.values[key] = (self.clock() + self.ttl, copy.deepcopy(value))
                # Large profile responses can be several MiB. Bound entry count.
                while len(self.values) > 32:
                    self.values.popitem(last=False)
                return value

            self.audit.emit("metadata_cache_miss")
            task = asyncio.create_task(load())
            self.inflight[key] = task
            self.origins[key] = RUN_ID.get()

            def done(completed):
                self.inflight.pop(key, None)
                self.origins.pop(key, None)
                if not completed.cancelled():
                    completed.exception()  # Consume errors even when all waiters cancel.

            task.add_done_callback(done)
        else:
            self.audit.emit("metadata_task_join", producer_run_id=self.origins[key])
        # One disconnected user must not cancel a producer used by other users.
        return copy.deepcopy(await asyncio.shield(task))

    async def close(self):
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

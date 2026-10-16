from __future__ import annotations
import asyncio
from concurrent.futures import ThreadPoolExecutor
import heapq
import itertools
import threading
import time


class InferenceAborted(RuntimeError):
    pass


class InferenceControl:
    def __init__(self, deadline_ms=None):
        self.received_at = time.monotonic()
        self.deadline = (
            None if deadline_ms is None else self.received_at + deadline_ms / 1000
        )
        self.cancelled = threading.Event()

    def stopped(self):
        return self.cancelled.is_set() or (
            self.deadline is not None and time.monotonic() >= self.deadline
        )

    def check(self):
        if self.stopped():
            raise InferenceAborted("Request cancelled or deadline expired")


class InferenceQueue:
    def __init__(self, executor: ThreadPoolExecutor, *, capacity=4):
        self.executor = executor
        self.capacity = capacity
        self.pending = 0
        self.completed = 0
        self.aborted = 0
        self.waiting = []
        self.sequence = itertools.count()
        self.worker = None

    async def _drain(self):
        while self.waiting:
            _, _, operation, control, result, received = heapq.heappop(self.waiting)
            started = time.monotonic()

            def run():
                if control is not None:
                    control.check()
                return operation()

            try:
                value = await asyncio.get_running_loop().run_in_executor(
                    self.executor, run
                )
                if isinstance(value, dict):
                    value = {
                        **value,
                        "queue_wait_ms": (started - received) * 1000,
                        "owner_execution_ms": (time.monotonic() - started) * 1000,
                    }
                result.set_result(value)
            except Exception as error:
                if isinstance(error, InferenceAborted):
                    self.aborted += 1
                result.set_exception(error)
                result.exception()
            finally:
                self.pending -= 1
                self.completed += 1

    async def invoke(
        self,
        operation,
        *,
        control=None,
        disconnected=None,
        cancel_on_disconnect=False,
        priority=0,
    ):
        if self.pending >= self.capacity:
            raise OverflowError("Bounded model queue is full")
        self.pending += 1
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(
            self.waiting,
            (
                priority,
                next(self.sequence),
                operation,
                control,
                future,
                time.monotonic(),
            ),
        )
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._drain())
        try:
            while not future.done():
                await asyncio.wait({future}, timeout=0.05)
                if (
                    cancel_on_disconnect
                    and disconnected is not None
                    and await disconnected()
                ):
                    control.cancelled.set()
            return future.result()
        except asyncio.CancelledError:
            if cancel_on_disconnect and control is not None:
                control.cancelled.set()
            raise

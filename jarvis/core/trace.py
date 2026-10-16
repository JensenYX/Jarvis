from __future__ import annotations
import json
import queue
import threading
from pathlib import Path
from typing import Callable
from jarvis.core.schemas import Event
from jarvis.core.secrets import redact


class ByteBoundedQueue(queue.Queue):
    def __init__(self, capacity, max_bytes):
        super().__init__(maxsize=capacity)
        self.max_bytes, self.resident_bytes = (max_bytes, 0)

    def _put(self, item):
        size = len(item.encode()) if isinstance(item, str) else 0
        if self.resident_bytes + size > self.max_bytes:
            raise queue.Full("Trace byte budget exceeded")
        super()._put((item, size))
        self.resident_bytes += size

    def _get(self):
        item, size = super()._get()
        self.resident_bytes -= size
        return item


class Trace:
    def __init__(
        self,
        directory: Path,
        session_id: str,
        *,
        pending_capacity=0,
        pending_bytes=16000000,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.session_id = session_id
        self.seq = 0
        self.file = (directory / "events.jsonl").open("a", buffering=65536)
        self.pending = (
            ByteBoundedQueue(pending_capacity, pending_bytes)
            if pending_capacity
            else queue.Queue()
        )
        self.bounded = bool(pending_capacity)
        self.writer_failure = None
        self.closing = threading.Event()
        self.writer = threading.Thread(
            target=self._write, name="trace-writer", daemon=True
        )
        self.writer.start()
        self.listeners: list[Callable[[Event], None]] = []

    def _write(self):
        try:
            while True:
                try:
                    line = self.pending.get(timeout=0.2)
                except queue.Empty:
                    self.file.flush()
                    if self.closing.is_set():
                        return
                    continue
                if line is None:
                    self.file.flush()
                    return
                if isinstance(line, threading.Event):
                    self.file.seek(0)
                    self.file.truncate()
                    self.file.flush()
                    line.set()
                    continue
                self.file.write(line)
        except Exception as error:
            self.writer_failure = type(error).__name__

    def emit(self, type: str, **payload) -> Event:
        if self.writer_failure:
            raise RuntimeError("Trace writer failed: " + self.writer_failure)
        if self.closing.is_set():
            raise RuntimeError("Trace is closed")
        self.seq += 1
        event = Event(
            session_id=self.session_id, seq=self.seq, type=type, payload=redact(payload)
        )
        self.pending.put_nowait(event.model_dump_json() + "\n")
        for listener in tuple(self.listeners):
            listener(event)
        return event

    def manifest(self, **values):
        (self.directory / "manifest.json").write_text(
            json.dumps(redact(values), ensure_ascii=False, indent=2) + "\n"
        )

    def close(self):
        self.closing.set()
        if not self.bounded and self.writer.is_alive():
            self.pending.put(None)
        self.writer.join(timeout=5 if self.bounded else None)
        if self.writer.is_alive():
            raise TimeoutError("Trace writer did not finish within its shutdown budget")
        self.file.close()

    def erase(self):
        if self.writer_failure or not self.writer.is_alive():
            raise RuntimeError("Trace writer is unavailable for erasure")
        barrier = threading.Event()
        self.pending.put(barrier, timeout=2 if self.bounded else None)
        if not barrier.wait(timeout=5 if self.bounded else None) or self.writer_failure:
            raise RuntimeError("Trace erasure was not confirmed")

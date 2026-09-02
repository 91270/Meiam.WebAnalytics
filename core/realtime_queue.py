#!/usr/bin/python3
# coding: utf-8
"""有界实时写入队列：隔离 Socket 收包与 SQLite 刷盘延迟。"""

from __future__ import annotations

import queue
import threading
import time
from collections import defaultdict
from typing import Dict, Optional, Tuple

from .parsers import AccessEvent


class RealtimeQueue:
    def __init__(self, repository, config: Dict[str, object]):
        self.repository = repository
        self.config = config
        self.max_size = max(1000, min(200000, int(config.get("queue_size", 20000))))
        self.flush_size = max(50, min(5000, int(config.get("flush_size", 500))))
        self.flush_interval = max(0.1, min(10.0, float(config.get("flush_interval_seconds", 1.0))))
        self.items: "queue.Queue[Optional[Tuple[int, AccessEvent]]]" = queue.Queue(self.max_size)
        self.lock = threading.Lock()
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.accepted = 0
        self.processed = 0
        self.dropped = 0
        self.write_errors = 0
        self.last_flush = 0

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._worker, name="webanalytics-writer", daemon=True)
        self.thread.start()

    def submit(self, site_id: int, event: AccessEvent) -> bool:
        try:
            self.items.put_nowait((site_id, event))
            with self.lock:
                self.accepted += 1
            return True
        except queue.Full:
            with self.lock:
                self.dropped += 1
            return False

    def _flush(self, pending) -> None:
        total = 0
        try:
            for site_id, events in pending.items():
                if not events:
                    continue
                self.repository.record_live_batch(
                    site_id,
                    events,
                    str(self.config["privacy_salt"]),
                    self.config.get("static_extensions", []),
                    int(self.config.get("hll_precision", 10)),
                )
                total += len(events)
        except Exception:
            with self.lock:
                self.write_errors += 1
        else:
            with self.lock:
                self.processed += total
                self.last_flush = int(time.time())

    def _worker(self) -> None:
        pending = defaultdict(list)
        pending_size = 0
        deadline = time.monotonic() + self.flush_interval
        while self.running or not self.items.empty():
            timeout = max(0.05, deadline - time.monotonic())
            try:
                item = self.items.get(timeout=timeout)
                if item is None:
                    self.items.task_done()
                    self.running = False
                    continue
                site_id, event = item
                pending[site_id].append(event)
                pending_size += 1
                self.items.task_done()
            except queue.Empty:
                pass
            now = time.monotonic()
            if pending_size and (pending_size >= self.flush_size or now >= deadline):
                self._flush(pending)
                pending.clear()
                pending_size = 0
                deadline = now + self.flush_interval
            elif now >= deadline:
                deadline = now + self.flush_interval
        if pending_size:
            self._flush(pending)

    def stop(self, timeout: float = 15.0) -> None:
        if not self.running:
            return
        self.running = False
        try:
            self.items.put_nowait(None)
        except queue.Full:
            pass
        if self.thread is not None:
            self.thread.join(timeout)

    def snapshot(self) -> Dict[str, int]:
        with self.lock:
            return {
                "size": self.items.qsize(),
                "capacity": self.max_size,
                "accepted": self.accepted,
                "processed": self.processed,
                "dropped": self.dropped,
                "write_errors": self.write_errors,
                "last_flush": self.last_flush,
            }

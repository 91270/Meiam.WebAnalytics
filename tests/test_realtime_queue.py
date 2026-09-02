from __future__ import annotations

import time
import unittest

from WebAnalytics.core.parsers import AccessEvent
from WebAnalytics.core.realtime_queue import RealtimeQueue


class FakeRepository:
    def __init__(self):
        self.batches = []

    def record_live_batch(
        self, site_id, events, privacy_salt, static_extensions, hll_precision=10
    ):
        self.batches.append((site_id, list(events)))


class RealtimeQueueTests(unittest.TestCase):
    def test_events_are_flushed_and_accounted(self):
        repository = FakeRepository()
        processor = RealtimeQueue(
            repository,
            {
                "queue_size": 1000,
                "flush_size": 50,
                "flush_interval_seconds": 0.1,
                "privacy_salt": "test",
                "static_extensions": [],
            },
        )
        processor.start()
        event = AccessEvent(1, "192.0.2.1", "GET", "/", "HTTP/1.1", 200, 12, "", "ua")
        for _ in range(3):
            self.assertTrue(processor.submit(7, event))
        time.sleep(0.2)
        processor.stop()
        snapshot = processor.snapshot()
        self.assertEqual(snapshot["accepted"], 3)
        self.assertEqual(snapshot["processed"], 3)
        self.assertEqual(snapshot["dropped"], 0)
        self.assertEqual(sum(len(batch[1]) for batch in repository.batches), 3)


if __name__ == "__main__":
    unittest.main()

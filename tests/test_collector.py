from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from WebAnalytics.core.collector import _read_batches, collect_site
from WebAnalytics.core.repository import Repository
from WebAnalytics.core.site_discovery import SiteDefinition


def line(ip, moment, uri, status=200, size=100, user_agent="Mozilla/5.0"):
    return (
        '{} - - [{}] "GET {} HTTP/1.1" {} {} "-" "{}"\n'.format(
            ip, moment, uri, status, size, user_agent
        )
    )


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log_path = self.root / "example.test.log"
        self.db_path = self.root / "stats.db"
        self.repository = Repository(self.db_path)
        self.repository.initialize()
        self.site = SiteDefinition(1, "example.test", "/srv/example", str(self.log_path))
        self.site_id = self.repository.register_site(self.site)
        self.config = {
            "collect_from_end": False,
            "batch_size": 100,
            "privacy_salt": "test-salt",
            "static_extensions": ["js", "css", "png"],
            "excluded_paths": ["/internal-health"],
        }

    def tearDown(self):
        self.temp.cleanup()

    def collect(self):
        return collect_site(
            self.repository,
            self.site_id,
            self.site,
            self.config,
            float("inf"),
        )

    def test_incremental_statistics_and_idempotency(self):
        self.log_path.write_text(
            line("192.0.2.1", "01/Sep/2026:10:20:30 +0800", "/")
            + line("192.0.2.1", "01/Sep/2026:10:20:31 +0800", "/app.js", size=200)
            + line("192.0.2.2", "01/Sep/2026:10:21:30 +0800", "/missing", 404, 50)
            + line("192.0.2.3", "01/Sep/2026:10:22:30 +0800", "/internal-health"),
            encoding="utf-8",
        )
        first = self.collect()
        self.assertEqual(first["lines"], 4)
        self.assertEqual(first["events"], 3)
        self.assertEqual(first["rejected"], 1)

        start = int(datetime.fromisoformat("2026-09-01T00:00:00+08:00").timestamp())
        end = start + 86400
        overview = self.repository.get_overview(self.site_id, start, end)
        self.assertEqual(overview["requests"], 3)
        self.assertEqual(overview["pv"], 1)
        self.assertEqual(overview["body_bytes"], 350)
        self.assertEqual(overview["errors"], 1)
        self.assertEqual(overview["uv"], 2)
        self.assertEqual(overview["ip"], 2)

        second = self.collect()
        self.assertEqual(second["lines"], 0)
        self.assertEqual(
            self.repository.get_overview(self.site_id, start, end)["requests"], 3
        )

        empty_site_id = self.repository.register_site(
            SiteDefinition(2, "empty.test", "/srv/empty", str(self.root / "empty.test.log"))
        )
        summaries = self.repository.get_site_summaries(
            [self.site_id, empty_site_id], start, end
        )
        self.assertEqual(summaries[self.site_id]["requests"], 3)
        self.assertEqual(summaries[self.site_id]["pv"], 1)
        self.assertEqual(summaries[self.site_id]["uv"], 2)
        self.assertEqual(summaries[self.site_id]["ip"], 2)
        self.assertGreater(summaries[self.site_id]["last_seen"], 0)
        self.assertEqual(summaries[empty_site_id]["requests"], 0)

    def test_rotation_finishes_old_file_then_reads_new_file(self):
        self.log_path.write_text(
            line("192.0.2.1", "01/Sep/2026:11:00:00 +0800", "/first"),
            encoding="utf-8",
        )
        self.collect()

        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(line("192.0.2.2", "01/Sep/2026:11:01:00 +0800", "/old-tail"))
        rotated = self.root / "example.test.log.1"
        self.log_path.rename(rotated)
        self.log_path.write_text(
            line("192.0.2.3", "01/Sep/2026:11:02:00 +0800", "/new-file"),
            encoding="utf-8",
        )

        result = self.collect()
        self.assertEqual(result["events"], 2)
        start = int(datetime.fromisoformat("2026-09-01T00:00:00+08:00").timestamp())
        overview = self.repository.get_overview(self.site_id, start, start + 86400)
        self.assertEqual(overview["requests"], 3)
        self.assertEqual(overview["ip"], 3)

    def test_first_install_can_start_at_end(self):
        self.log_path.write_text(
            line("192.0.2.1", "01/Sep/2026:12:00:00 +0800", "/historical"),
            encoding="utf-8",
        )
        self.config["collect_from_end"] = True
        first = self.collect()
        self.assertEqual(first["events"], 0)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(line("192.0.2.2", "01/Sep/2026:12:01:00 +0800", "/new"))
        second = self.collect()
        self.assertEqual(second["events"], 1)

    def test_initial_tail_discards_only_the_partial_first_line(self):
        self.log_path.write_bytes(b"first-line\nsecond-line\nthird-line\n")
        batches = list(_read_batches(self.log_path, 3, 100, True))
        self.assertEqual(batches[0][0], ["second-line\n", "third-line\n"])


if __name__ == "__main__":
    unittest.main()

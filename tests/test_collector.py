from __future__ import annotations

import tempfile
import unittest
import gzip
import io
import tarfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from WebAnalytics.core.collector import (
    _history_files,
    _read_batches,
    _run_history_backfill_unlocked,
    _run_once_unlocked,
    _stat_cursor,
    collect_site,
)
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

        empty_log = self.root / "empty.test.log"
        empty_log.write_text(
            line("198.51.100.8", "01/Sep/2026:10:23:30 +0800", "/new-site"),
            encoding="utf-8",
        )
        empty_site = SiteDefinition(2, "empty.test", "/srv/empty", str(empty_log))
        self.repository.save_cursor(
            empty_site_id,
            str(empty_log),
            _stat_cursor(empty_log, empty_log.stat().st_size),
        )
        backfill_config = dict(self.config)
        backfill_config.update(
            {
                "collect_from_end": False,
                "only_sites_without_statistics": True,
                "force_tail_backfill": True,
                "run_budget_seconds": 5,
            }
        )
        with patch(
            "WebAnalytics.core.collector.Repository", return_value=self.repository
        ), patch(
            "WebAnalytics.core.collector.discover_sites",
            return_value=[self.site, empty_site],
        ):
            backfill = _run_once_unlocked(backfill_config)
        self.assertEqual(backfill["discovered_sites"], 2)
        self.assertEqual(backfill["sites"], 1)
        self.assertEqual(
            self.repository.get_overview(empty_site_id, start, end)["requests"], 1
        )
        self.assertEqual(
            self.repository.get_overview(self.site_id, start, end)["requests"], 3
        )

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

    def test_full_history_reads_rotated_gzip_and_current_logs_once(self):
        rotated_plain = self.root / "example.test.log.2"
        rotated_gzip = self.root / "example.test.log.1.gz"
        rotated_tar = self.root / "example.test.log.archive.tar.gz"
        rotated_plain.write_text(
            line("192.0.2.1", "31/Aug/2026:23:59:00 +0800", "/old"),
            encoding="utf-8",
        )
        with gzip.open(str(rotated_gzip), "wt", encoding="utf-8") as stream:
            stream.write(line("192.0.2.2", "01/Sep/2026:00:01:00 +0800", "/gzip"))
        archived_line = line(
            "192.0.2.4", "01/Sep/2026:00:01:30 +0800", "/tar-gzip"
        ).encode("utf-8")
        with tarfile.open(str(rotated_tar), "w:gz") as archive:
            member = tarfile.TarInfo("example.test.log")
            member.size = len(archived_line)
            archive.addfile(member, io.BytesIO(archived_line))
        self.log_path.write_text(
            line("192.0.2.3", "01/Sep/2026:00:02:00 +0800", "/current"),
            encoding="utf-8",
        )
        # 文件名不是可靠时间顺序，采集器使用 mtime，并始终把当前文件放最后。
        paths = _history_files(self.log_path)
        self.assertEqual(paths[-1], self.log_path)

        config = dict(self.config)
        config["full_history"] = True
        first = collect_site(
            self.repository, self.site_id, self.site, config, float("inf")
        )
        self.assertTrue(first["complete"])
        self.assertEqual(first["events"], 4)
        second = collect_site(
            self.repository, self.site_id, self.site, config, float("inf")
        )
        self.assertEqual(second["events"], 0)

        start = int(datetime.fromisoformat("2026-08-31T00:00:00+08:00").timestamp())
        overview = self.repository.get_overview(self.site_id, start, start + 172800)
        self.assertEqual(overview["requests"], 4)

    def test_history_backfill_keeps_targets_not_reached_in_previous_round(self):
        second_log = self.root / "second.test.log"
        second_log.write_text(
            line("198.51.100.2", "01/Sep/2026:01:00:00 +0800", "/second"),
            encoding="utf-8",
        )
        second_site = SiteDefinition(2, "second.test", "/srv/second", str(second_log))
        calls = []

        def fake_round(config):
            targets = list(config["target_site_ids"])
            calls.append(targets)
            if len(calls) == 1:
                return {
                    "sites": 1,
                    "lines": 1,
                    "events": 1,
                    "rejected": 0,
                    "completed_site_ids": [targets[0]],
                    "incomplete_site_ids": [],
                    "errors": [],
                }
            return {
                "sites": 1,
                "lines": 1,
                "events": 1,
                "rejected": 0,
                "completed_site_ids": targets,
                "incomplete_site_ids": [],
                "errors": [],
            }

        with patch(
            "WebAnalytics.core.collector.Repository", return_value=self.repository
        ), patch(
            "WebAnalytics.core.collector.discover_sites",
            return_value=[self.site, second_site],
        ), patch(
            "WebAnalytics.core.collector._run_once_unlocked", side_effect=fake_round
        ):
            result = _run_history_backfill_unlocked(self.config)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(calls[0]), 2)
        self.assertEqual(len(calls[1]), 1)
        self.assertEqual(result["incomplete_site_ids"], [])

    def test_history_backfill_completes_and_persists_site_state(self):
        self.log_path.write_text(
            line("203.0.113.9", "02/Sep/2026:17:30:54 +0800", "/history"),
            encoding="utf-8",
        )
        with patch(
            "WebAnalytics.core.collector.Repository", return_value=self.repository
        ), patch(
            "WebAnalytics.core.collector.discover_sites", return_value=[self.site]
        ):
            result = _run_history_backfill_unlocked(self.config)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["events"], 1)
        self.assertFalse(self.repository.needs_history_import(self.site_id))


if __name__ == "__main__":
    unittest.main()

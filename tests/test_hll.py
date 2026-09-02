from __future__ import annotations

import unittest
import tempfile
import sqlite3
from pathlib import Path

from WebAnalytics.core.hll import HyperLogLog
from WebAnalytics.core.repository import Repository
from WebAnalytics.core.site_discovery import SiteDefinition


class HyperLogLogTests(unittest.TestCase):
    def test_small_cardinality_and_serialization(self):
        sketch = HyperLogLog(10)
        for value in ("a", "b", "c", "c"):
            sketch.add(value)
        restored = HyperLogLog.loads(sketch.dumps(), 10)
        self.assertEqual(restored.estimate(), 3)

    def test_merge_and_large_cardinality(self):
        left = HyperLogLog(10)
        right = HyperLogLog(10)
        for number in range(10000):
            (left if number < 6000 else right).add(str(number))
        left.merge(right)
        self.assertLess(abs(left.estimate() - 10000) / 10000.0, 0.08)

    def test_schema_one_exact_rows_are_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "stats.db")
            repository.initialize()
            site_id = repository.register_site(
                SiteDefinition(3, "example.test", "/srv/example", "/logs/example.test.log")
            )
            with repository.session() as connection:
                connection.execute("UPDATE schema_version SET version=1")
                connection.execute(
                    "INSERT INTO visitor_day(site_id, day, visitor_hash, first_minute) VALUES (?,?,?,?)",
                    (site_id, "2026-09-01", "visitor-a", 1788217200),
                )
                connection.execute(
                    "INSERT INTO ip_day(site_id, day, ip_hash, first_minute) VALUES (?,?,?,?)",
                    (site_id, "2026-09-01", "ip-a", 1788217200),
                )
            repository.initialize()
            overview = repository.get_overview(site_id, 1788192000, 1788278400)
            self.assertEqual(overview["uv"], 1)
            self.assertEqual(overview["ip"], 1)

    def test_early_database_columns_are_repaired_without_data_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "stats.db"
            connection = sqlite3.connect(str(database))
            try:
                connection.execute(
                    """CREATE TABLE sites (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        panel_site_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        document_root TEXT NOT NULL DEFAULT '',
                        log_path TEXT NOT NULL UNIQUE,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    )"""
                )
                connection.execute(
                    "INSERT INTO sites(panel_site_id,name,document_root,log_path,created_at,updated_at) "
                    "VALUES (1,'legacy.test','/srv/legacy','/logs/legacy.log',1,1)"
                )
                connection.commit()
            finally:
                connection.close()
            repository = Repository(database)
            repository.initialize()
            sites = repository.list_sites()
            self.assertEqual(sites[0]["name"], "legacy.test")
            self.assertEqual(sites[0]["web_server"], "nginx")
            self.assertEqual(sites[0]["enabled"], 1)

    def test_duplicate_panel_site_rows_are_merged_when_log_path_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "stats.db")
            repository.initialize()
            old_id = repository.register_site(
                SiteDefinition(7, "example.test", "/srv/example", "/logs/inferred.log")
            )
            with repository.session() as connection:
                connection.execute(
                    """INSERT INTO metric_minute
                       (site_id,minute_ts,requests,pv,body_bytes,errors,bot_requests)
                       VALUES (?,?,?,?,?,?,?)""",
                    (old_id, 1788314400, 10, 6, 1000, 1, 0),
                )
                cursor = connection.execute(
                    """INSERT INTO sites(panel_site_id,name,document_root,log_path,
                                         web_server,enabled,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (7, "example.test", "/srv/example", "/logs/actual.log", "nginx", 1, 2, 2),
                )
                duplicate_id = int(cursor.lastrowid)
                connection.execute(
                    """INSERT INTO metric_minute
                       (site_id,minute_ts,requests,pv,body_bytes,errors,bot_requests)
                       VALUES (?,?,?,?,?,?,?),(?,?,?,?,?,?,?)""",
                    (
                        duplicate_id, 1788314400, 3, 2, 300, 0, 0,
                        duplicate_id, 1788314460, 2, 1, 200, 0, 0,
                    ),
                )

            resolved_id = repository.register_site(
                SiteDefinition(7, "example.test", "/srv/example", "/logs/actual.log")
            )
            self.assertEqual(resolved_id, old_id)
            sites = repository.list_sites()
            self.assertEqual(len(sites), 1)
            self.assertEqual(sites[0]["log_path"], "/logs/actual.log")
            overview = repository.get_overview(
                old_id, 1788314000, 1788315000
            )
            self.assertEqual(overview["requests"], 12)
            self.assertEqual(overview["pv"], 7)
            with repository.session() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) AS total FROM sites WHERE panel_site_id=7"
                ).fetchone()
            self.assertEqual(int(count["total"]), 1)


if __name__ == "__main__":
    unittest.main()

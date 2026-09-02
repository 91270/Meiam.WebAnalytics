from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from WebAnalytics.scripts import restore_data


def database(path: Path, rows: int) -> None:
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE metric_minute (site_id INTEGER, minute_ts INTEGER)"
        )
        connection.executemany(
            "INSERT INTO metric_minute(site_id, minute_ts) VALUES (1, ?)",
            [(index,) for index in range(rows)],
        )


class RestoreDataTests(unittest.TestCase):
    def test_latest_empty_backup_is_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            backup_root = root / "backup"
            older = backup_root / "20260902-111701"
            newer = backup_root / "20260902-112147"
            older.mkdir(parents=True)
            newer.mkdir(parents=True)
            database(older / "stats.db", 3)
            database(newer / "stats.db", 0)
            # 排序以 mtime 为准，明确让空库更新。
            (newer / "stats.db").touch()

            with patch.object(restore_data, "DATA_DIR", data_dir), patch.object(
                restore_data, "DB_PATH", data_dir / "stats.db"
            ), patch.object(restore_data, "BACKUP_ROOT", backup_root):
                restore_data.restore()
                self.assertEqual(restore_data.metric_rows(data_dir / "stats.db"), 3)


if __name__ == "__main__":
    unittest.main()

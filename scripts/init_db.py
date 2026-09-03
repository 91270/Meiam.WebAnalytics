#!/usr/bin/python3
# coding: utf-8

from __future__ import annotations

import sys
import sqlite3
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.repository import Repository, SCHEMA_VERSION
from core.settings import DB_PATH, load_config


def backup_before_schema_upgrade(db_path: Path):
    """仅在结构版本升级前创建 SQLite 一致性备份，普通重装不会重复备份。"""
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return None
    source = sqlite3.connect(str(db_path), timeout=30)
    try:
        row = source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if row is None:
            old_version = 0
        else:
            version_row = source.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            old_version = int(version_row[0]) if version_row else 0
        if old_version >= SCHEMA_VERSION:
            return None
        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / "stats-v{}-{}.db".format(old_version, time.strftime("%Y%m%d-%H%M%S"))
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
        backups = sorted(backup_dir.glob("stats-v*.db"), key=lambda item: item.stat().st_mtime, reverse=True)
        for expired in backups[3:]:
            expired.unlink()
        return target
    finally:
        source.close()


if __name__ == "__main__":
    load_config()
    backup = backup_before_schema_upgrade(DB_PATH)
    Repository().initialize()
    if backup is not None:
        print("Database backup created: {}".format(backup))
    print("WebAnalytics database initialized")

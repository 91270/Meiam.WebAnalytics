#!/usr/bin/python3
# coding: utf-8
"""当前库为空时，恢复最近一份真正包含统计数据的卸载备份。"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path


DATA_DIR = Path("/www/server/webanalytics/data")
DB_PATH = DATA_DIR / "stats.db"
BACKUP_ROOT = Path("/www/backup/plugin/WebAnalytics")


def metric_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        connection = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return 0
            return int(connection.execute("SELECT COUNT(*) FROM metric_minute").fetchone()[0])
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError):
        return 0


def newest_backup_with_data():
    if not BACKUP_ROOT.is_dir():
        return None
    candidates = sorted(
        BACKUP_ROOT.glob("*/stats.db"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    return next((path for path in candidates if metric_rows(path) > 0), None)


def restore() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current_rows = metric_rows(DB_PATH)
    if current_rows > 0:
        print("保留当前统计数据库（{} 条分钟记录）".format(current_rows))
        return
    source = newest_backup_with_data()
    if source is None:
        print("未找到含历史统计的备份，将从现存网站日志完整重建")
        return

    for suffix in ("", "-wal", "-shm"):
        source_file = Path(str(source) + suffix)
        target_file = Path(str(DB_PATH) + suffix)
        if source_file.is_file():
            temporary = Path(str(target_file) + ".restore")
            shutil.copy2(str(source_file), str(temporary))
            temporary.replace(target_file)
        elif target_file.exists():
            target_file.unlink()
    source_config = source.parent / "config.json"
    target_config = DATA_DIR / "config.json"
    if source_config.is_file():
        shutil.copy2(str(source_config), str(target_config))
    print(
        "已恢复历史统计：{}（{} 条分钟记录）".format(
            source, metric_rows(DB_PATH)
        )
    )


if __name__ == "__main__":
    restore()

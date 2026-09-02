#!/usr/bin/python3
# coding: utf-8
"""SQLite 数据访问层。"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .hll import HyperLogLog
from .metrics import ip_hash, is_page_view, spider_name, visitor_hash
from .parsers import AccessEvent
from .settings import DB_PATH
from .site_discovery import SiteDefinition


SCHEMA_VERSION = 3


@dataclass(frozen=True)
class FileCursor:
    inode: int
    device: int
    offset: int
    size: int
    mtime_ns: int


class Repository:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    @contextmanager
    def session(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info({})".format(table))
        }
        if column not in columns:
            connection.execute(
                "ALTER TABLE {} ADD COLUMN {} {}".format(table, column, definition)
            )

    @classmethod
    def _upgrade_legacy_columns(cls, connection: sqlite3.Connection) -> None:
        """修补早期测试版数据库中尚未建立的非破坏性字段。"""
        cls._ensure_column(connection, "sites", "web_server", "TEXT NOT NULL DEFAULT 'nginx'")
        cls._ensure_column(connection, "sites", "enabled", "INTEGER NOT NULL DEFAULT 1")
        cls._ensure_column(connection, "file_cursors", "device", "TEXT NOT NULL DEFAULT '0'")
        cls._ensure_column(connection, "file_cursors", "size", "INTEGER NOT NULL DEFAULT 0")
        cls._ensure_column(connection, "file_cursors", "mtime_ns", "INTEGER NOT NULL DEFAULT 0")
        cls._ensure_column(
            connection, "metric_minute", "body_bytes", "INTEGER NOT NULL DEFAULT 0"
        )
        cls._ensure_column(connection, "metric_minute", "errors", "INTEGER NOT NULL DEFAULT 0")
        cls._ensure_column(
            connection, "metric_minute", "bot_requests", "INTEGER NOT NULL DEFAULT 0"
        )

    def initialize(self) -> None:
        with self.session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    panel_site_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    document_root TEXT NOT NULL DEFAULT '',
                    log_path TEXT NOT NULL UNIQUE,
                    web_server TEXT NOT NULL DEFAULT 'nginx',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS file_cursors (
                    site_id INTEGER PRIMARY KEY,
                    log_path TEXT NOT NULL,
                    inode TEXT NOT NULL,
                    device TEXT NOT NULL,
                    offset INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS metric_minute (
                    site_id INTEGER NOT NULL,
                    minute_ts INTEGER NOT NULL,
                    requests INTEGER NOT NULL DEFAULT 0,
                    pv INTEGER NOT NULL DEFAULT 0,
                    body_bytes INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    bot_requests INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(site_id, minute_ts),
                    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS visitor_day (
                    site_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    visitor_hash TEXT NOT NULL,
                    first_minute INTEGER NOT NULL,
                    PRIMARY KEY(site_id, day, visitor_hash),
                    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS ip_day (
                    site_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    ip_hash TEXT NOT NULL,
                    first_minute INTEGER NOT NULL,
                    PRIMARY KEY(site_id, day, ip_hash),
                    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS unique_hll_hour (
                    site_id INTEGER NOT NULL,
                    hour_ts INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('uv','ip')),
                    precision INTEGER NOT NULL,
                    registers BLOB NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(site_id, hour_ts, kind),
                    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS runtime_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS history_import (
                    site_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending',
                    lines INTEGER NOT NULL DEFAULT 0,
                    events INTEGER NOT NULL DEFAULT 0,
                    rejected INTEGER NOT NULL DEFAULT 0,
                    started_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    completed_at INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
                );

                """
            )
            self._upgrade_legacy_columns(connection)
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_metric_minute_time
                    ON metric_minute(minute_ts);
                CREATE INDEX IF NOT EXISTS idx_visitor_first_minute
                    ON visitor_day(site_id, first_minute);
                CREATE INDEX IF NOT EXISTS idx_ip_first_minute
                    ON ip_day(site_id, first_minute);
                CREATE INDEX IF NOT EXISTS idx_hll_hour_time
                    ON unique_hll_hour(site_id, hour_ts);
                """
            )
            row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                self._migrate_exact_uniques(connection)
                connection.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif int(row["version"]) <= 1:
                self._migrate_exact_uniques(connection)
                connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
                now = int(time.time())
                connection.execute(
                    """INSERT OR IGNORE INTO history_import
                       (site_id,status,started_at,updated_at,completed_at)
                       SELECT DISTINCT site_id,'complete',?,?,? FROM metric_minute""",
                    (now, now, now),
                )
            elif int(row["version"]) == 2:
                now = int(time.time())
                connection.execute(
                    """INSERT OR IGNORE INTO history_import
                       (site_id,status,started_at,updated_at,completed_at)
                       SELECT DISTINCT site_id,'complete',?,?,? FROM metric_minute""",
                    (now, now, now),
                )
                connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
            elif int(row["version"]) != SCHEMA_VERSION:
                raise RuntimeError("不支持的数据库版本: {}".format(row["version"]))

    @staticmethod
    def _migrate_exact_uniques(connection: sqlite3.Connection, precision: int = 10) -> None:
        """把 0.2.x 精确去重行迁移到 HLL；旧表保留用于回滚。"""
        for table, hash_column, kind in (
            ("visitor_day", "visitor_hash", "uv"),
            ("ip_day", "ip_hash", "ip"),
        ):
            rows = connection.execute(
                "SELECT site_id, (first_minute / 3600) * 3600 AS hour_ts, {column} AS value "
                "FROM {table} ORDER BY site_id, hour_ts".format(
                    column=hash_column, table=table
                )
            )
            current_key = None
            sketch = None
            for row in rows:
                key = (int(row["site_id"]), int(row["hour_ts"]))
                if current_key is not None and key != current_key:
                    Repository._store_hll(connection, current_key[0], current_key[1], kind, sketch)
                    sketch = None
                if sketch is None:
                    sketch = HyperLogLog(precision)
                    current_key = key
                sketch.add(str(row["value"]))
            if current_key is not None and sketch is not None:
                Repository._store_hll(connection, current_key[0], current_key[1], kind, sketch)

    @staticmethod
    def _store_hll(
        connection: sqlite3.Connection,
        site_id: int,
        hour_ts: int,
        kind: str,
        sketch: HyperLogLog,
    ) -> None:
        existing = connection.execute(
            "SELECT precision, registers FROM unique_hll_hour "
            "WHERE site_id=? AND hour_ts=? AND kind=?",
            (site_id, hour_ts, kind),
        ).fetchone()
        if existing is not None:
            current = HyperLogLog.loads(existing["registers"], int(existing["precision"]))
            current.merge(sketch)
            sketch = current
        connection.execute(
            """
            INSERT INTO unique_hll_hour(site_id, hour_ts, kind, precision, registers, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(site_id, hour_ts, kind) DO UPDATE SET
                precision=excluded.precision,
                registers=excluded.registers,
                updated_at=excluded.updated_at
            """,
            (site_id, hour_ts, kind, sketch.precision, sketch.dumps(), int(time.time())),
        )

    @classmethod
    def _merge_site_rows(
        cls, connection: sqlite3.Connection, canonical_id: int, duplicate_id: int
    ) -> None:
        """合并同一宝塔站点因日志路径变化产生的重复内部记录。"""
        if canonical_id == duplicate_id:
            return
        connection.execute(
            """
            INSERT INTO metric_minute(site_id, minute_ts, requests, pv, body_bytes,
                                      errors, bot_requests)
            SELECT ?, minute_ts, requests, pv, body_bytes, errors, bot_requests
            FROM metric_minute WHERE site_id=?
            ON CONFLICT(site_id, minute_ts) DO UPDATE SET
                requests=MAX(metric_minute.requests, excluded.requests),
                pv=MAX(metric_minute.pv, excluded.pv),
                body_bytes=MAX(metric_minute.body_bytes, excluded.body_bytes),
                errors=MAX(metric_minute.errors, excluded.errors),
                bot_requests=MAX(metric_minute.bot_requests, excluded.bot_requests)
            """,
            (canonical_id, duplicate_id),
        )
        for table, hash_column in (
            ("visitor_day", "visitor_hash"),
            ("ip_day", "ip_hash"),
        ):
            connection.execute(
                "INSERT OR IGNORE INTO {table}(site_id, day, {column}, first_minute) "
                "SELECT ?, day, {column}, first_minute FROM {table} WHERE site_id=?".format(
                    table=table, column=hash_column
                ),
                (canonical_id, duplicate_id),
            )
        hll_rows = connection.execute(
            "SELECT hour_ts, kind, precision, registers FROM unique_hll_hour "
            "WHERE site_id=?",
            (duplicate_id,),
        ).fetchall()
        for row in hll_rows:
            cls._store_hll(
                connection,
                canonical_id,
                int(row["hour_ts"]),
                str(row["kind"]),
                HyperLogLog.loads(row["registers"], int(row["precision"])),
            )

        duplicate_cursor = connection.execute(
            "SELECT * FROM file_cursors WHERE site_id=?", (duplicate_id,)
        ).fetchone()
        canonical_cursor = connection.execute(
            "SELECT * FROM file_cursors WHERE site_id=?", (canonical_id,)
        ).fetchone()
        if duplicate_cursor is not None and (
            canonical_cursor is None
            or int(duplicate_cursor["updated_at"] or 0)
            >= int(canonical_cursor["updated_at"] or 0)
        ):
            connection.execute("DELETE FROM file_cursors WHERE site_id=?", (canonical_id,))
            connection.execute(
                """INSERT INTO file_cursors(site_id, log_path, inode, device, offset,
                                              size, mtime_ns, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    canonical_id,
                    duplicate_cursor["log_path"],
                    duplicate_cursor["inode"],
                    duplicate_cursor["device"],
                    duplicate_cursor["offset"],
                    duplicate_cursor["size"],
                    duplicate_cursor["mtime_ns"],
                    duplicate_cursor["updated_at"],
                ),
            )
        connection.execute("DELETE FROM sites WHERE id=?", (duplicate_id,))

    def register_site(self, site: SiteDefinition) -> int:
        now = int(time.time())
        with self.session() as connection:
            if site.panel_site_id > 0:
                panel_rows = connection.execute(
                    "SELECT id, log_path FROM sites WHERE panel_site_id=? ORDER BY id",
                    (site.panel_site_id,),
                ).fetchall()
                if panel_rows:
                    canonical_id = int(panel_rows[0]["id"])
                    previous_log_path = str(panel_rows[0]["log_path"] or "")
                    duplicate_ids = [int(row["id"]) for row in panel_rows[1:]]
                    path_row = connection.execute(
                        "SELECT id, panel_site_id FROM sites WHERE log_path=?",
                        (site.log_path,),
                    ).fetchone()
                    if (
                        path_row is not None
                        and int(path_row["id"]) != canonical_id
                        and int(path_row["id"]) not in duplicate_ids
                        and int(path_row["panel_site_id"] or 0) <= 0
                    ):
                        duplicate_ids.append(int(path_row["id"]))
                    for duplicate_id in duplicate_ids:
                        self._merge_site_rows(connection, canonical_id, duplicate_id)
                    connection.execute(
                        """UPDATE sites SET name=?, document_root=?, log_path=?,
                                              web_server=?, enabled=1, updated_at=?
                           WHERE id=?""",
                        (
                            site.name,
                            site.document_root,
                            site.log_path,
                            site.web_server,
                            now,
                            canonical_id,
                        ),
                    )
                    if previous_log_path != site.log_path:
                        cursor = connection.execute(
                            "SELECT log_path FROM file_cursors WHERE site_id=?",
                            (canonical_id,),
                        ).fetchone()
                        if cursor is not None and str(cursor["log_path"] or "") != site.log_path:
                            connection.execute(
                                "DELETE FROM file_cursors WHERE site_id=?", (canonical_id,)
                            )
                    return canonical_id
            connection.execute(
                """
                INSERT INTO sites(panel_site_id, name, document_root, log_path, web_server,
                                  enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(log_path) DO UPDATE SET
                    panel_site_id=excluded.panel_site_id,
                    name=excluded.name,
                    document_root=excluded.document_root,
                    web_server=excluded.web_server,
                    enabled=1,
                    updated_at=excluded.updated_at
                """,
                (
                    site.panel_site_id,
                    site.name,
                    site.document_root,
                    site.log_path,
                    site.web_server,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM sites WHERE log_path=?", (site.log_path,)
            ).fetchone()
            return int(row["id"])

    def retain_sites(self, log_paths: Iterable[str]) -> None:
        """仅启用仍存在于宝塔 sites 表中的站点，保留历史数据以便恢复。"""
        retained = [str(path) for path in log_paths]
        with self.session() as connection:
            connection.execute("UPDATE sites SET enabled=0")
            if retained:
                placeholders = ",".join("?" for _ in retained)
                connection.execute(
                    "UPDATE sites SET enabled=1 WHERE log_path IN ({})".format(placeholders),
                    retained,
                )

    def list_sites(self) -> List[Dict[str, Any]]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.panel_site_id, s.name, s.document_root, s.log_path,
                       s.web_server, s.enabled, c.offset, c.size, c.updated_at AS collected_at
                FROM sites s
                LEFT JOIN file_cursors c ON c.site_id=s.id
                WHERE s.enabled=1
                ORDER BY s.name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_cursor(self, site_id: int) -> Optional[FileCursor]:
        with self.session() as connection:
            row = connection.execute(
                "SELECT inode, device, offset, size, mtime_ns FROM file_cursors WHERE site_id=?",
                (site_id,),
            ).fetchone()
        if row is None:
            return None
        return FileCursor(
            inode=int(row["inode"]),
            device=int(row["device"]),
            offset=int(row["offset"]),
            size=int(row["size"]),
            mtime_ns=int(row["mtime_ns"]),
        )

    def save_cursor(
        self, site_id: int, log_path: str, cursor: FileCursor
    ) -> None:
        with self.session() as connection:
            self._upsert_cursor(connection, site_id, log_path, cursor)

    def clear_cursor(self, site_id: int) -> None:
        with self.session() as connection:
            connection.execute("DELETE FROM file_cursors WHERE site_id=?", (int(site_id),))

    def history_import_status(self, site_id: int) -> Optional[Dict[str, Any]]:
        with self.session() as connection:
            row = connection.execute(
                "SELECT * FROM history_import WHERE site_id=?", (int(site_id),)
            ).fetchone()
        return dict(row) if row is not None else None

    def needs_history_import(self, site_id: int) -> bool:
        state = self.history_import_status(site_id)
        return state is None or str(state.get("status")) != "complete"

    def begin_history_import(self, site_id: int) -> bool:
        """首次开始返回 True；中断续跑返回 False。"""
        now = int(time.time())
        with self.session() as connection:
            row = connection.execute(
                "SELECT status FROM history_import WHERE site_id=?", (int(site_id),)
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO history_import
                       (site_id,status,started_at,updated_at) VALUES (?,'running',?,?)""",
                    (int(site_id), now, now),
                )
                return True
            connection.execute(
                "UPDATE history_import SET status='running',updated_at=?,error='' WHERE site_id=?",
                (now, int(site_id)),
            )
        return False

    def update_history_import(
        self, site_id: int, result: Dict[str, Any], complete: bool = False
    ) -> None:
        now = int(time.time())
        with self.session() as connection:
            connection.execute(
                """UPDATE history_import SET status=?,lines=lines+?,events=events+?,
                   rejected=rejected+?,updated_at=?,completed_at=? WHERE site_id=?""",
                (
                    "complete" if complete else "running",
                    int(result.get("lines", 0)),
                    int(result.get("events", 0)),
                    int(result.get("rejected", 0)),
                    now,
                    now if complete else 0,
                    int(site_id),
                ),
            )

    @staticmethod
    def _upsert_cursor(
        connection: sqlite3.Connection,
        site_id: int,
        log_path: str,
        cursor: FileCursor,
    ) -> None:
        connection.execute(
            """
            INSERT INTO file_cursors(site_id, log_path, inode, device, offset, size,
                                     mtime_ns, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(site_id) DO UPDATE SET
                log_path=excluded.log_path,
                inode=excluded.inode,
                device=excluded.device,
                offset=excluded.offset,
                size=excluded.size,
                mtime_ns=excluded.mtime_ns,
                updated_at=excluded.updated_at
            """,
            (
                site_id,
                log_path,
                str(cursor.inode),
                str(cursor.device),
                cursor.offset,
                cursor.size,
                cursor.mtime_ns,
                int(time.time()),
            ),
        )

    def record_batch(
        self,
        site_id: int,
        canonical_log_path: str,
        cursor: Optional[FileCursor],
        events: Iterable[AccessEvent],
        privacy_salt: str,
        static_extensions: Iterable[str],
        hll_precision: int = 10,
    ) -> None:
        minute_stats: Dict[int, Dict[str, int]] = defaultdict(
            lambda: {"requests": 0, "pv": 0, "bytes": 0, "errors": 0, "bots": 0}
        )
        hll_precision = max(4, min(16, int(hll_precision)))
        unique_sketches: Dict[tuple, HyperLogLog] = {}
        for event in events:
            minute = event.timestamp - (event.timestamp % 60)
            day = datetime.fromtimestamp(event.timestamp).strftime("%Y-%m-%d")
            item = minute_stats[minute]
            item["requests"] += 1
            item["pv"] += int(is_page_view(event, static_extensions))
            item["bytes"] += max(0, event.body_bytes)
            item["errors"] += int(400 <= event.status <= 599)
            item["bots"] += int(spider_name(event.user_agent) is not None)
            hour = minute - (minute % 3600)
            for kind, value in (
                ("uv", visitor_hash(event, privacy_salt)),
                ("ip", ip_hash(event, privacy_salt)),
            ):
                key = (hour, kind)
                if key not in unique_sketches:
                    unique_sketches[key] = HyperLogLog(hll_precision)
                unique_sketches[key].add(value)

        with self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for minute, item in minute_stats.items():
                connection.execute(
                    """
                    INSERT INTO metric_minute(site_id, minute_ts, requests, pv, body_bytes,
                                              errors, bot_requests)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(site_id, minute_ts) DO UPDATE SET
                        requests=requests+excluded.requests,
                        pv=pv+excluded.pv,
                        body_bytes=body_bytes+excluded.body_bytes,
                        errors=errors+excluded.errors,
                        bot_requests=bot_requests+excluded.bot_requests
                    """,
                    (
                        site_id,
                        minute,
                        item["requests"],
                        item["pv"],
                        item["bytes"],
                        item["errors"],
                        item["bots"],
                    ),
                )
            for (hour, kind), sketch in unique_sketches.items():
                self._store_hll(connection, site_id, hour, kind, sketch)
            if cursor is not None:
                self._upsert_cursor(connection, site_id, canonical_log_path, cursor)

    def record_live_batch(
        self,
        site_id: int,
        events: Iterable[AccessEvent],
        privacy_salt: str,
        static_extensions: Iterable[str],
        hll_precision: int = 10,
    ) -> None:
        self.record_batch(
            site_id=site_id,
            canonical_log_path="",
            cursor=None,
            events=events,
            privacy_salt=privacy_salt,
            static_extensions=static_extensions,
            hll_precision=hll_precision,
        )

    def set_state(self, key: str, value: Any) -> None:
        serialized = json.dumps(value, ensure_ascii=False)
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO runtime_state(state_key, state_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_value=excluded.state_value,
                    updated_at=excluded.updated_at
                """,
                (key, serialized, int(time.time())),
            )

    def get_health(self) -> Dict[str, Any]:
        state: Dict[str, Any] = {}
        with self.session() as connection:
            for row in connection.execute(
                "SELECT state_key, state_value, updated_at FROM runtime_state"
            ):
                try:
                    state[row["state_key"]] = json.loads(row["state_value"])
                except ValueError:
                    state[row["state_key"]] = row["state_value"]
            sites = connection.execute("SELECT COUNT(*) AS total FROM sites").fetchone()
        state["site_count"] = int(sites["total"])
        state["database_bytes"] = self.db_path.stat().st_size if self.db_path.exists() else 0
        return state

    def has_statistics(self) -> bool:
        with self.session() as connection:
            row = connection.execute(
                "SELECT 1 FROM metric_minute LIMIT 1"
            ).fetchone()
        return row is not None

    def has_site_statistics(self, site_id: int) -> bool:
        """判断指定站点是否已有聚合数据，不能用全库状态代替。"""
        with self.session() as connection:
            row = connection.execute(
                "SELECT 1 FROM metric_minute WHERE site_id=? LIMIT 1", (int(site_id),)
            ).fetchone()
        return row is not None

    def get_overview(self, site_id: int, start_ts: int, end_ts: int) -> Dict[str, Any]:
        with self.session() as connection:
            totals = connection.execute(
                """
                SELECT COALESCE(SUM(requests),0) AS requests,
                       COALESCE(SUM(pv),0) AS pv,
                       COALESCE(SUM(body_bytes),0) AS body_bytes,
                       COALESCE(SUM(errors),0) AS errors,
                       COALESCE(SUM(bot_requests),0) AS bot_requests
                FROM metric_minute
                WHERE site_id=? AND minute_ts>=? AND minute_ts<?
                """,
                (site_id, start_ts, end_ts),
            ).fetchone()
            unique = self._read_hll_counts(connection, site_id, start_ts, end_ts)
            latest = connection.execute(
                """
                SELECT minute_ts, requests, body_bytes FROM metric_minute
                WHERE site_id=? AND minute_ts>=? AND minute_ts<?
                ORDER BY minute_ts DESC LIMIT 1
                """,
                (site_id, start_ts, end_ts),
            ).fetchone()
        result = dict(totals)
        result["uv"] = unique["uv"]
        result["ip"] = unique["ip"]
        result["realtime_bytes"] = int(latest["body_bytes"]) if latest else 0
        result["qps"] = round((int(latest["requests"]) / 60.0), 2) if latest else 0
        return result

    def get_site_summaries(
        self, site_ids: Iterable[int], start_ts: int, end_ts: int
    ) -> Dict[int, Dict[str, Any]]:
        """批量读取网站列表指标，避免按站点逐一执行概览查询。"""
        valid_ids = sorted({int(site_id) for site_id in site_ids if int(site_id) > 0})
        summaries: Dict[int, Dict[str, Any]] = {
            site_id: {
                "requests": 0,
                "pv": 0,
                "body_bytes": 0,
                "errors": 0,
                "bot_requests": 0,
                "uv": 0,
                "ip": 0,
                "last_seen": 0,
            }
            for site_id in valid_ids
        }
        if not valid_ids:
            return summaries

        with self.session() as connection:
            # 每批不超过 500 个参数，兼容 SQLite 常见的 999 参数上限。
            for offset in range(0, len(valid_ids), 500):
                batch = valid_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                metric_rows = connection.execute(
                    """
                    SELECT site_id, COALESCE(SUM(requests),0) AS requests,
                           COALESCE(SUM(pv),0) AS pv,
                           COALESCE(SUM(body_bytes),0) AS body_bytes,
                           COALESCE(SUM(errors),0) AS errors,
                           COALESCE(SUM(bot_requests),0) AS bot_requests,
                           COALESCE(MAX(minute_ts),0) AS last_seen
                    FROM metric_minute
                    WHERE site_id IN ({}) AND minute_ts>=? AND minute_ts<?
                    GROUP BY site_id
                    """.format(placeholders),
                    tuple(batch) + (start_ts, end_ts),
                ).fetchall()
                for row in metric_rows:
                    site_id = int(row["site_id"])
                    summaries[site_id].update(
                        {
                            "requests": int(row["requests"] or 0),
                            "pv": int(row["pv"] or 0),
                            "body_bytes": int(row["body_bytes"] or 0),
                            "errors": int(row["errors"] or 0),
                            "bot_requests": int(row["bot_requests"] or 0),
                            "last_seen": int(row["last_seen"] or 0),
                        }
                    )

                unique_rows = connection.execute(
                    """SELECT site_id, kind, precision, registers
                       FROM unique_hll_hour
                       WHERE site_id IN ({}) AND hour_ts>=? AND hour_ts<?
                       ORDER BY site_id, kind, hour_ts""".format(placeholders),
                    tuple(batch) + (start_ts - (start_ts % 3600), end_ts),
                ).fetchall()
                sketches: Dict[tuple, HyperLogLog] = {}
                for row in unique_rows:
                    key = (int(row["site_id"]), str(row["kind"]))
                    current = HyperLogLog.loads(
                        row["registers"], int(row["precision"])
                    )
                    if key in sketches:
                        sketches[key].merge(current)
                    else:
                        sketches[key] = current
                for (site_id, kind), sketch in sketches.items():
                    summaries[site_id][kind] = sketch.estimate()
        return summaries

    @staticmethod
    def _read_hll_counts(
        connection: sqlite3.Connection, site_id: int, start_ts: int, end_ts: int
    ) -> Dict[str, int]:
        sketches: Dict[str, Optional[HyperLogLog]] = {"uv": None, "ip": None}
        rows = connection.execute(
            """SELECT kind, precision, registers FROM unique_hll_hour
               WHERE site_id=? AND hour_ts>=? AND hour_ts<?""",
            (site_id, start_ts - (start_ts % 3600), end_ts),
        ).fetchall()
        for row in rows:
            kind = str(row["kind"])
            current = HyperLogLog.loads(row["registers"], int(row["precision"]))
            if sketches[kind] is None:
                sketches[kind] = current
            else:
                sketches[kind].merge(current)
        return {
            kind: sketch.estimate() if sketch is not None else 0
            for kind, sketch in sketches.items()
        }

    def get_trend(
        self, site_id: int, start_ts: int, end_ts: int, bucket_seconds: int
    ) -> List[Dict[str, Any]]:
        buckets: Dict[int, Dict[str, Any]] = {}
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT (minute_ts / ?) * ? AS bucket,
                       SUM(requests) AS requests,
                       SUM(pv) AS pv,
                       SUM(body_bytes) AS body_bytes,
                       SUM(errors) AS errors,
                       SUM(bot_requests) AS bot_requests
                FROM metric_minute
                WHERE site_id=? AND minute_ts>=? AND minute_ts<?
                GROUP BY bucket ORDER BY bucket
                """,
                (bucket_seconds, bucket_seconds, site_id, start_ts, end_ts),
            ).fetchall()
            for row in rows:
                bucket = int(row["bucket"])
                buckets[bucket] = {
                    "timestamp": bucket,
                    "requests": int(row["requests"] or 0),
                    "pv": int(row["pv"] or 0),
                    "body_bytes": int(row["body_bytes"] or 0),
                    "errors": int(row["errors"] or 0),
                    "bot_requests": int(row["bot_requests"] or 0),
                    "uv": 0,
                    "ip": 0,
                }
            hll_buckets: Dict[tuple, HyperLogLog] = {}
            unique_rows = connection.execute(
                """SELECT hour_ts, kind, precision, registers FROM unique_hll_hour
                   WHERE site_id=? AND hour_ts>=? AND hour_ts<? ORDER BY hour_ts""",
                (site_id, start_ts - (start_ts % 3600), end_ts),
            ).fetchall()
            for row in unique_rows:
                bucket = (int(row["hour_ts"]) // bucket_seconds) * bucket_seconds
                key = (bucket, str(row["kind"]))
                sketch = HyperLogLog.loads(row["registers"], int(row["precision"]))
                if key in hll_buckets:
                    hll_buckets[key].merge(sketch)
                else:
                    hll_buckets[key] = sketch
            for (bucket, field), sketch in hll_buckets.items():
                if bucket not in buckets:
                    buckets[bucket] = {
                        "timestamp": bucket,
                        "requests": 0,
                        "pv": 0,
                        "body_bytes": 0,
                        "errors": 0,
                        "bot_requests": 0,
                        "uv": 0,
                        "ip": 0,
                    }
                buckets[bucket][field] = sketch.estimate()
        empty = {
            "requests": 0,
            "pv": 0,
            "body_bytes": 0,
            "errors": 0,
            "bot_requests": 0,
            "uv": 0,
            "ip": 0,
        }
        first_bucket = (start_ts // bucket_seconds) * bucket_seconds
        result: List[Dict[str, Any]] = []
        current = first_bucket
        while current < end_ts:
            item = dict(empty)
            item.update(buckets.get(current, {}))
            item["timestamp"] = current
            result.append(item)
            current += bucket_seconds
        return result

#!/usr/bin/python3
# coding: utf-8
"""增量日志采集器。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .metrics import is_excluded
from .parsers import AccessEvent, parse_access_line
from .repository import FileCursor, Repository
from .settings import DATA_DIR, load_config
from .site_discovery import SiteDefinition, discover_sites


def _stat_cursor(path: Path, offset: int) -> FileCursor:
    stat = path.stat()
    return FileCursor(
        inode=int(stat.st_ino),
        device=int(stat.st_dev),
        offset=int(offset),
        size=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
    )


def _find_rotated_file(canonical: Path, cursor: FileCursor) -> Optional[Path]:
    try:
        candidates = canonical.parent.glob(canonical.name + "*")
        for candidate in candidates:
            if candidate == canonical or not candidate.is_file():
                continue
            stat = candidate.stat()
            if int(stat.st_ino) == cursor.inode and int(stat.st_dev) == cursor.device:
                return candidate
    except OSError:
        return None
    return None


def _sources_for_run(
    canonical: Path, cursor: Optional[FileCursor], initial_offset: int = 0
) -> List[Tuple[Path, int]]:
    if not canonical.is_file():
        return []
    current = canonical.stat()
    if cursor is None:
        return [(canonical, max(0, initial_offset))]
    if int(current.st_ino) == cursor.inode and int(current.st_dev) == cursor.device:
        offset = 0 if int(current.st_size) < cursor.offset else cursor.offset
        return [(canonical, offset)]
    rotated = _find_rotated_file(canonical, cursor)
    sources: List[Tuple[Path, int]] = []
    if rotated is not None:
        sources.append((rotated, cursor.offset))
    sources.append((canonical, 0))
    return sources


def _read_batches(
    source: Path,
    start_offset: int,
    batch_size: int,
    discard_partial_line: bool = False,
) -> Iterable[Tuple[List[str], int]]:
    with source.open("rb") as stream:
        stream.seek(max(0, start_offset))
        if start_offset > 0 and discard_partial_line:
            # 尾部补录可能落在一行中间，丢弃该残行后再解析完整 combined 日志。
            stream.readline()
        while True:
            lines: List[str] = []
            while len(lines) < batch_size:
                raw = stream.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    stream.seek(-len(raw), os.SEEK_CUR)
                    break
                lines.append(raw.decode("utf-8", "replace"))
            offset = stream.tell()
            if not lines:
                break
            yield lines, offset


def collect_site(
    repository: Repository,
    site_id: int,
    site: SiteDefinition,
    config: Dict[str, object],
    deadline: float,
) -> Dict[str, int]:
    canonical = Path(site.log_path)
    result = {"lines": 0, "events": 0, "rejected": 0}
    if not canonical.is_file():
        return result

    cursor = repository.get_cursor(site_id)
    if cursor is None and bool(config.get("collect_from_end", True)):
        stat = canonical.stat()
        repository.save_cursor(site_id, str(canonical), _stat_cursor(canonical, stat.st_size))
        return result

    initial_offset = 0
    if cursor is None:
        tail_bytes = max(
            1048576,
            min(268435456, int(config.get("initial_tail_bytes", 33554432))),
        )
        initial_offset = max(0, canonical.stat().st_size - tail_bytes)
    sources = _sources_for_run(canonical, cursor, initial_offset)
    batch_size = max(100, min(20000, int(config.get("batch_size", 5000))))
    excluded_paths = config.get("excluded_paths", [])
    static_extensions = config.get("static_extensions", [])
    salt = str(config["privacy_salt"])

    for source, offset in sources:
        discard_partial = (
            cursor is None
            and initial_offset > 0
            and source == canonical
            and offset == initial_offset
        )
        for lines, next_offset in _read_batches(
            source, offset, batch_size, discard_partial
        ):
            events: List[AccessEvent] = []
            for line in lines:
                result["lines"] += 1
                event = parse_access_line(line)
                if event is None or is_excluded(event, excluded_paths):
                    result["rejected"] += 1
                    continue
                events.append(event)
            result["events"] += len(events)
            repository.record_batch(
                site_id=site_id,
                canonical_log_path=str(canonical),
                cursor=_stat_cursor(source, next_offset),
                events=events,
                privacy_salt=salt,
                static_extensions=static_extensions,
                hll_precision=int(config.get("hll_precision", 10)),
            )
            if time.monotonic() >= deadline:
                return result
    return result


def _run_once_unlocked(config: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    config = config or load_config()
    repository = Repository()
    repository.initialize()
    started = int(time.time())
    summary: Dict[str, object] = {
        "started_at": started,
        "finished_at": started,
        "discovered_sites": 0,
        "sites": 0,
        "lines": 0,
        "events": 0,
        "rejected": 0,
        "errors": [],
    }
    if not bool(config.get("enabled", True)):
        repository.set_state("last_run", summary)
        return summary

    discovered = list(discover_sites(config))
    summary["discovered_sites"] = len(discovered)
    only_missing = bool(config.get("only_sites_without_statistics", False))
    candidates = []
    for site in discovered:
        try:
            site_id = repository.register_site(site)
            if only_missing and repository.has_site_statistics(site_id):
                continue
            candidates.append((site_id, site))
        except Exception as error:
            summary["errors"].append({"site": site.name, "message": str(error)[:500]})

    budget = max(5, min(300, int(config.get("run_budget_seconds", 45))))
    deadline = time.monotonic() + budget
    for index, (site_id, site) in enumerate(candidates):
        if time.monotonic() >= deadline:
            break
        try:
            summary["sites"] = int(summary["sites"]) + 1
            # 把剩余时间公平分给尚未处理的站点，避免首个大日志独占补录预算。
            remaining_sites = max(1, len(candidates) - index)
            remaining_seconds = max(0.0, deadline - time.monotonic())
            site_deadline = min(
                deadline,
                time.monotonic() + max(0.25, remaining_seconds / remaining_sites),
            )
            site_result = collect_site(repository, site_id, site, config, site_deadline)
            for key in ("lines", "events", "rejected"):
                summary[key] = int(summary[key]) + site_result[key]
        except Exception as error:  # 单站异常不能阻断其他站点采集
            summary["errors"].append({"site": site.name, "message": str(error)[:500]})

    summary["finished_at"] = int(time.time())
    repository.set_state("last_run", summary)
    return summary


def _acquire_lock():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = (DATA_DIR / "collector.lock").open("a+")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        return handle
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def run_once(config: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    lock_handle = _acquire_lock()
    if lock_handle is None:
        return {
            "skipped": True,
            "reason": "collector is already running",
            "finished_at": int(time.time()),
        }
    try:
        return _run_once_unlocked(config)
    finally:
        lock_handle.close()

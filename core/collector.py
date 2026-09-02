#!/usr/bin/python3
# coding: utf-8
"""增量日志采集器。"""

from __future__ import annotations

import os
import gzip
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


def _history_files(canonical: Path) -> List[Path]:
    """返回同一访问日志的轮转文件和当前文件，按旧到新排序。"""
    try:
        candidates = [
            path
            for path in canonical.parent.glob(canonical.name + "*")
            if path.is_file()
        ]
    except OSError:
        candidates = []
    if canonical.is_file() and canonical not in candidates:
        candidates.append(canonical)

    def order(path: Path):
        try:
            return (int(path.stat().st_mtime_ns), str(path))
        except OSError:
            return (0, str(path))

    rotated = sorted((path for path in candidates if path != canonical), key=order)
    if canonical.is_file():
        rotated.append(canonical)
    return rotated


def _same_file(path: Path, cursor: FileCursor) -> bool:
    try:
        stat_result = path.stat()
        return (
            int(stat_result.st_ino) == cursor.inode
            and int(stat_result.st_dev) == cursor.device
        )
    except OSError:
        return False


def _sources_for_run(
    canonical: Path,
    cursor: Optional[FileCursor],
    initial_offset: int = 0,
    include_history: bool = False,
) -> List[Tuple[Path, int]]:
    if include_history:
        history = _history_files(canonical)
        if not history:
            return []
        if cursor is None:
            return [(path, 0) for path in history]
        for index, path in enumerate(history):
            if _same_file(path, cursor):
                return [(path, cursor.offset)] + [
                    (next_path, 0) for next_path in history[index + 1 :]
                ]
        # 已记录的轮转文件可能被系统清理；从当前日志开始继续实时增量，
        # 不重读仍在目录中的其他历史文件，避免重复累计。
        return [(canonical, 0)] if canonical.is_file() else []
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
    stream_context = (
        gzip.open(str(source), "rb")
        if source.name.lower().endswith(".gz")
        else source.open("rb")
    )
    with stream_context as stream:
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
    full_history = bool(config.get("full_history", False))
    result = {"lines": 0, "events": 0, "rejected": 0, "complete": False}
    if not canonical.is_file() and not (full_history and _history_files(canonical)):
        result["complete"] = True
        return result

    cursor = repository.get_cursor(site_id)
    if bool(config.get("force_tail_backfill", False)):
        # 早期版本可能在未生成任何指标前就把游标保存到文件末尾。
        # 对明确判定为“无统计”的预热任务忽略该旧游标，重新读取受限日志尾部。
        cursor = None
    if cursor is None and bool(config.get("collect_from_end", True)):
        stat = canonical.stat()
        repository.save_cursor(site_id, str(canonical), _stat_cursor(canonical, stat.st_size))
        return result

    initial_offset = 0
    if cursor is None and not full_history:
        tail_bytes = max(
            1048576,
            min(268435456, int(config.get("initial_tail_bytes", 33554432))),
        )
        initial_offset = max(0, canonical.stat().st_size - tail_bytes)
    sources = _sources_for_run(
        canonical,
        cursor,
        initial_offset,
        include_history=full_history,
    )
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
    result["complete"] = True
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
        "completed_site_ids": [],
        "incomplete_site_ids": [],
        "errors": [],
    }
    if not bool(config.get("enabled", True)):
        repository.set_state("last_run", summary)
        return summary

    discovered = list(discover_sites(config))
    summary["discovered_sites"] = len(discovered)
    only_missing = bool(config.get("only_sites_without_statistics", False))
    target_site_ids = {
        int(site_id) for site_id in config.get("target_site_ids", [])
    }
    candidates = []
    for site in discovered:
        try:
            site_id = repository.register_site(site)
            if target_site_ids and site_id not in target_site_ids:
                continue
            if only_missing and repository.has_site_statistics(site_id):
                continue
            if (
                bool(config.get("reset_empty_site_cursors", False))
                and not repository.has_site_statistics(site_id)
            ):
                repository.clear_cursor(site_id)
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
            completion_key = (
                "completed_site_ids" if site_result["complete"] else "incomplete_site_ids"
            )
            summary[completion_key].append(site_id)
        except Exception as error:  # 单站异常不能阻断其他站点采集
            summary["errors"].append({"site": site.name, "message": str(error)[:500]})

    summary["finished_at"] = int(time.time())
    repository.set_state("last_run", summary)
    return summary


def _run_history_backfill_unlocked(
    config: Optional[Dict[str, object]] = None,
    requested_site_ids: Optional[Iterable[int]] = None,
) -> Dict[str, object]:
    """完整且可续跑地导入无统计站点的现存日志（含轮转及 gzip）。"""
    backfill_config = dict(config or load_config())
    repository = Repository()
    repository.initialize()
    discovered = list(discover_sites(backfill_config))
    requested = {int(site_id) for site_id in (requested_site_ids or [])}
    pending: List[int] = []
    for site in discovered:
        site_id = repository.register_site(site)
        if requested and site_id not in requested:
            continue
        if repository.has_site_statistics(site_id):
            continue
        repository.clear_cursor(site_id)
        pending.append(site_id)

    started = int(time.time())
    total: Dict[str, object] = {
        "status": "complete",
        "started_at": started,
        "finished_at": started,
        "discovered_sites": len(discovered),
        "requested_site_ids": sorted(pending),
        "completed_site_ids": [],
        "incomplete_site_ids": sorted(pending),
        "sites": 0,
        "lines": 0,
        "events": 0,
        "rejected": 0,
        "errors": [],
    }
    if not pending:
        return total

    backfill_config.update(
        {
            "collect_from_end": False,
            "full_history": True,
            "force_tail_backfill": False,
            "only_sites_without_statistics": False,
            "reset_empty_site_cursors": False,
            "run_budget_seconds": max(
                30, min(300, int(backfill_config.get("run_budget_seconds", 45)))
            ),
        }
    )
    completed = set()
    while pending:
        backfill_config["target_site_ids"] = list(pending)
        result = _run_once_unlocked(backfill_config)
        for key in ("sites", "lines", "events", "rejected"):
            total[key] = int(total[key]) + int(result.get(key, 0))
        total["errors"].extend(result.get("errors", []))
        round_completed = {int(value) for value in result["completed_site_ids"]}
        completed.update(round_completed)
        # 一轮的时间预算可能在后续站点开始前就耗尽；未出现在
        # completed_site_ids 的目标都必须保留到下一轮，不能静默漏掉。
        next_pending = [site_id for site_id in pending if site_id not in round_completed]
        if next_pending == pending and int(result.get("lines", 0)) == 0:
            total["status"] = "incomplete"
            break
        pending = next_pending

    total["completed_site_ids"] = sorted(completed)
    total["incomplete_site_ids"] = sorted(pending)
    total["finished_at"] = int(time.time())
    repository.set_state("last_run", total)
    return total


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


def run_history_backfill(
    config: Optional[Dict[str, object]] = None,
    requested_site_ids: Optional[Iterable[int]] = None,
) -> Dict[str, object]:
    lock_handle = _acquire_lock()
    if lock_handle is None:
        return {
            "status": "skipped",
            "skipped": True,
            "reason": "collector is already running",
            "finished_at": int(time.time()),
        }
    try:
        return _run_history_backfill_unlocked(config, requested_site_ids)
    finally:
        lock_handle.close()

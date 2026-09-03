#!/usr/bin/python3
# coding: utf-8
"""增量日志采集器。"""

from __future__ import annotations

import os
import gzip
import multiprocessing
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .metrics import is_excluded
from .parsers import AccessEvent, parse_access_line
from .repository import FileCursor, Repository
from .settings import DATA_DIR, load_config
from .site_discovery import SiteDefinition, discover_sites

HISTORY_IMPORT_DAYS = 30
HISTORY_DETAIL_DAYS = 7
MAX_PARSE_WORKERS = 4


def _parse_line_chunk(payload) -> Tuple[List[AccessEvent], int]:
    """进程安全的日志解析单元；解析进程不接触 SQLite。"""
    lines, excluded_paths, cutoff_ts = payload
    events: List[AccessEvent] = []
    rejected = 0
    for line in lines:
        event = parse_access_line(line)
        if (
            event is None
            or is_excluded(event, excluded_paths)
            or (cutoff_ts and event.timestamp < cutoff_ts)
        ):
            rejected += 1
        else:
            events.append(event)
    return events, rejected


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
    lower_name = source.name.lower()
    if lower_name.endswith((".tar.gz", ".tgz", ".tar")):
        yield from _read_tar_batches(source, start_offset, batch_size)
        return
    stream_context = (
        gzip.open(str(source), "rb")
        if lower_name.endswith(".gz")
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


def _read_tar_batches(
    source: Path, start_offset: int, batch_size: int
) -> Iterable[Tuple[List[str], int]]:
    """按归档成员内容的虚拟偏移续读 tar/tar.gz，忽略目录和元数据块。"""
    virtual_base = 0
    with tarfile.open(str(source), "r:*") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            member_end = virtual_base + int(member.size)
            if start_offset >= member_end:
                virtual_base = member_end
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                virtual_base = member_end
                continue
            with extracted as stream:
                local_offset = max(0, start_offset - virtual_base)
                stream.seek(local_offset)
                while True:
                    lines: List[str] = []
                    while len(lines) < batch_size:
                        raw = stream.readline()
                        if not raw:
                            break
                        # 归档日志已经关闭，不会再补齐末行；接受无换行的完整末行。
                        lines.append(raw.decode("utf-8", "replace"))
                    if not lines:
                        break
                    yield lines, virtual_base + stream.tell()
            virtual_base = member_end


def collect_site(
    repository: Repository,
    site_id: int,
    site: SiteDefinition,
    config: Dict[str, object],
    deadline: float,
    progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
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
    history_cutoff_ts = max(0, int(config.get("history_cutoff_ts", 0) or 0))
    if full_history and history_cutoff_ts:
        sources = [
            item for item in sources
            if item[0] == canonical or int(item[0].stat().st_mtime) >= history_cutoff_ts
        ]
    batch_size = max(100, min(50000, int(config.get("batch_size", 5000))))
    excluded_paths = config.get("excluded_paths", [])
    static_extensions = config.get("static_extensions", [])
    salt = str(config["privacy_salt"])
    detail_cutoff_ts = max(0, int(config.get("detail_cutoff_ts", 0) or 0))
    error_detail_cutoff_ts = max(0, int(config.get("error_detail_cutoff_ts", 0) or 0))
    parse_workers = 1
    if full_history:
        parse_workers = max(
            1,
            min(MAX_PARSE_WORKERS, int(config.get("history_parse_workers", max(1, min(4, (os.cpu_count() or 2) - 1))))),
        )
    executor = None

    source_count = len(sources)
    try:
        for source_index, (source, offset) in enumerate(sources):
            discard_partial = (
                cursor is None
                and initial_offset > 0
                and source == canonical
                and offset == initial_offset
            )
            for lines, next_offset in _read_batches(
                source, offset, batch_size, discard_partial
            ):
                result["lines"] += len(lines)
                events: List[AccessEvent] = []
                rejected = 0
                if parse_workers > 1 and len(lines) >= 10000:
                    chunk_size = max(2500, (len(lines) + parse_workers - 1) // parse_workers)
                    chunks = [
                        (lines[index:index + chunk_size], excluded_paths, history_cutoff_ts)
                        for index in range(0, len(lines), chunk_size)
                    ]
                    try:
                        if executor is None:
                            executor = ProcessPoolExecutor(
                                max_workers=parse_workers,
                                mp_context=multiprocessing.get_context("spawn"),
                            )
                        for parsed, chunk_rejected in executor.map(_parse_line_chunk, chunks):
                            events.extend(parsed)
                            rejected += chunk_rejected
                    except Exception:
                        if executor is not None:
                            executor.shutdown(wait=True)
                            executor = None
                        events, rejected = _parse_line_chunk((lines, excluded_paths, history_cutoff_ts))
                        parse_workers = 1
                else:
                    events, rejected = _parse_line_chunk((lines, excluded_paths, history_cutoff_ts))
                result["rejected"] += rejected
                result["events"] += len(events)
                repository.record_batch(
                    site_id=site_id,
                    canonical_log_path=str(canonical),
                    cursor=_stat_cursor(source, next_offset),
                    events=events,
                    privacy_salt=salt,
                    static_extensions=static_extensions,
                    hll_precision=int(config.get("hll_precision", 10)),
                    detail_cutoff_ts=detail_cutoff_ts,
                    error_detail_cutoff_ts=error_detail_cutoff_ts,
                )
                if progress_callback is not None:
                    try:
                        source_size = max(1, int(source.stat().st_size))
                    except OSError:
                        source_size = max(1, int(next_offset))
                    progress_callback(
                        {
                            "source": str(source),
                            "source_index": source_index + 1,
                            "source_count": source_count,
                            "source_offset": int(next_offset),
                            "source_size": source_size,
                            "lines": int(result["lines"]),
                            "events": int(result["events"]),
                            "rejected": int(result["rejected"]),
                            "parse_workers": parse_workers,
                        }
                    )
                if time.monotonic() >= deadline:
                    return result
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
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
        "site_results": {},
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
        site_id = 0
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
            callback = config.get("_progress_callback")
            site_result = collect_site(
                repository,
                site_id,
                site,
                config,
                site_deadline,
                (lambda progress, sid=site_id, current=site: callback({
                    **progress,
                    "site_id": sid,
                    "site_index": index + 1,
                    "site_count": len(candidates),
                    "site_name": current.name,
                    "log_path": current.log_path,
                })) if callable(callback) else None,
            )
            for key in ("lines", "events", "rejected"):
                summary[key] = int(summary[key]) + site_result[key]
            completion_key = (
                "completed_site_ids" if site_result["complete"] else "incomplete_site_ids"
            )
            summary[completion_key].append(site_id)
            summary["site_results"][str(site_id)] = {
                "name": site.name,
                "log_path": site.log_path,
                "lines": int(site_result["lines"]),
                "events": int(site_result["events"]),
                "rejected": int(site_result["rejected"]),
                "complete": bool(site_result["complete"]),
            }
        except Exception as error:  # 单站异常不能阻断其他站点采集
            summary["errors"].append({"site": site.name, "message": str(error)[:500]})
            summary["site_results"][str(site_id)] = {
                "name": site.name,
                "log_path": site.log_path,
                "lines": 0,
                "events": 0,
                "rejected": 0,
                "complete": False,
                "error": str(error)[:500],
            }

    summary["finished_at"] = int(time.time())
    repository.set_state("last_run", summary)
    return summary


def _run_history_backfill_unlocked(
    config: Optional[Dict[str, object]] = None,
    requested_site_ids: Optional[Iterable[int]] = None,
    progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Dict[str, object]:
    """完整且可续跑地导入站点现存日志（含轮转及 gzip）。"""
    backfill_config = dict(config or load_config())
    history_days = HISTORY_IMPORT_DAYS
    history_cutoff_ts = int(time.time()) - history_days * 86400
    repository = Repository()
    repository.initialize()
    discovered = list(discover_sites(backfill_config))
    requested = {int(site_id) for site_id in (requested_site_ids or [])}
    pending: List[int] = []
    for site in discovered:
        site_id = repository.register_site(site)
        if requested and site_id not in requested:
            continue
        if not repository.needs_history_import(site_id):
            continue
        if repository.begin_history_import(site_id, history_days, history_cutoff_ts):
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
        "site_results": {},
        "sites": 0,
        "lines": 0,
        "events": 0,
        "rejected": 0,
        "errors": [],
    }
    if not pending:
        return total

    logs: List[Dict[str, object]] = []
    last_source = ""
    last_publish = 0.0
    round_base = {"lines": 0, "events": 0, "rejected": 0}
    round_site_progress: Dict[int, Dict[str, object]] = {}

    def publish(progress: Optional[Dict[str, object]] = None, force: bool = False) -> None:
        nonlocal last_source, last_publish
        progress = progress or {}
        progress_site_id = int(progress.get("site_id") or 0)
        if progress_site_id:
            round_site_progress[progress_site_id] = progress
        now_monotonic = time.monotonic()
        source = str(progress.get("source") or "")
        if source and source != last_source:
            last_source = source
            logs.append({
                "time": int(time.time()),
                "level": "info",
                "message": "开始读取 {}".format(source),
            })
            force = True
        if not force and now_monotonic - last_publish < 1.0:
            return
        source_count = max(1, int(progress.get("source_count") or 1))
        source_index = max(1, int(progress.get("source_index") or 1))
        source_size = max(1, int(progress.get("source_size") or 1))
        source_ratio = min(1.0, int(progress.get("source_offset") or 0) / source_size)
        site_ratio = min(1.0, ((source_index - 1) + source_ratio) / source_count)
        completed_sites = len(total["completed_site_ids"])
        site_index = max(1, int(progress.get("site_index") or 1))
        percent = min(99.9, (completed_sites + site_index - 1 + site_ratio) * 100 / max(1, len(total["requested_site_ids"])))
        round_totals = {
            key: sum(int(item.get(key) or 0) for item in round_site_progress.values())
            for key in ("lines", "events", "rejected")
        }
        snapshot = {
            "status": "running",
            "started_at": started,
            "updated_at": int(time.time()),
            "requested_site_ids": total["requested_site_ids"],
            "completed_sites": completed_sites,
            "total_sites": len(total["requested_site_ids"]),
            "percent": round(percent, 1),
            "site_id": int(progress.get("site_id") or 0),
            "site_name": str(progress.get("site_name") or ""),
            "current_file": source,
            "file_index": source_index,
            "file_count": source_count,
            "lines": round_base["lines"] + round_totals["lines"],
            "events": round_base["events"] + round_totals["events"],
            "rejected": round_base["rejected"] + round_totals["rejected"],
            "parse_workers": int(progress.get("parse_workers") or 1),
            "logs": logs[-30:],
        }
        repository.set_state("history_backfill_progress", snapshot)
        if progress_callback is not None:
            progress_callback(snapshot)
        last_publish = now_monotonic

    logs.append({"time": started, "level": "info", "message": "开始恢复最近 {} 天历史数据，共 {} 个网站".format(history_days, len(pending))})
    publish(force=True)

    backfill_config.update(
        {
            "collect_from_end": False,
            "full_history": True,
            "force_tail_backfill": False,
            "only_sites_without_statistics": False,
            "reset_empty_site_cursors": False,
            # 历史恢复使用更大的事务批次，减少 SQLite 提交和索引维护次数。
            "batch_size": max(20000, min(50000, int(backfill_config.get("batch_size", 5000)) * 4)),
            "run_budget_seconds": max(
                30, min(300, int(backfill_config.get("run_budget_seconds", 45)))
            ),
            "history_cutoff_ts": history_cutoff_ts,
            "detail_cutoff_ts": int(time.time()) - HISTORY_DETAIL_DAYS * 86400,
            "error_detail_cutoff_ts": history_cutoff_ts,
        }
    )
    completed = set()
    while pending:
        round_base = {key: int(total[key]) for key in ("lines", "events", "rejected")}
        round_site_progress.clear()
        backfill_config["_progress_callback"] = publish
        backfill_config["target_site_ids"] = list(pending)
        result = _run_once_unlocked(backfill_config)
        for key in ("sites", "lines", "events", "rejected"):
            total[key] = int(total[key]) + int(result.get(key, 0))
        total["errors"].extend(result.get("errors", []))
        for item in result.get("errors", []):
            logs.append({
                "time": int(time.time()),
                "level": "error",
                "message": "{}：{}".format(item.get("site") or "站点", item.get("message") or "恢复失败"),
            })
        for site_id, site_result in result.get("site_results", {}).items():
            aggregate = total["site_results"].setdefault(
                str(site_id),
                {
                    "name": site_result.get("name", ""),
                    "log_path": site_result.get("log_path", ""),
                    "lines": 0,
                    "events": 0,
                    "rejected": 0,
                    "complete": False,
                },
            )
            for key in ("lines", "events", "rejected"):
                aggregate[key] = int(aggregate[key]) + int(site_result.get(key, 0))
            aggregate["complete"] = bool(site_result.get("complete", False))
            if site_result.get("error"):
                aggregate["error"] = site_result["error"]
        round_completed = {int(value) for value in result["completed_site_ids"]}
        for site_id in pending:
            site_result = result.get("site_results", {}).get(str(site_id), {})
            if site_result:
                repository.update_history_import(
                    site_id, site_result, site_id in round_completed
                )
        completed.update(round_completed)
        total["completed_site_ids"] = sorted(completed)
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
    try:
        repository.optimize_after_history_import()
    except Exception as error:
        logs.append({
            "time": int(time.time()),
            "level": "warn",
            "message": "数据库查询优化未完成：{}".format(str(error)[:200]),
        })
    logs.append({
        "time": int(total["finished_at"]),
        "level": "success" if total["status"] == "complete" else "warn",
        "message": "历史数据恢复{}：导入 {} 条，忽略 {} 行".format(
            "完成" if total["status"] == "complete" else "未完成",
            total["events"],
            total["rejected"],
        ),
    })
    final_progress = {
        "status": total["status"],
        "started_at": started,
        "updated_at": int(total["finished_at"]),
        "finished_at": int(total["finished_at"]),
        "requested_site_ids": total["requested_site_ids"],
        "completed_sites": len(total["completed_site_ids"]),
        "total_sites": len(total["requested_site_ids"]),
        "percent": round(len(total["completed_site_ids"]) * 100 / max(1, len(total["requested_site_ids"])), 1),
        "lines": total["lines"],
        "events": total["events"],
        "rejected": total["rejected"],
        "logs": logs[-30:],
    }
    repository.set_state("history_backfill_progress", final_progress)
    if progress_callback is not None:
        progress_callback(final_progress)
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
    progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
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
        return _run_history_backfill_unlocked(config, requested_site_ids, progress_callback)
    finally:
        lock_handle.close()

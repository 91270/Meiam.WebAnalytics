#!/usr/bin/python3
# coding: utf-8
"""接收 Nginx/Apache Unix datagram syslog 的常驻采集服务。"""

from __future__ import annotations

import os
import signal
import socket
import stat
import sys
import threading
import time
import traceback
from collections import defaultdict
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.metrics import is_excluded
from core.collector import run_once
from core.nginx_config import configure_all
from core.realtime_queue import RealtimeQueue
from core.repository import Repository
from core.settings import load_config
from core.site_discovery import discover_sites
from core.socket_protocol import parse_datagram


SOCKET_PATH = Path(os.environ.get("WEBANALYTICS_SOCKET_PATH", "/tmp/webanalytics.sock"))
running = True


def stop_service(signum, frame):
    global running
    running = False


def site_mapping(repository, config):
    mapping = {}
    discovered = discover_sites(config)
    for site in discovered:
        internal_id = repository.register_site(site)
        if site.panel_site_id > 0:
            mapping[site.panel_site_id] = internal_id
    retain_sites = getattr(repository, "retain_sites", None)
    if callable(retain_sites):
        retain_sites(site.log_path for site in discovered)
    return mapping


def prepare_socket():
    if SOCKET_PATH.exists() or SOCKET_PATH.is_symlink():
        mode = SOCKET_PATH.lstat().st_mode
        if not stat.S_ISSOCK(mode):
            raise RuntimeError("拒绝覆盖非 socket 路径: {}".format(SOCKET_PATH))
        SOCKET_PATH.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(SOCKET_PATH))
    os.chmod(str(SOCKET_PATH), 0o666)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    server.settimeout(0.5)
    return server


def main():
    config = load_config()
    repository = Repository()
    repository.initialize()
    # 先绑定 Socket，让 systemd/安装脚本能立即确认服务已就绪；首次补录可能持续数十秒。
    server = prepare_socket()
    processor = RealtimeQueue(repository, config)
    processor.start()
    mapping = site_mapping(repository, config)
    backfill_state = {"status": "skipped", "reason": "all sites already have statistics"}
    backfill_thread = None
    attempted_backfill_sites = set()

    def initial_backfill(site_ids):
        nonlocal backfill_state
        try:
            backfill_config = dict(config)
            backfill_config["collect_from_end"] = False
            backfill_config["only_sites_without_statistics"] = True
            result = run_once(backfill_config)
            result["status"] = "complete"
            result["requested_site_ids"] = sorted(site_ids)
            backfill_state = result
        except Exception as error:
            backfill_state = {
                "status": "error",
                "success": False,
                "message": str(error)[:500],
                "finished_at": int(time.time()),
            }

    def schedule_missing_backfill():
        nonlocal backfill_thread, backfill_state
        if backfill_thread is not None and backfill_thread.is_alive():
            return
        missing = {
            int(site_id)
            for site_id in mapping.values()
            if not repository.has_site_statistics(int(site_id))
            and int(site_id) not in attempted_backfill_sites
        }
        if not missing:
            return
        attempted_backfill_sites.update(missing)
        backfill_state = {
            "status": "running",
            "started_at": int(time.time()),
            "site_ids": sorted(missing),
        }
        backfill_thread = threading.Thread(
            target=initial_backfill,
            args=(missing,),
            name="webanalytics-initial-backfill",
            daemon=True,
        )
        backfill_thread.start()

    try:
        startup_config_sync = configure_all(True)
        sync_state = {
            "success": True,
            "updated_at": int(time.time()),
            "changed_sites": startup_config_sync.get("changed_sites", []),
            "message": "service startup sync",
        }
    except Exception as error:
        sync_state = {
            "success": False,
            "updated_at": int(time.time()),
            "message": str(error)[:500],
        }
    schedule_missing_backfill()
    received_by_site = defaultdict(int)
    received = 0
    rejected = 0
    last_received = 0
    last_health = 0.0
    last_sync = time.monotonic()

    repository.set_state(
        "realtime_service",
        {
            "running": True,
            "phase": "backfill" if backfill_state.get("status") == "running" else "running",
            "pid": os.getpid(),
            "socket": str(SOCKET_PATH),
            "initial_backfill": backfill_state,
            "updated_at": int(time.time()),
        },
    )

    signal.signal(signal.SIGTERM, stop_service)
    signal.signal(signal.SIGINT, stop_service)
    try:
        while running:
            try:
                payload = server.recv(65535)
                parsed = parse_datagram(payload, config.get("trusted_proxy_cidrs", []))
                if parsed is None:
                    rejected += 1
                else:
                    panel_site_id, event = parsed
                    if is_excluded(event, config.get("excluded_paths", [])):
                        rejected += 1
                    else:
                        if panel_site_id not in mapping:
                            mapping = site_mapping(repository, config)
                        internal_id = mapping.get(panel_site_id)
                        if internal_id is None:
                            rejected += 1
                        else:
                            received += 1
                            last_received = int(time.time())
                            if processor.submit(internal_id, event):
                                received_by_site[internal_id] += 1
            except socket.timeout:
                pass

            now = time.monotonic()
            sync_interval = max(10, min(3600, int(config.get("config_sync_seconds", 15))))
            if now - last_sync >= sync_interval:
                try:
                    config = load_config()
                    mapping = site_mapping(repository, config)
                    result = configure_all(True)
                    schedule_missing_backfill()
                    sync_state = {
                        "success": True,
                        "updated_at": int(time.time()),
                        "changed_sites": result.get("changed_sites", []),
                    }
                except Exception as error:
                    sync_state = {
                        "success": False,
                        "updated_at": int(time.time()),
                        "message": str(error)[:500],
                    }
                last_sync = now

            if now - last_health >= 10.0:
                phase = "backfill" if backfill_state.get("status") == "running" else "running"
                repository.set_state(
                    "realtime_service",
                    {
                        "running": True,
                        "phase": phase,
                        "pid": os.getpid(),
                        "socket": str(SOCKET_PATH),
                        "received": received,
                        "received_by_site": dict(received_by_site),
                        "rejected": rejected,
                        "last_received": last_received,
                        "queue": processor.snapshot(),
                        "initial_backfill": backfill_state,
                        "config_sync": sync_state,
                        "updated_at": int(time.time()),
                    },
                )
                last_health = now
    finally:
        processor.stop()
        repository.set_state(
            "realtime_service",
            {
                "running": False,
                "pid": os.getpid(),
                "queue": processor.snapshot(),
                "updated_at": int(time.time()),
            },
        )
        server.close()
        try:
            if SOCKET_PATH.exists() and stat.S_ISSOCK(SOCKET_PATH.lstat().st_mode):
                SOCKET_PATH.unlink()
        except OSError:
            pass


def record_fatal_error(error):
    """把启动/运行崩溃原因留在统计库，同时继续写入 systemd journal。"""
    try:
        repository = Repository()
        repository.initialize()
        repository.set_state(
            "realtime_service",
            {
                "running": False,
                "phase": "error",
                "error": str(error)[:1000],
                "updated_at": int(time.time()),
            },
        )
    except Exception:
        pass
    traceback.print_exc()


if __name__ == "__main__":
    try:
        main()
    except Exception as fatal_error:
        record_fatal_error(fatal_error)
        raise

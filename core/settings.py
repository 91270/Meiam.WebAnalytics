#!/usr/bin/python3
# coding: utf-8
"""插件路径和本地配置管理。"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict


PLUGIN_ROOT = Path(
    os.environ.get(
        "WEBANALYTICS_PLUGIN_DIR",
        Path(__file__).resolve().parents[1],
    )
).resolve()
SERVER_DATA_DIR = Path("/www/server/webanalytics/data")
DEFAULT_DATA_DIR = (
    SERVER_DATA_DIR if Path("/www/server").is_dir() else PLUGIN_ROOT / "data"
)
DATA_DIR = Path(
    os.environ.get("WEBANALYTICS_DATA_DIR", DEFAULT_DATA_DIR)
).resolve()
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = Path(os.environ.get("WEBANALYTICS_DB_PATH", DATA_DIR / "stats.db")).resolve()


DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "log_dir": os.environ.get("WEBANALYTICS_LOG_DIR", "/www/wwwlogs"),
    "panel_db": os.environ.get(
        "WEBANALYTICS_PANEL_DB", "/www/server/panel/data/default.db"
    ),
    "collect_from_end": True,
    "initial_tail_bytes": 33554432,
    "discover_orphan_logs": False,
    "batch_size": 5000,
    "queue_size": 20000,
    "flush_size": 500,
    "flush_interval_seconds": 1.0,
    "config_sync_seconds": 15,
    "hll_precision": 10,
    "trusted_proxy_cidrs": [
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
    ],
    "run_budget_seconds": 45,
    "raw_retention_days": 7,
    "error_retention_days": 30,
    "static_extensions": [
        "css",
        "js",
        "map",
        "jpg",
        "jpeg",
        "png",
        "gif",
        "webp",
        "svg",
        "ico",
        "woff",
        "woff2",
        "ttf",
        "eot",
        "mp3",
        "mp4",
        "avi",
        "mov",
        "zip",
        "gz",
        "rar",
        "7z",
        "pdf",
    ],
    "excluded_paths": [],
    "privacy_salt": "",
}


def _atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_config() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current: Dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, ValueError):
            current = {}

    migrated = False
    if current.get("config_sync_seconds") == 60:
        # 0.4.0 及更早版本的内部默认值；缩短新增站点自动接入等待。
        current["config_sync_seconds"] = 15
        migrated = True

    config = dict(DEFAULTS)
    config.update(current)
    if not config.get("privacy_salt"):
        config["privacy_salt"] = secrets.token_hex(32)
        _atomic_write_json(CONFIG_PATH, config)
    elif migrated:
        _atomic_write_json(CONFIG_PATH, config)
    elif config != current and not CONFIG_PATH.exists():
        _atomic_write_json(CONFIG_PATH, config)
    return config


def save_config(config: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update(config)
    if not merged.get("privacy_salt"):
        merged["privacy_salt"] = secrets.token_hex(32)
    _atomic_write_json(CONFIG_PATH, merged)
    return merged

#!/usr/bin/python3
# coding: utf-8
"""从宝塔站点数据库与日志目录发现可监控站点。"""

from __future__ import annotations

import importlib
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List


NGINX_VHOST_DIR = Path("/www/server/panel/vhost/nginx")
APACHE_VHOST_DIR = Path("/www/server/panel/vhost/apache")


@dataclass(frozen=True)
class SiteDefinition:
    panel_site_id: int
    name: str
    document_root: str
    log_path: str
    web_server: str = "nginx"


def _safe_log_candidate(log_dir: Path, site_name: str) -> Path:
    safe_name = Path(site_name).name
    return (log_dir / (safe_name + ".log")).resolve()


def _web_server(site_name: str) -> str:
    safe_name = Path(site_name).name
    if (NGINX_VHOST_DIR / (safe_name + ".conf")).is_file():
        return "nginx"
    if (APACHE_VHOST_DIR / (safe_name + ".conf")).is_file():
        return "apache"
    return "nginx"


def _configured_log_path(log_dir: Path, site_name: str, web_server: str) -> Path:
    """优先读取站点真实日志路径；只接受配置日志目录内的普通路径。"""
    safe_name = Path(site_name).name
    vhost_dir = APACHE_VHOST_DIR if web_server == "apache" else NGINX_VHOST_DIR
    vhost_path = vhost_dir / (safe_name + ".conf")
    candidates = []
    try:
        content = vhost_path.read_text(encoding="utf-8", errors="replace")
        if web_server == "apache":
            values = re.findall(
                r"(?im)^\s*CustomLog\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
                content,
            )
            raw_paths = [next((item for item in match if item), "") for match in values]
        else:
            values = re.findall(
                r"(?im)^\s*access_log\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
                content,
            )
            raw_paths = [next((item for item in match if item), "") for match in values]
        allowed_root = log_dir.resolve()
        for raw_path in raw_paths:
            if (
                not raw_path
                or raw_path.lower() == "off"
                or raw_path.startswith("syslog:")
                or "$" in raw_path
            ):
                continue
            path = Path(raw_path)
            candidate = (path if path.is_absolute() else allowed_root / path).resolve()
            try:
                candidate.relative_to(allowed_root)
            except ValueError:
                continue
            candidates.append(candidate)
    except OSError:
        candidates = []
    if candidates:
        expected_names = {safe_name + ".log", safe_name + "-access_log"}
        candidates.sort(key=lambda path: (path.name not in expected_names, str(path)))
        return candidates[0]
    return (
        (log_dir / (safe_name + "-access_log")).resolve()
        if web_server == "apache"
        else _safe_log_candidate(log_dir, safe_name)
    )


def _from_rows(rows: Iterable[Any], log_dir: Path) -> List[SiteDefinition]:
    sites: List[SiteDefinition] = []
    for row in rows:
        if not isinstance(row, dict):
            try:
                row = dict(row)
            except (TypeError, ValueError):
                continue
        name = str(row.get("name") or "").strip()
        try:
            panel_site_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            panel_site_id = 0
        if not name or panel_site_id <= 0:
            continue
        web_server = _web_server(name)
        candidate = _configured_log_path(log_dir, name, web_server)
        sites.append(
            SiteDefinition(
                panel_site_id=panel_site_id,
                name=name,
                document_root=str(row.get("path") or ""),
                log_path=str(candidate),
                web_server=web_server,
            )
        )
    return sites


def _from_panel_api(log_dir: Path) -> List[SiteDefinition]:
    """与官方 site_total 一致，优先通过宝塔 public.M('sites') 读取站点。"""
    panel_root = Path("/www/server/panel")
    panel_class = panel_root / "class"
    if not panel_root.is_dir() or not panel_class.is_dir():
        return []
    class_path = str(panel_class)
    if class_path not in sys.path:
        sys.path.append(class_path)
    previous_cwd = os.getcwd()
    try:
        os.chdir(str(panel_root))
        public_module = importlib.import_module("public")
        try:
            rows = public_module.M("sites").field("id,name,path").select()
        except Exception:
            rows = public_module.M("sites").field("id,name").select()
        return _from_rows(rows if isinstance(rows, (list, tuple)) else [], log_dir)
    except Exception:
        return []
    finally:
        try:
            os.chdir(previous_cwd)
        except OSError:
            pass


def _from_panel_db(panel_db: Path, log_dir: Path) -> List[SiteDefinition]:
    if not panel_db.is_file():
        return []
    sites: List[SiteDefinition] = []
    try:
        connection = sqlite3.connect(str(panel_db))
        connection.row_factory = sqlite3.Row
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sites)").fetchall()
        }
        if not {"id", "name"}.issubset(columns):
            return []
        selected = ["id", "name"]
        if "path" in columns:
            selected.append("path")
        query = "SELECT {} FROM sites ORDER BY id".format(",".join(selected))
        sites = _from_rows((dict(row) for row in connection.execute(query)), log_dir)
    except (OSError, sqlite3.DatabaseError, ValueError):
        return []
    finally:
        try:
            connection.close()
        except (NameError, UnboundLocalError):
            pass
    return sites


def discover_sites(config: Dict[str, object]) -> List[SiteDefinition]:
    log_dir = Path(str(config["log_dir"])).resolve()
    panel_db = Path(str(config["panel_db"])).resolve()
    discovered = _from_panel_api(log_dir)
    if not discovered:
        discovered = _from_panel_db(panel_db, log_dir)
    known = {site.log_path for site in discovered}

    if bool(config.get("discover_orphan_logs", False)) and log_dir.is_dir():
        next_fallback_id = -1
        for log_file in sorted(log_dir.glob("*.log")):
            if log_file.name.endswith(".error.log") or log_file.name == "access.log":
                continue
            resolved = str(log_file.resolve())
            if resolved in known:
                continue
            discovered.append(
                SiteDefinition(
                    panel_site_id=next_fallback_id,
                    name=log_file.name[:-4],
                    document_root="",
                    log_path=resolved,
                )
            )
            next_fallback_id -= 1
    return discovered

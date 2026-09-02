#!/usr/bin/python3
# coding: utf-8
"""宝塔 Linux 面板 WebAnalytics 插件后端入口。"""

from __future__ import annotations

import os
import importlib
import importlib.util
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Tuple


PANEL_ROOT = Path("/www/server/panel")
PLUGIN_ROOT = Path(__file__).resolve().parent
if PANEL_ROOT.is_dir():
    os.chdir(str(PANEL_ROOT))
    panel_class = str(PANEL_ROOT / "class")
    if panel_class not in sys.path:
        sys.path.append(panel_class)
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

try:
    import public  # type: ignore  # 宝塔面板运行时提供
except ImportError:  # 允许本地自动测试导入
    public = None

def _load_runtime_package():
    """以版本化名称加载核心，避开宝塔常驻进程中残留的 ``core`` 模块。"""
    package_name = "_webanalytics_runtime_040"
    package_init = PLUGIN_ROOT / "core" / "__init__.py"
    loaded = sys.modules.get(package_name)
    loaded_file = Path(getattr(loaded, "__file__", "")).resolve() if loaded else None
    if loaded is None or loaded_file != package_init.resolve():
        spec = importlib.util.spec_from_file_location(
            package_name,
            str(package_init),
            submodule_search_locations=[str(package_init.parent)],
        )
        if spec is None or spec.loader is None:
            raise ImportError("无法加载 WebAnalytics 核心模块")
        module = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = module
        spec.loader.exec_module(module)
    return package_name


_RUNTIME_PACKAGE = _load_runtime_package()
Repository = importlib.import_module(_RUNTIME_PACKAGE + ".repository").Repository
load_config = importlib.import_module(_RUNTIME_PACKAGE + ".settings").load_config
discover_sites = importlib.import_module(_RUNTIME_PACKAGE + ".site_discovery").discover_sites
configure_all = importlib.import_module(_RUNTIME_PACKAGE + ".nginx_config").configure_all


def _arg(args: Any, key: str, default: Any = None) -> Any:
    if isinstance(args, dict):
        return args.get(key, default)
    return getattr(args, key, default)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _period_range(period: str) -> Tuple[int, int, int]:
    now = datetime.now().astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "yesterday":
        start = today - timedelta(days=1)
        end = today
        bucket = 3600
    elif period == "7d":
        start = today - timedelta(days=6)
        end = now
        bucket = 86400
    elif period == "30d":
        start = today - timedelta(days=29)
        end = now
        bucket = 86400
    else:
        start = today
        end = now
        bucket = 3600
    return int(start.timestamp()), int(end.timestamp()) + 1, bucket


class WebAnalytics_main:
    def __init__(self):
        self.config = {}
        self.repository = None
        self.initialization_error = ""
        try:
            self.config = load_config()
            self.repository = Repository()
            self.repository.initialize()
        except Exception as error:
            # 宝塔会在类构造阶段之外处理插件方法；这里抛异常会直接变成 HTTP 500。
            self.initialization_error = str(error)[:500]

    def _repo(self):
        if self.repository is None:
            raise RuntimeError(
                "插件初始化失败：{}".format(self.initialization_error or "未知错误")
            )
        return self.repository

    @staticmethod
    def _ok(data: Any = None, message: str = "ok") -> Dict[str, Any]:
        return {
            "success": True,
            "message": message,
            "data": data,
            "request_id": str(int(time.time() * 1000)),
        }

    @staticmethod
    def _error(message: str) -> Dict[str, Any]:
        return {
            "success": False,
            "message": message,
            "data": None,
            "request_id": str(int(time.time() * 1000)),
        }

    def _sync_sites(self):
        repository = self._repo()
        discovered = discover_sites(self.config)
        for site in discovered:
            repository.register_site(site)
        retained = {site.log_path for site in discovered}
        retain_sites = getattr(repository, "retain_sites", None)
        if callable(retain_sites):
            retain_sites(retained)
        # 宝塔面板升级插件后可能短暂缓存旧 Repository 类；入口层继续完成白名单过滤。
        return [
            site for site in repository.list_sites()
            if str(site.get("log_path") or "") in retained
        ]

    @staticmethod
    def _diagnostics(site: Dict[str, Any], health: Dict[str, Any]) -> Dict[str, Any]:
        socket_path = Path("/tmp/webanalytics.sock")
        socket_ready = False
        try:
            socket_ready = socket_path.exists() and stat.S_ISSOCK(socket_path.lstat().st_mode)
        except OSError:
            socket_ready = False

        panel_site_id = int(site.get("panel_site_id") or 0)
        safe_name = Path(str(site.get("name") or "")).name
        web_server = "apache" if site.get("web_server") == "apache" else "nginx"
        vhost_dir = Path("/www/server/panel/vhost") / web_server
        vhost_path = vhost_dir / (safe_name + ".conf")
        extension_path = vhost_dir / "extension" / safe_name / "webanalytics.conf"
        expected_tag = "tag=wa_{}_access".format(panel_site_id)
        if web_server == "apache":
            expected_tag = "wa_{}_access".format(panel_site_id)
        configured = False
        try:
            configured = panel_site_id > 0 and expected_tag in extension_path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            configured = False

        service = health.get("realtime_service") or {}
        updated_at = int(service.get("updated_at") or 0)
        service_ready = bool(service.get("running")) and int(time.time()) - updated_at <= 30
        received_map = service.get("received_by_site") or {}
        queue_state = service.get("queue") or {}
        internal_site_id = int(site.get("id") or 0)
        received_for_site = int(received_map.get(str(internal_site_id), 0) or 0)
        return {
            "socket_ready": socket_ready,
            "service_ready": service_ready,
            "nginx_configured": configured,
            "webserver_configured": configured,
            "web_server": web_server,
            "received_for_site": received_for_site,
            "vhost": str(vhost_path),
            "extension": str(extension_path),
            "queue": queue_state,
            "config_sync": service.get("config_sync") or {},
        }

    def get_bootstrap(self, args):
        try:
            repository = self._repo()
            sites = self._sync_sites()
            if not sites:
                return self._ok(
                    {
                        "sites": [],
                        "selected_site_id": None,
                        "overview": {},
                        "previous": {},
                        "trend": [],
                        "health": repository.get_health(),
                    },
                    "未发现已开启访问日志的网站",
                )
            requested_id = _bounded_int(_arg(args, "site_id", 0), 0, 0, 2147483647)
            valid_ids = {int(site["id"]) for site in sites}
            site_id = requested_id if requested_id in valid_ids else int(sites[0]["id"])
            selected_site = next(site for site in sites if int(site["id"]) == site_id)
            period = str(_arg(args, "period", "today"))
            if period not in {"today", "yesterday", "7d", "30d"}:
                period = "today"
            start_ts, end_ts, bucket = _period_range(period)
            duration = end_ts - start_ts
            previous_start = start_ts - duration
            previous_end = start_ts
            health = repository.get_health()
            return self._ok(
                {
                    "sites": sites,
                    "selected_site_id": site_id,
                    "period": period,
                    "range": {"start": start_ts, "end": end_ts, "bucket": bucket},
                    "overview": repository.get_overview(site_id, start_ts, end_ts),
                    "previous": repository.get_overview(
                        site_id, previous_start, previous_end
                    ),
                    "trend": repository.get_trend(site_id, start_ts, end_ts, bucket),
                    "previous_trend": repository.get_trend(
                        site_id, previous_start, previous_end, bucket
                    ),
                    "health": health,
                    "diagnostics": self._diagnostics(selected_site, health),
                    "generated_at": int(time.time()),
                }
            )
        except Exception as error:
            return self._error("读取统计失败：{}".format(str(error)[:300]))

    def get_overview(self, args):
        return self.get_bootstrap(args)

    def get_sites(self, args):
        try:
            repository = self._repo()
            sites = self._sync_sites()
            period = str(_arg(args, "period", "today"))
            if period not in {"today", "yesterday", "7d", "30d"}:
                period = "today"
            start_ts, end_ts, _bucket = _period_range(period)
            health = repository.get_health()
            summaries = repository.get_site_summaries(
                (int(site["id"]) for site in sites), start_ts, end_ts
            )
            result = []
            for site in sites:
                site_id = int(site["id"])
                metrics = summaries.get(site_id, {})
                diagnostics = self._diagnostics(site, health)
                if not diagnostics["service_ready"] or not diagnostics["socket_ready"]:
                    status = {"key": "error", "label": "服务异常"}
                elif not diagnostics["webserver_configured"]:
                    status = {"key": "unconfigured", "label": "未接入"}
                elif diagnostics["received_for_site"] or int(metrics.get("requests") or 0):
                    status = {"key": "collecting", "label": "采集中"}
                else:
                    status = {"key": "waiting", "label": "等待访问"}
                requests = int(metrics.get("requests") or 0)
                errors = int(metrics.get("errors") or 0)
                result.append(
                    {
                        "id": site_id,
                        "panel_site_id": int(site.get("panel_site_id") or 0),
                        "name": str(site.get("name") or ""),
                        "web_server": str(site.get("web_server") or "nginx"),
                        "status": status,
                        "metrics": metrics,
                        "error_rate": round(errors * 100.0 / requests, 2) if requests else 0,
                    }
                )
            return self._ok(
                {
                    "sites": result,
                    "period": period,
                    "range": {"start": start_ts, "end": end_ts},
                    "generated_at": int(time.time()),
                }
            )
        except Exception as error:
            return self._error("读取网站列表失败：{}".format(str(error)[:300]))

    def get_health(self, args):
        try:
            return self._ok(self._repo().get_health())
        except Exception as error:
            return self._error("读取采集状态失败：{}".format(str(error)[:300]))

    def collect_now(self, args):
        try:
            return self._ok(
                self._repo().get_health(),
                "当前为 Unix Socket 实时采集模式，无需手动扫描日志",
            )
        except Exception as error:
            return self._error("读取采集状态失败：{}".format(str(error)[:300]))

    def repair_realtime(self, args):
        try:
            restart = subprocess.run(
                ["systemctl", "restart", "webanalytics.service"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
            )
            if restart.returncode != 0:
                return self._error("实时服务重启失败：{}".format(restart.stdout[-500:]))
            result = configure_all(True)
            return self._ok(result, "实时采集配置已修复")
        except Exception as error:
            return self._error("修复实时采集失败：{}".format(str(error)[:500]))

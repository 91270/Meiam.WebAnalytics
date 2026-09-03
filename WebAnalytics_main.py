#!/usr/bin/python3
# coding: utf-8
"""宝塔 Linux 面板 WebAnalytics 插件后端入口。"""

from __future__ import annotations

import os
import csv
import io
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
    package_name = "_webanalytics_runtime_0506"
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
save_config = importlib.import_module(_RUNTIME_PACKAGE + ".settings").save_config
discover_sites = importlib.import_module(_RUNTIME_PACKAGE + ".site_discovery").discover_sites
configure_all = importlib.import_module(_RUNTIME_PACKAGE + ".nginx_config").configure_all
geoip_database_status = importlib.import_module(_RUNTIME_PACKAGE + ".ip_location").database_status


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
        extension_configured = False
        include_configured = False
        try:
            extension_configured = panel_site_id > 0 and expected_tag in extension_path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            extension_configured = False
        try:
            vhost_content = vhost_path.read_text(encoding="utf-8", errors="replace")
            normalized_vhost = vhost_content.replace("\\", "/")
            include_marker = "/extension/{}/".format(safe_name)
            include_configured = include_marker in normalized_vhost
        except OSError:
            include_configured = False
        configured = extension_configured and include_configured

        service = health.get("realtime_service") or {}
        updated_at = int(service.get("updated_at") or 0)
        service_ready = bool(service.get("running")) and int(time.time()) - updated_at <= 30
        received_map = service.get("received_by_site") or {}
        queue_state = service.get("queue") or {}
        internal_site_id = int(site.get("id") or 0)
        received_for_site = int(received_map.get(str(internal_site_id), 0) or 0)
        initial_backfill = service.get("initial_backfill") or {}
        last_run = health.get("last_run") or {}
        backfill_site_ids = {
            int(value)
            for value in (
                initial_backfill.get("site_ids")
                or initial_backfill.get("requested_site_ids")
                or []
            )
            if str(value).lstrip("-").isdigit()
        }
        return {
            "socket_ready": socket_ready,
            "service_ready": service_ready,
            "nginx_configured": configured,
            "webserver_configured": configured,
            "web_server": web_server,
            "received_for_site": received_for_site,
            "backfill_for_site": (
                service.get("phase") == "backfill"
                and internal_site_id in backfill_site_ids
            ),
            "vhost": str(vhost_path),
            "extension": str(extension_path),
            "extension_file_ready": extension_configured,
            "extension_include_ready": include_configured,
            "queue": queue_state,
            "config_sync": service.get("config_sync") or {},
            "history_import": (last_run.get("site_results") or {}).get(
                str(internal_site_id), {}
            ),
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

    def get_spiders(self, args):
        try:
            repository = self._repo()
            sites = self._sync_sites()
            if not sites:
                return self._ok({"sites": [], "ranking": [], "trend": [], "summary": {}})
            requested_id = _bounded_int(_arg(args, "site_id", 0), 0, 0, 2147483647)
            valid_ids = {int(site["id"]) for site in sites}
            site_id = requested_id if requested_id in valid_ids else int(sites[0]["id"])
            period = str(_arg(args, "period", "today"))
            if period not in {"today", "yesterday", "7d", "30d"}:
                period = "today"
            start_ts, end_ts, bucket = _period_range(period)
            data = repository.get_spiders(site_id, start_ts, end_ts, bucket)
            data.update({
                "sites": sites,
                "selected_site_id": site_id,
                "period": period,
                "range": {"start": start_ts, "end": end_ts, "bucket": bucket},
                "generated_at": int(time.time()),
            })
            return self._ok(data)
        except Exception as error:
            return self._error("读取蜘蛛统计失败：{}".format(str(error)[:300]))

    def _query_context(self, args):
        sites = self._sync_sites()
        if not sites:
            raise RuntimeError("未发现已开启访问日志的网站")
        requested_id = _bounded_int(_arg(args, "site_id", 0), 0, 0, 2147483647)
        valid_ids = {int(site["id"]) for site in sites}
        site_id = requested_id if requested_id in valid_ids else int(sites[0]["id"])
        period = str(_arg(args, "period", "today"))
        if period not in {"today", "yesterday", "7d", "30d"}:
            period = "today"
        start_ts, end_ts, bucket = _period_range(period)
        return sites, site_id, period, start_ts, end_ts, bucket

    def get_clients(self, args):
        try:
            sites, site_id, period, start_ts, end_ts, _ = self._query_context(args)
            data = self._repo().get_client_stats(site_id, start_ts, end_ts)
            return self._ok({"sites": sites, "selected_site_id": site_id, "period": period,
                             "dimensions": data, "generated_at": int(time.time())})
        except Exception as error:
            return self._error("读取客户端统计失败：{}".format(str(error)[:300]))

    def _get_rank(self, args, kind):
        try:
            sites, site_id, period, start_ts, end_ts, _ = self._query_context(args)
            rows = self._repo().get_rank(kind, site_id, start_ts, end_ts, 100)
            data = {"sites": sites, "selected_site_id": site_id, "period": period,
                    "items": rows, "generated_at": int(time.time())}
            if kind == "ip":
                data["geoip"] = geoip_database_status()
            return self._ok(data)
        except Exception as error:
            return self._error("读取排行失败：{}".format(str(error)[:300]))

    def get_ip_rank(self, args):
        return self._get_rank(args, "ip")

    def get_uri_rank(self, args):
        return self._get_rank(args, "uri")

    def _get_request_page(self, args, errors_only=False):
        try:
            sites, site_id, period, start_ts, end_ts, _ = self._query_context(args)
            page = _bounded_int(_arg(args, "page", 1), 1, 1, 100000)
            page_size = _bounded_int(_arg(args, "page_size", 50), 50, 10, 200)
            query = str(_arg(args, "query", ""))[:100]
            status_group = str(_arg(args, "status_group", ""))
            data = self._repo().get_requests(site_id, start_ts, end_ts, page, page_size,
                                               errors_only, query, status_group)
            data.update({"sites": sites, "selected_site_id": site_id, "period": period,
                         "generated_at": int(time.time())})
            return self._ok(data)
        except Exception as error:
            return self._error("读取访问明细失败：{}".format(str(error)[:300]))

    def get_errors(self, args):
        return self._get_request_page(args, True)

    def get_requests(self, args):
        return self._get_request_page(args, False)

    def get_reports(self, args):
        try:
            sites, site_id, period, start_ts, end_ts, bucket = self._query_context(args)
            overview = self._repo().get_overview(site_id, start_ts, end_ts)
            return self._ok({"sites": sites, "selected_site_id": site_id, "period": period,
                             "overview": overview,
                             "top_uri": self._repo().get_rank("uri", site_id, start_ts, end_ts, 10),
                             "top_ip": self._repo().get_rank("ip", site_id, start_ts, end_ts, 10),
                             "trend": self._repo().get_trend(site_id, start_ts, end_ts, bucket),
                             "generated_at": int(time.time())})
        except Exception as error:
            return self._error("生成统计报告失败：{}".format(str(error)[:300]))

    def get_settings(self, args):
        safe = {key: self.config.get(key) for key in (
            "enabled", "raw_retention_days", "error_retention_days", "analytics_retention_days", "queue_size",
            "batch_size", "hll_precision", "excluded_paths", "static_extensions"
        )}
        return self._ok(safe)

    def save_settings(self, args):
        try:
            updated = dict(self.config)
            updated["enabled"] = str(_arg(args, "enabled", "true")).lower() in {"1", "true", "yes", "on"}
            updated["raw_retention_days"] = _bounded_int(_arg(args, "raw_retention_days", 7), 7, 1, 365)
            updated["error_retention_days"] = _bounded_int(_arg(args, "error_retention_days", 30), 30, 1, 730)
            updated["analytics_retention_days"] = _bounded_int(_arg(args, "analytics_retention_days", 90), 90, 30, 3650)
            updated["queue_size"] = _bounded_int(_arg(args, "queue_size", 20000), 20000, 1000, 200000)
            raw_paths = str(_arg(args, "excluded_paths", ""))
            updated["excluded_paths"] = [item.strip() for item in raw_paths.split("\n") if item.strip().startswith("/")][:100]
            self.config = save_config(updated)
            return self._ok(self.get_settings({})["data"], "设置已保存，采集服务将在下一次同步时应用")
        except Exception as error:
            return self._error("保存设置失败：{}".format(str(error)[:300]))

    def set_site_enabled(self, args):
        try:
            site_id = _bounded_int(_arg(args, "site_id", 0), 0, 1, 2147483647)
            enabled = str(_arg(args, "enabled", "true")).lower() in {"1", "true", "yes", "on"}
            if not self._repo().set_site_enabled(site_id, enabled):
                return self._error("网站不存在")
            return self._ok({"site_id": site_id, "enabled": enabled}, "网站采集状态已更新")
        except Exception as error:
            return self._error("更新网站采集状态失败：{}".format(str(error)[:300]))

    def clear_data(self, args):
        try:
            if str(_arg(args, "confirm", "")) != "CLEAR":
                return self._error("清理数据需要确认标记 CLEAR")
            site_id = _bounded_int(_arg(args, "site_id", 0), 0, 1, 2147483647)
            self._repo().clear_site_data(site_id)
            return self._ok({"site_id": site_id}, "网站统计数据已清理")
        except Exception as error:
            return self._error("清理数据失败：{}".format(str(error)[:300]))

    def export_csv(self, args):
        response = self.get_errors(args) if str(_arg(args, "type", "requests")) == "errors" else self.get_requests(args)
        if not response.get("success"):
            return response
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["时间", "IP", "方法", "URI", "状态码", "流量", "浏览器", "系统", "设备", "蜘蛛"])
        for row in response["data"].get("items", []):
            values = [datetime.fromtimestamp(int(row["timestamp"])).isoformat(sep=" "), row["remote_addr"],
                      row["method"], row["uri"], row["status"], row["body_bytes"], row["browser"],
                      row["system"], row["device"], row["spider"]]
            writer.writerow(["'" + str(value) if str(value).startswith(("=", "+", "-", "@")) else value for value in values])
        return self._ok({"filename": "webanalytics-{}.csv".format(int(time.time())),
                         "content": "\ufeff" + output.getvalue()})

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

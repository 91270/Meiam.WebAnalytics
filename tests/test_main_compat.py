from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import WebAnalytics.WebAnalytics_main as main_module
from WebAnalytics.WebAnalytics_main import WebAnalytics_main
from WebAnalytics.core.repository import Repository as CoreRepository
from WebAnalytics.core.site_discovery import SiteDefinition


class LegacyRepository:
    """模拟升级期间仍被宝塔进程缓存的 0.2.0 Repository。"""

    def __init__(self):
        self.registered = []

    def register_site(self, site):
        self.registered.append(site)
        return len(self.registered)

    def list_sites(self):
        return [
            {"id": 1, "name": "example.test", "log_path": self.registered[0].log_path},
            {"id": 2, "name": "panel", "log_path": "/www/wwwlogs/panel.log"},
        ]


class MainCompatibilityTests(unittest.TestCase):
    def test_panel_runtime_uses_versioned_module_namespace(self):
        self.assertTrue(
            main_module.Repository.__module__.startswith("_webanalytics_runtime_040.")
        )

    def test_initialization_failure_returns_json_instead_of_http_500(self):
        with patch(
            "WebAnalytics.WebAnalytics_main.load_config",
            side_effect=RuntimeError("database unavailable"),
        ):
            instance = WebAnalytics_main()
        response = instance.get_bootstrap({})
        self.assertFalse(response["success"])
        self.assertIn("插件初始化失败", response["message"])

    def test_bootstrap_response_is_successful_and_json_serializable(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = CoreRepository(Path(directory) / "stats.db")
            repository.initialize()
            instance = object.__new__(WebAnalytics_main)
            instance.config = {}
            instance.repository = repository
            instance.initialization_error = ""
            discovered = [
                SiteDefinition(
                    panel_site_id=7,
                    name="api.test",
                    document_root="/www/wwwroot/api.test",
                    log_path="/www/wwwlogs/api.test.log",
                    web_server="nginx",
                )
            ]
            with patch(
                "WebAnalytics.WebAnalytics_main.discover_sites",
                return_value=discovered,
            ):
                response = instance.get_bootstrap({"period": "today"})
            self.assertTrue(response["success"])
            self.assertEqual(response["data"]["sites"][0]["name"], "api.test")
            json.dumps(response, ensure_ascii=False)

            with patch(
                "WebAnalytics.WebAnalytics_main.discover_sites",
                return_value=discovered,
            ):
                sites_response = instance.get_sites({"period": "unsupported"})
            self.assertTrue(sites_response["success"], sites_response)
            self.assertEqual(sites_response["data"]["period"], "today")
            self.assertEqual(sites_response["data"]["sites"][0]["name"], "api.test")
            self.assertIn("metrics", sites_response["data"]["sites"][0])
            self.assertIn("status", sites_response["data"]["sites"][0])
            json.dumps(sites_response, ensure_ascii=False)

    def test_old_repository_without_retain_sites_is_supported(self):
        instance = object.__new__(WebAnalytics_main)
        instance.config = {}
        instance.repository = LegacyRepository()
        discovered = [
            SiteDefinition(
                panel_site_id=1,
                name="example.test",
                document_root="/www/wwwroot/example.test",
                log_path="/www/wwwlogs/example.test.log",
                web_server="nginx",
            )
        ]
        with patch(
            "WebAnalytics.WebAnalytics_main.discover_sites",
            return_value=discovered,
        ):
            sites = instance._sync_sites()
        self.assertEqual([site["name"] for site in sites], ["example.test"])


if __name__ == "__main__":
    unittest.main()

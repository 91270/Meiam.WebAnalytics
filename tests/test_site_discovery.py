from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from WebAnalytics.core.repository import Repository
from WebAnalytics.core.site_discovery import SiteDefinition, discover_sites


class SiteDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir()
        self.panel_db = self.root / "default.db"
        with sqlite3.connect(str(self.panel_db)) as connection:
            connection.execute(
                "CREATE TABLE sites (id INTEGER PRIMARY KEY, name TEXT, path TEXT)"
            )
            connection.execute(
                "INSERT INTO sites(id, name, path) VALUES (7, 'example.test', '/www/wwwroot/example.test')"
            )
        (self.log_dir / "example.test.log").write_text("", encoding="utf-8")
        (self.log_dir / "panel.log").write_text("", encoding="utf-8")
        (self.log_dir / "proxy.log").write_text("", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def config(self, discover_orphan_logs=False):
        return {
            "log_dir": str(self.log_dir),
            "panel_db": str(self.panel_db),
            "discover_orphan_logs": discover_orphan_logs,
        }

    def test_only_panel_sites_are_discovered_by_default(self):
        sites = discover_sites(self.config())
        self.assertEqual([site.name for site in sites], ["example.test"])
        self.assertEqual(sites[0].panel_site_id, 7)

    def test_bt_public_api_is_preferred_over_direct_sqlite(self):
        api_site = SiteDefinition(
            9,
            "from-api.test",
            "/www/wwwroot/from-api.test",
            str(self.log_dir / "from-api.test.log"),
        )
        with patch(
            "WebAnalytics.core.site_discovery._from_panel_api",
            return_value=[api_site],
        ):
            sites = discover_sites(self.config())
        self.assertEqual([site.name for site in sites], ["from-api.test"])

    def test_real_log_path_is_read_from_nginx_vhost(self):
        vhost_dir = self.root / "vhost" / "nginx"
        vhost_dir.mkdir(parents=True)
        custom_log = self.log_dir / "custom-access.log"
        custom_log.write_text("", encoding="utf-8")
        (vhost_dir / "example.test.conf").write_text(
            "server {{\n    access_log \"{}\";\n}}\n".format(custom_log),
            encoding="utf-8",
        )
        with patch(
            "WebAnalytics.core.site_discovery.NGINX_VHOST_DIR", vhost_dir
        ):
            sites = discover_sites(self.config())
        self.assertEqual(sites[0].web_server, "nginx")
        self.assertEqual(sites[0].log_path, str(custom_log.resolve()))

    def test_orphan_log_discovery_requires_explicit_opt_in(self):
        sites = discover_sites(self.config(True))
        self.assertEqual(
            [site.name for site in sites],
            ["example.test", "panel", "proxy"],
        )

    def test_previous_orphan_rows_are_hidden_without_deleting_history(self):
        repository = Repository(self.root / "stats.db")
        repository.initialize()
        current, orphan = discover_sites(self.config(True))[:2]
        repository.register_site(current)
        repository.register_site(orphan)
        repository.retain_sites([current.log_path])
        self.assertEqual([site["name"] for site in repository.list_sites()], ["example.test"])

        with repository.connect() as connection:
            stored = connection.execute("SELECT COUNT(*) AS total FROM sites").fetchone()
        self.assertEqual(int(stored["total"]), 2)


if __name__ == "__main__":
    unittest.main()

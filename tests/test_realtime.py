from __future__ import annotations

import unittest

from WebAnalytics.core.nginx_config import (
    _ensure_include,
    _inject,
    _site_extension,
    _strip_blocks,
)
from WebAnalytics.core.socket_protocol import parse_datagram, resolve_client_ip


class RealtimeTests(unittest.TestCase):
    def test_parse_nginx_syslog_datagram(self):
        parsed = parse_datagram(
            b'<190>Sep  1 13:40:22 wa_17_access: '
            b'{"time":"2026-09-01T13:40:22+08:00","ip":"203.0.113.2",'
            b'"method":"GET","uri":"/news?id=1","protocol":"HTTP/2.0",'
            b'"status":200,"bytes":1234,"referer":"-","ua":"Mozilla/5.0"}'
        )
        self.assertIsNotNone(parsed)
        panel_site_id, event = parsed
        self.assertEqual(panel_site_id, 17)
        self.assertEqual(event.uri, "/news?id=1")
        self.assertEqual(event.body_bytes, 1234)

    def test_invalid_datagram_is_rejected(self):
        self.assertIsNone(parse_datagram(b"not webanalytics syslog"))

    def test_forwarded_ip_requires_trusted_peer(self):
        data = {
            "remote_addr": "10.0.0.8",
            "x_forwarded_for": "198.51.100.7, 10.0.0.7",
        }
        self.assertEqual(resolve_client_ip(data, ["10.0.0.0/8"]), "198.51.100.7")
        data["remote_addr"] = "203.0.113.9"
        self.assertEqual(resolve_client_ip(data, ["10.0.0.0/8"]), "203.0.113.9")

    def test_known_cdn_header_has_priority_for_trusted_peer(self):
        data = {
            "remote_addr": "127.0.0.1",
            "cf_connecting_ip": "2001:db8::8",
            "x_forwarded_for": "198.51.100.2",
        }
        self.assertEqual(resolve_client_ip(data, ["127.0.0.0/8"]), "2001:db8::8")

    def test_nginx_injection_is_repeatable_and_removable(self):
        original = """server {
    listen 80;
    location /assets/ {
        access_log /dev/null;
    }
    access_log /www/wwwlogs/example.test.log;
}
"""
        first = _inject(original, 5)
        self.assertEqual(first.count("WebAnalytics-Config-Start"), 2)
        self.assertEqual(first.count("tag=wa_5_access"), 2)
        second = _inject(first, 7)
        self.assertEqual(second.count("WebAnalytics-Config-Start"), 2)
        self.assertNotIn("tag=wa_5_access", second)
        self.assertEqual(second.count("tag=wa_7_access"), 2)
        self.assertEqual(_strip_blocks(second), original)

    def test_nginx_extension_include_is_repeatable(self):
        original = "server {\n    listen 80;\n}\n"
        include = "include /www/server/panel/vhost/nginx/extension/example.test/*.conf;"
        first = _ensure_include(original, include, "example.test", "nginx")
        second = _ensure_include(first, include, "example.test", "nginx")
        self.assertEqual(first, second)
        self.assertEqual(first.count("WebAnalytics-Extension-Start"), 1)
        self.assertEqual(_strip_blocks(first), original)

    def test_existing_bt_extension_include_is_reused(self):
        original = (
            "server {\n"
            "    include /www/server/panel/vhost/nginx/extension/example.test/*.conf;\n"
            "}\n"
        )
        result = _ensure_include(
            original,
            "include /www/server/panel/vhost/nginx/extension/example.test/*.conf;",
            "example.test",
            "nginx",
        )
        self.assertEqual(result, original)
        self.assertNotIn("WebAnalytics-Extension-Start", result)

    def test_apache_extension_uses_local_unix_socket(self):
        content = _site_extension(19, "apache")
        self.assertIn("/tmp/webanalytics.sock", content)
        self.assertIn("wa_19_access", content)


if __name__ == "__main__":
    unittest.main()

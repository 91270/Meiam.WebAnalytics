from __future__ import annotations

import unittest

from WebAnalytics.core.metrics import is_page_view, spider_name
from WebAnalytics.core.parsers import parse_access_line


class ParserTests(unittest.TestCase):
    def test_combined_log(self):
        event = parse_access_line(
            '203.0.113.8 - - [01/Sep/2026:10:20:30 +0800] '
            '"GET /articles?id=7 HTTP/1.1" 200 1536 '
            '"https://example.test/" "Mozilla/5.0"'
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.remote_addr, "203.0.113.8")
        self.assertEqual(event.method, "GET")
        self.assertEqual(event.uri, "/articles?id=7")
        self.assertEqual(event.status, 200)
        self.assertEqual(event.body_bytes, 1536)

    def test_ipv6_common_log(self):
        event = parse_access_line(
            '2001:db8::1 - - [01/Sep/2026:10:21:30 +0800] '
            '"HEAD /health HTTP/2.0" 204 -'
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.remote_addr, "2001:db8::1")
        self.assertEqual(event.body_bytes, 0)

    def test_invalid_line_is_rejected(self):
        self.assertIsNone(parse_access_line("not an access log"))

    def test_page_view_excludes_static_assets(self):
        page = parse_access_line(
            '192.0.2.1 - - [01/Sep/2026:10:20:30 +0800] '
            '"GET /news/1 HTTP/1.1" 200 100 "-" "Mozilla/5.0"'
        )
        asset = parse_access_line(
            '192.0.2.1 - - [01/Sep/2026:10:20:31 +0800] '
            '"GET /assets/app.js HTTP/1.1" 200 200 "-" "Mozilla/5.0"'
        )
        self.assertTrue(is_page_view(page, ["js", "css"]))
        self.assertFalse(is_page_view(asset, ["js", "css"]))

    def test_spider_detection(self):
        self.assertEqual(spider_name("Mozilla/5.0 (compatible; Baiduspider/2.0)"), "Baiduspider")
        self.assertEqual(spider_name("Mozilla/5.0 GPTBot/1.2"), "GPTBot")
        self.assertIsNone(spider_name("Mozilla/5.0 Chrome/140"))


if __name__ == "__main__":
    unittest.main()

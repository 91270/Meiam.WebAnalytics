from __future__ import annotations

import unittest

from WebAnalytics.core.ip_location import locate_ip


class IpLocationTests(unittest.TestCase):
    def test_private_and_loopback_addresses_are_labeled_offline(self):
        self.assertEqual(locate_ip("192.168.1.2")["location"], "内网地址")
        self.assertEqual(locate_ip("127.0.0.1")["location"], "本机")

    def test_invalid_address_is_not_looked_up(self):
        self.assertEqual(locate_ip("not-an-ip")["location"], "无效地址")


if __name__ == "__main__":
    unittest.main()

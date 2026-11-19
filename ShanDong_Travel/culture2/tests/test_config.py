"""Offline checks for the expanded collection scope and run limits."""
from urllib.parse import urlsplit
import unittest

import config
from culture.types import Budget


class CollectionConfigTests(unittest.TestCase):
    def test_seven_unique_sites_include_all_four_requested_sources(self):
        sites = config.OFFICIAL_SITES
        self.assertEqual(len(sites), 7)
        self.assertEqual(len({site["id"] for site in sites}), 7)
        self.assertTrue({
            "http://www.dongchangfu.gov.cn/",
            "https://liaocheng.dzwww.com/",
            "http://zrghj.liaocheng.gov.cn/",
            "https://yunhebowuguan.cn/",
        }.issubset({site["url"] for site in sites}))
        for site in sites:
            with self.subTest(site=site["id"]):
                self.assertIn(urlsplit(site["url"]).hostname, site["hosts"])

    def test_expanded_budgets_and_five_page_site_rotation(self):
        budget = Budget()
        self.assertEqual(config.MAX_CANDIDATES, 500)
        self.assertEqual(config.MAX_PAGES, 280)
        self.assertEqual(config.MAX_MODEL_CALLS, 501)
        self.assertEqual(config.MAX_REQUESTS, 1800)
        self.assertEqual(config.MAX_SECONDS, 90 * 60)
        self.assertEqual(config.MAX_DOWNLOAD_BYTES, 15 * 1024 ** 3)
        self.assertEqual(config.SITE_BATCH, 5)
        for field, name in (("max_candidates", "MAX_CANDIDATES"), ("max_pages", "MAX_PAGES"),
                            ("max_calls", "MAX_MODEL_CALLS"), ("max_requests", "MAX_REQUESTS"),
                            ("max_seconds", "MAX_SECONDS"), ("max_download_bytes", "MAX_DOWNLOAD_BYTES")):
            self.assertEqual(getattr(budget, field), getattr(config, name))


if __name__ == "__main__":
    unittest.main()

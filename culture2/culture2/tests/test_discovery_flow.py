"""Exercise real queue/discovery code without network access or paid model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import config
from culture.collector import _collect_plan
from culture.types import Budget
from culture.web import Response


class DiscoveryFlowTests(unittest.TestCase):
    def test_topic_request_reaches_download_review_and_delivery(self):
        site = {"id": "fixture", "name": "离线官网", "url": "https://example.org/",
                "hosts": ["example.org"], "search_enabled": False}
        plan = {"raw_request": "聊城风土人情", "city": "聊城", "targets": ["风土人情"],
                "themes": [], "media_type": "image"}
        article_url = "https://example.org/20261119/marriage.html"
        pages = {site["url"]: "<a href='/20261119/marriage.html'>聊城传统婚俗表演</a>",
                 article_url: "<h1>聊城传统婚俗表演</h1><div class='article-content'>"
                     "<p>本文介绍当地民俗节庆中的传统婚俗表演。</p><img src='/marriage.jpg'></div>"}
        web, model = Mock(), Mock()
        web.html.side_effect = lambda url, **kwargs: (url, pages[url])

        def download(url, dest, kind, referer):
            Path(dest).write_bytes(b"offline-fixture")
            return Response(url, b"", "image/jpeg", None)

        web.download.side_effect = download
        model.complete.return_value = json.dumps({"description": "街头演员身着传统婚服表演民俗。",
            "decision": "keep", "reason": "符合风土人情主题", "requirement_status": "match",
            "requirement_evidence": "传统婚俗", "uncertainties": [], "tags": [], "is_museum_display": False})
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "OFFICIAL_SITES", [site]), \
                patch.object(config, "OUTPUT_DIR", Path(temp) / "delivery"), \
                patch("culture.collector._check_media"):
            report = _collect_plan(plan, Budget(), model, web, lambda _: None)
            self.assertEqual(report["status"], "采集完成")
            self.assertEqual(report["candidates_attempted"], 1)
            web.download.assert_called_once()
            model.complete.assert_called_once()
            self.assertEqual(len(list((config.OUTPUT_DIR / "assets").iterdir())), 1)

    def test_duplicate_articles_do_not_keep_empty_pagination_running(self):
        site = {"id": "fixture", "name": "离线官网", "url": "https://example.org/",
                "hosts": ["example.org"], "search_enabled": False,
                "entry_urls": ["https://example.org/video/", "https://example.org/folk/"]}
        other_site = {"id": "other", "name": "另一个官网", "url": "https://other.example/",
                      "hosts": ["other.example"], "search_enabled": False}
        plan = {"raw_request": "聊城风土人情", "city": "聊城", "targets": [],
                "themes": ["风土人情"], "media_type": "image"}
        featured = "<a href='/20261119/fair.html'>民俗大集</a>"
        web = Mock()

        def html(url, **kwargs):
            if url == site["url"]:
                return url, featured
            if "/video/" in url:
                number = int(url.split("index_")[1].split(".")[0]) if "index_" in url else 1
                return url, featured + f"<a rel='next' href='index_{number + 1}.html'>下一页</a>"
            return url, "<h1>离线页面</h1>"

        web.html.side_effect = html
        messages = []
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "OFFICIAL_SITES", [site, other_site]), \
                patch.object(config, "OUTPUT_DIR", Path(temp) / "delivery"), \
                patch.object(config, "MAX_EMPTY_PAGINATION_PAGES", 3):
            report = _collect_plan(plan, Budget(max_pages=20), Mock(), web, messages.append)
        opened = [call.args[0] for call in web.html.call_args_list]
        self.assertEqual(sum("/video/" in url for url in opened), 3)
        self.assertIn("https://example.org/folk/", opened)
        self.assertIn(other_site["url"], opened)
        self.assertLess(report["pages_read"], 20)
        self.assertTrue(any("停止该栏翻页" in message for message in messages))
        self.assertTrue(all(row["new_article_links"] == 0 for row in report["visited_pages"]
                            if "/video/" in row["url"]))

    def run_fixture(self, max_pages=20):
        site = {"id": "fixture", "name": "离线官网", "url": "https://example.org/",
                "hosts": ["example.org"], "search_enabled": False,
                "entry_urls": ["https://example.org/tourism/"]}
        plan = {"raw_request": "聊城光岳楼", "city": "聊城", "targets": ["光岳楼"],
                "districts": [], "themes": [], "exclude_terms": [], "media_type": "image"}

        def article(images):
            return "<h1>光岳楼</h1><div class='article-content'><p>光岳楼的景观介绍。</p>" + "".join(
                f"<img src='/photo{number}.jpg' alt='光岳楼'>" for number in images) + "</div>"

        pages = {
            site["url"]: "<h1>首页没有本次相关链接</h1>",
            site["entry_urls"][0]: "<a href='/20261119/one.html'>光岳楼风光</a>"
                "<div class='pagination'><a href='index_2.html'>下一页</a></div>",
            "https://example.org/20261119/one.html": article([1]),
            "https://example.org/tourism/index_2.html": "<a href='/20261119/two.html'>光岳楼夜景</a>"
                "<div class='pagination'><a href='./'>上一页</a></div>",
            "https://example.org/20261119/two.html": article([2, 3, 4, 5]),
        }
        web = Mock()
        web.html.side_effect = lambda url, **kwargs: (url, pages[url])

        def download(url, dest, kind, referer):
            Path(dest).write_bytes(b"offline media bytes; validation is separate from discovery")
            return Response(url, b"", "image/jpeg", None)

        web.download.side_effect = download
        model = Mock()
        model.complete.return_value = json.dumps({"description": "古城楼阁的景观。", "decision": "keep",
            "reason": "符合需求", "requirement_status": "match", "requirement_evidence": "光岳楼",
            "uncertainties": [], "tags": [], "is_museum_display": False}, ensure_ascii=False)
        budget = Budget(max_pages=max_pages)
        messages = []
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "OFFICIAL_SITES", [site]), \
                patch.object(config, "OUTPUT_DIR", Path(temp) / "delivery"), \
                patch.object(config, "INITIAL_PAGES", 3), \
                patch("culture.collector._check_media"):
            report = _collect_plan(plan, budget, model, web, messages.append)
            if max_pages >= 5:
                self.assertEqual(report["status"], "采集完成")
                self.assertEqual(len(report["selected_asset_ids"]), 5)
                self.assertEqual(len(list((config.OUTPUT_DIR / "assets").iterdir())), 5)
                self.assertTrue(any("[自动扩展]" in message for message in messages))
            else:
                self.assertEqual(report["status"], "预算停止")
                self.assertLess(len(report["selected_asset_ids"]), 5)
                self.assertLessEqual(report["pages_read"], max_pages)
            self.assertFalse((config.OUTPUT_DIR / "review").exists())
        return report

    def test_empty_homepage_uses_configured_column_then_next_page(self):
        report = self.run_fixture()
        self.assertEqual(report["pages_read"], 5)
        self.assertTrue(any(row["kind"] == "pagination" for row in report["visited_pages"]))

    def test_expansion_honors_overall_page_budget_and_publishes_partial_results(self):
        self.run_fixture(max_pages=4)


if __name__ == "__main__":
    unittest.main()

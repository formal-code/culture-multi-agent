"""Regression checks for topic requests, using no network or paid calls."""
import json
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from culture.extract import article_matches_targets, find_links, pagination_branch
from culture.intent import finalize_intent
from culture.model import analyze_request
from culture.ranking import worth_opening
from culture.types import Article, Plan


SITE = {"id": "fixture", "name": "离线官网", "url": "https://example.org/",
        "hosts": ["example.org"], "search_enabled": False}


class ThemeIntentTests(unittest.TestCase):
    def test_display_exception_requires_explicit_user_request_evidence(self):
        for raw, evidence, expected in (
                ("聊城风土人情", "风土人情", False),
                ("聊城博物馆", "博物馆", False),
                ("聊城博物馆外观", "博物馆外观", False),
                ("聊城不要博物馆展品", "不要博物馆展品", False),
                ("聊城博物馆展品照片", "博物馆展品照片", True)):
            with self.subTest(raw=raw):
                client = Mock()
                client.complete.return_value = json.dumps({"city": "聊城", "targets": [],
                    "themes": [], "museum_display_requested": True,
                    "museum_display_request_evidence": evidence})
                result = analyze_request(client, raw)
                self.assertEqual(result["museum_display_requested"], expected)

    def test_model_topic_misclassification_is_repaired(self):
        for targets in ([], ["风土人情"]):
            with self.subTest(targets=targets):
                client = Mock()
                client.complete.return_value = json.dumps({"city": "聊城", "targets": targets,
                    "themes": ["风土人情"], "districts": [], "exclude_terms": []})
                plan = analyze_request(client, "聊城的风土人情")
                self.assertEqual(plan["targets"], [])
                self.assertEqual(plan["themes"], ["风土人情"])
                self.assertIn("婚俗", plan["theme_keywords"])
                self.assertIn("非遗", plan["theme_keywords"])
                client.complete.assert_called_once()

    def test_theme_discovery_finds_related_articles_without_literal_topic(self):
        plan = Plan("聊城的风土人情", targets=["风土人情"])
        html = ("<a href='/20261119/marriage.html'>聊城婚俗博物馆</a>"
                "<a href='/20261119/fair.html'>赶运河大集赏非遗文化</a>"
                "<a href='/20261119/industry.html'>工业园项目建设</a>"
                "<a href='/20261119/culture.html'>企业文化建设会议</a>")
        links = find_links(html, SITE["url"], SITE, plan, page_kind="home")
        self.assertEqual({link.title for link in links}, {"聊城婚俗博物馆", "赶运河大集赏非遗文化"})
        self.assertFalse(worth_opening("企业文化展览", plan)[0])
        self.assertTrue(article_matches_targets(Article("u", "运河大集", "传统婚俗和非遗技艺展示。", []), plan))
        self.assertFalse(article_matches_targets(Article("u", "项目工作进展", "工业园建设任务安排。", []), plan))

    def test_named_target_stays_required_in_mixed_request(self):
        plan = finalize_intent(Plan("聊城光岳楼的风土人情", targets=["光岳楼", "风土人情"]))
        self.assertEqual(plan["targets"], ["光岳楼"])
        self.assertFalse(article_matches_targets(Article("u", "婚俗博物馆", "传统婚俗展览。", []), plan))
        self.assertTrue(article_matches_targets(Article("u", "光岳楼", "楼前民俗表演。", []), plan))

    def test_topic_search_uses_real_form_with_related_vocabulary(self):
        html = "<form method='get' action='/search.html'><input name='keyword'></form>"
        links = find_links(html, SITE["url"], dict(SITE, search_enabled=True),
                           Plan("聊城的风土人情", themes=["风土人情"]), page_kind="search_entry")
        queries = [parse_qs(urlsplit(link.url).query)["keyword"][0] for link in links]
        self.assertIn("聊城 婚俗", queries)
        self.assertIn("聊城 非遗", queries)
        self.assertTrue(all(urlsplit(link.url).netloc == "example.org" for link in links))
        self.assertTrue(all(urlsplit(link.url).path == "/search.html" for link in links))

    def test_exclusions_apply_to_expanded_theme_vocabulary(self):
        plan = Plan("聊城风土人情，不要美食和小吃", themes=["风土人情"], exclude_terms=["美食", "小吃"])
        self.assertNotIn("地方美食", plan.theme_keywords)
        self.assertNotIn("小吃", plan.theme_keywords)
        self.assertFalse(article_matches_targets(Article("u", "地方小吃", "传统美食介绍。", []), plan))

    def test_search_pagination_separates_topics_but_shares_page_counter(self):
        self.assertEqual(pagination_branch("https://example.org/search.html?keyword=婚俗&page=1"),
                         pagination_branch("https://example.org/search.html?page=2&keyword=婚俗"))
        self.assertNotEqual(pagination_branch("https://example.org/search.html?keyword=婚俗&page=1"),
                            pagination_branch("https://example.org/search.html?keyword=非遗&page=1"))


if __name__ == "__main__":
    unittest.main()

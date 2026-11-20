"""Production format, duration and audience must not narrow material searches."""
import json
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from culture.extract import article_matches_targets, find_links
from culture.intent import finalize_intent, retrieval_term
from culture.model import analyze_request
from culture.types import Article


SITE = {'id': 'fixture', 'name': '离线网站', 'url': 'https://example.org/',
        'hosts': ['example.org'], 'search_enabled': True}
FORM = '<form action="/search.html"><input name="keyword"></form>'


class SearchKeywordTests(unittest.TestCase):
    def test_user_example_cleans_misclassified_model_fields_and_preserves_brief(self):
        raw = '聊城光岳楼视频，面向儿童，时长30s'
        client = Mock()
        client.complete.return_value = json.dumps({
            'city': '聊城', 'targets': ['光岳楼视频', '儿童', '30s'],
            'themes': ['视频', '时长30s', '面向儿童'], 'exclude_terms': []})
        plan = analyze_request(client, raw)
        self.assertEqual(plan['raw_request'], raw)
        self.assertEqual(plan['targets'], ['光岳楼'])
        self.assertEqual(plan['themes'], [])
        self.assertEqual(plan['theme_keywords'], [])
        self.assertEqual(plan['media_type'], 'all')
        links = find_links(FORM, SITE['url'], SITE, plan, page_kind='home')
        queries = {parse_qs(urlsplit(link.url).query)['keyword'][0] for link in links}
        self.assertEqual(queries, {'光岳楼', '聊城 光岳楼'})
        self.assertTrue(article_matches_targets(Article('u', '光岳楼', '古建筑景观介绍。', []), plan))

    def test_common_duration_and_format_variations_have_identical_queries(self):
        for detail in ('视频，时长30s', '短视频，30秒', '纪录片，时长1分钟',
                       '宣传片，时长三十秒', '视频，时长30 S', '短片，片长00:30'):
            with self.subTest(detail=detail):
                plan = finalize_intent({'raw_request': '聊城光岳楼' + detail,
                                        'targets': ['光岳楼' + detail], 'themes': [detail]})
                links = find_links(FORM, SITE['url'], SITE, plan, page_kind='home')
                queries = {parse_qs(urlsplit(link.url).query)['keyword'][0] for link in links}
                self.assertEqual(queries, {'光岳楼', '聊城 光岳楼'})

    def test_heritage_theme_survives_metadata_cleanup(self):
        plan = finalize_intent({'raw_request': '聊城非遗短视频，面向大众，时长60秒',
                                'targets': [], 'themes': ['非遗短视频', '60秒', '大众']})
        self.assertEqual(plan['targets'], [])
        self.assertEqual(plan['themes'], ['非遗'])
        self.assertIn('木版年画', plan['theme_keywords'])
        links = find_links(FORM, SITE['url'], SITE, plan, page_kind='home')
        self.assertTrue(links)
        for link in links:
            query = parse_qs(urlsplit(link.url).query)['keyword'][0]
            self.assertFalse(any(word in query for word in ('视频', '时长', '60', '大众')))

    def test_explicit_media_type_is_a_filter_not_a_search_term(self):
        plan = finalize_intent({'raw_request': '聊城光岳楼，仅视频素材，时长30秒',
                                'targets': ['光岳楼', '视频', '30秒'], 'media_type': 'video'})
        links = find_links(FORM, SITE['url'], SITE, plan, page_kind='home')
        self.assertEqual(plan['media_type'], 'video')
        self.assertEqual({parse_qs(urlsplit(link.url).query)['keyword'][0] for link in links},
                         {'光岳楼', '聊城 光岳楼'})

    def test_subject_names_and_content_exclusions_are_retained(self):
        plan = finalize_intent({'targets': ['儿童乐园', '东昌府木版年画'],
                                'themes': ['传统技艺'], 'exclude_terms': ['施工', '会议', '视频']})
        self.assertEqual(plan['targets'], ['儿童乐园', '东昌府木版年画'])
        self.assertEqual(plan['exclude_terms'], ['施工', '会议'])
        self.assertEqual(retrieval_term('光岳楼30秒视频'), '光岳楼')
        self.assertEqual(retrieval_term('东昌湖1分钟短视频'), '东昌湖')
        self.assertEqual(retrieval_term('关帝庙'), '关帝庙')


if __name__ == '__main__':
    unittest.main()

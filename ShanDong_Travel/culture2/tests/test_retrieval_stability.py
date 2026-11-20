"""Regressions for repeated prompts, theme recall, and transient site failures."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

import config
from culture.collector import collect
from culture.extract import find_links
from culture.intent import finalize_intent, source_media_type
from culture.model import analyze_request
from culture.types import Budget, BudgetExceeded, CollectionError, TransientReadError
from culture.web import Response, WebClient


SITE = {'id': 'fixture', 'name': '离线官网', 'url': 'https://example.org/',
        'hosts': ['example.org'], 'search_enabled': True}


class RequestStabilityTests(unittest.TestCase):
    def test_creation_prompt_keeps_images_regardless_of_model_media_choice(self):
        for raw in ('聊城光岳楼视频，面向儿童，时长30s', '生成一条聊城光岳楼的视频',
                    '给我生成一个关于聊城东昌湖的视频，面向大众', '聊城非遗短视频', '制作聊城文旅宣传片'):
            for model_type in ('all', 'video', 'image'):
                with self.subTest(raw=raw, model_type=model_type):
                    client = Mock()
                    client.complete.return_value = json.dumps({'city': '聊城', 'media_type': model_type})
                    self.assertEqual(analyze_request(client, raw)['media_type'], 'all')

    def test_explicit_source_media_restrictions_and_mixed_sources(self):
        cases = {'聊城光岳楼，仅视频素材': 'video', '只要图片素材': 'image',
                 '搜集聊城光岳楼视频': 'video', '查找聊城非遗照片': 'image',
                 '制作聊城视频，不要图片': 'video', '不要视频，只搜集图片': 'image',
                 '聊城光岳楼图片和视频素材': 'all', '视频和图片素材都需要': 'all',
                 '收集聊城光岳楼图片和视频': 'all'}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(source_media_type(raw), expected)

    def test_missing_or_compound_theme_has_same_retrieval_vocabulary(self):
        plans = [finalize_intent({'raw_request': '聊城非遗', 'city': '聊城', 'targets': targets,
                                  'themes': themes})
                 for targets, themes in (([], []), (['非遗'], []), (['聊城非遗'], []), ([], ['非遗']))]
        self.assertTrue(all(plan['targets'] == [] for plan in plans))
        self.assertTrue(all(plan['theme_keywords'] == plans[0]['theme_keywords'] for plan in plans))
        self.assertIn('木版年画', plans[0]['theme_keywords'])
        self.assertIn('葫芦雕刻', plans[0]['theme_keywords'])

    def test_heritage_queries_include_local_terms_without_mandatory_city_prefix(self):
        form = '<form action="/search.html"><input name="keyword"></form>'
        links = find_links(form, SITE['url'], SITE,
                           {'raw_request': '聊城非遗', 'themes': ['非遗']}, page_kind='home')
        queries = [parse_qs(urlsplit(link.url).query)['keyword'][0] for link in links]
        for word in ('非遗', '非物质文化遗产', '木版年画', '葫芦雕刻', '剪纸', '戏曲'):
            self.assertIn(word, queries)
            self.assertIn('聊城 ' + word, queries)
        self.assertEqual(len(queries), len(set(queries)))
        self.assertLessEqual(len(queries), config.MAX_SEARCH_QUERIES)

    def test_heritage_article_without_literal_heritage_keyword_is_discovered(self):
        html = ('<a href="/20261119/wood.html">东昌府木版年画制作</a>'
                '<a href="/20261119/gourd.html">东昌雕刻葫芦技艺</a>'
                '<a href="/20261119/industry.html">工业项目推进</a>')
        links = find_links(html, SITE['url'], SITE,
                           {'raw_request': '聊城非遗', 'themes': ['非遗']}, page_kind='home')
        self.assertEqual({link.title for link in links}, {'东昌府木版年画制作', '东昌雕刻葫芦技艺'})

    def test_news_real_get_search_form_is_enabled(self):
        site = next(site for site in config.OFFICIAL_SITES if site['id'] == 'news')
        html = '''<form action="https://lcadmin.lcxw.cn/index.php?m=search&amp;c=index" method="get">
                  <input name="q"><input type="hidden" name="a" value="init">
                  <input type="hidden" name="siteid" value="1"></form>'''
        links = find_links(html, site['url'], site,
                           {'raw_request': '给我生成一个关于聊城东昌湖的视频，面向大众',
                            'city': '聊城', 'targets': ['东昌湖']}, page_kind='home')
        self.assertTrue(links)
        for link in links:
            self.assertEqual(link.kind, 'search')
            self.assertEqual(urlsplit(link.url).hostname, 'lcadmin.lcxw.cn')
            params = parse_qs(urlsplit(link.url).query)
            self.assertEqual(params['siteid'], ['1'])
            self.assertEqual(params['a'], ['init'])
        self.assertIn('东昌湖', [parse_qs(urlsplit(link.url).query)['q'][0] for link in links])

    def test_user_lake_video_prompt_keeps_images_and_video(self):
        prompt = '给我生成一个关于聊城东昌湖的视频，面向大众'
        article_url = SITE['url'] + '20261119/lake.html'
        pages = {SITE['url']: '<a href="/20261119/lake.html">东昌湖</a>',
                 article_url: '<h1>东昌湖</h1><div class="article-content"><p>东昌湖景观。</p>'
                              '<img src="/lake1.jpg"><img src="/lake2.jpg"><img src="/lake3.jpg">'
                              '<img src="/lake4.jpg"><video src="/lake.mp4"></video></div>'}
        web = Mock()
        web.html.side_effect = lambda url, **kwargs: (url, pages[url])

        def download(url, path, kind, referer):
            Path(path).write_bytes(b'offline media fixture')
            return Response(url, b'', 'image/jpeg' if kind == 'image' else 'video/mp4', None)

        web.download.side_effect = download

        def complete(system, content, **kwargs):
            if 'request' in json.loads(content):
                return json.dumps({'city': '聊城', 'targets': ['东昌湖'], 'media_type': 'video'})
            return json.dumps({'description': '古建筑楼阁。', 'decision': 'keep', 'reason': '符合需求',
                               'requirement_status': 'match', 'requirement_evidence': '东昌湖',
                               'is_museum_display': False, 'uncertainties': []})

        model = Mock()
        model.complete.side_effect = complete
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(config, 'OUTPUT_DIR', Path(directory) / 'delivery'), \
             patch.object(config, 'OFFICIAL_SITES', [dict(SITE, search_enabled=False)]), \
             patch('culture.collector.WebClient', return_value=web), \
             patch('culture.collector.ModelClient', return_value=model), \
             patch('culture.collector._check_media'):
            report = collect(prompt, progress=lambda _: None)
        self.assertEqual(report['approved_counts'], {'total': 5, 'image': 4, 'video': 1}, report)


class TransientWebTests(unittest.TestCase):
    def setUp(self):
        self.host = patch('culture.web.public_host')
        self.host.start()
        self.addCleanup(self.host.stop)
        self.session_patch = patch('culture.web.requests.Session')
        self.session = self.session_patch.start().return_value
        self.addCleanup(self.session_patch.stop)
        self.sleep = patch('culture.web.time.sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.messages = []
        self.web = WebClient(Budget(), progress=self.messages.append)
        self.addCleanup(self.web.close)

    def response(self, code=200, body=b'', content_type='text/plain'):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = code
        response.is_redirect = response.is_permanent_redirect = False
        response.headers = {'Content-Type': content_type}
        response.encoding = 'utf-8'
        response.iter_content.return_value = [body]
        return response

    def test_temporary_rules_failure_recovers_instead_of_blocking_entire_site(self):
        self.session.get.side_effect = [self.response(503), self.response(body=b'User-agent: *\nDisallow:\n'),
                                       self.response(body=b'<h1>Article</h1>', content_type='text/html')]
        _, html = self.web.html(SITE['url'])
        self.assertIn('Article', html)
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.web.budget.requests, 3)

    def test_page_timeout_recovers_once(self):
        self.web.robots[SITE['url'].rstrip('/')] = None
        self.session.get.side_effect = [requests.ReadTimeout(), self.response(body=b'ok')]
        self.assertEqual(self.web.html(SITE['url'])[1], 'ok')
        self.assertEqual(self.session.get.call_count, 2)

    def test_repeated_page_failure_stops_after_two_attempts(self):
        self.web.robots[SITE['url'].rstrip('/')] = None
        self.session.get.side_effect = requests.ConnectionError()
        with self.assertRaises(TransientReadError):
            self.web.html(SITE['url'])
        self.assertEqual(self.session.get.call_count, 2)

    def test_denied_pages_are_not_retried(self):
        self.web.robots[SITE['url'].rstrip('/')] = None
        for status in (401, 403, 404, 429):
            with self.subTest(status=status):
                self.session.get.reset_mock()
                self.session.get.side_effect = [self.response(status)]
                with self.assertRaises(CollectionError):
                    self.web.html(SITE['url'])
                self.assertEqual(self.session.get.call_count, 1)

    def test_retry_remains_bounded_by_global_request_budget(self):
        self.web.robots[SITE['url'].rstrip('/')] = None
        self.web.budget.max_requests = 1
        self.session.get.side_effect = requests.ReadTimeout()
        with self.assertRaises(BudgetExceeded):
            self.web.html(SITE['url'])
        self.assertEqual(self.session.get.call_count, 1)


if __name__ == '__main__':
    unittest.main()

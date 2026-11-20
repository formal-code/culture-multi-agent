import unittest
from culture.extract import extract_article, find_links, article_matches_targets
from culture.types import Plan, Article


SITE = {"id": "news", "name": "example", "url": "https://news.example/",
        "hosts": ["news.example"], "search_enabled": False}


class ExtractionTests(unittest.TestCase):
    def test_poster_image_sources_are_not_video_candidates(self):
        html = """<h1>东昌湖视频</h1><div class='article-content'><p>东昌湖文旅视频介绍。</p>
        <video src='/cover.PNG?token=fixture'></video>
        <video><source src='/poster.jpg'><source src='/clip.mp4' type='video/mp4'></video>
        <video><source src='/image-endpoint' type='image/jpeg'></video>
        <embed src='/another.webp'><a href='/cover.gif'>封面</a>
        <video src='/stream-endpoint' type='video/mp4'></video>
        <script>const clip = '/script-clip.mp4';</script></div>"""
        article = extract_article(html, "https://news.example/20261119/lake.html")
        videos = [item.url for item in article.media if item.media_type == "video"]
        self.assertEqual(set(videos), {"https://news.example/clip.mp4",
            "https://news.example/stream-endpoint", "https://news.example/script-clip.mp4"})

    def test_column_next_page_and_numeric_page_are_not_lost_to_keyword_filter(self):
        html = """<div class='pagination'><a href='index_2.html'>2</a>
        <a rel='next' href='index_3.html'>下一页</a></div>
        <a href='/unrelated/next.html'>下一页</a>"""
        links = find_links(html, "https://news.example/tourism/", SITE,
                           Plan("聊城光岳楼", targets=["光岳楼"]), page_kind="column")
        pages = [link for link in links if link.kind == "pagination"]
        self.assertEqual({x.url for x in pages}, {"https://news.example/tourism/index_2.html",
                                                  "https://news.example/tourism/index_3.html"})

    def test_article_next_page_label_not_used_to_expand_unrelated_pages(self):
        html = "<a href='/article/other.html' rel='next'>下一页</a>"
        links = find_links(html, "https://news.example/article/tower.html", SITE,
                           Plan("聊城光岳楼", targets=["光岳楼"]), page_kind="article")
        self.assertFalse(links)

    def test_search_entry_form_preserves_hidden_fields_and_expands_target_queries(self):
        from urllib.parse import parse_qs, urlsplit
        site = dict(SITE, search_enabled=True)
        html = """<form action='/search.html' method='get'><input name='keyword'>
        <input type='hidden' name='channel' value='tourism'></form>"""
        links = find_links(html, "https://news.example/search.html", site,
            Plan("聊城东昌府区光岳楼和东昌湖", districts=["东昌府区"], targets=["光岳楼", "东昌湖"]),
            page_kind="search_entry")
        self.assertEqual(len(links), 6)
        queries = [parse_qs(urlsplit(x.url).query) for x in links]
        self.assertTrue(all(q["channel"] == ["tourism"] for q in queries))
        self.assertIn("东昌府区 光岳楼", [q["keyword"][0] for q in queries])
        self.assertIn("聊城 东昌湖", [q["keyword"][0] for q in queries])

    def test_pagination_search_does_not_regenerate_initial_search_forms(self):
        html = """<form action='/search.html'><input name='keyword'></form>
        <a href='/search.html?keyword=光岳楼&amp;page=2' rel='next'>下一页</a>"""
        links = find_links(html, "https://news.example/search.html?keyword=光岳楼", dict(SITE, search_enabled=True),
                           Plan("聊城光岳楼", targets=["光岳楼"]), page_kind="search")
        self.assertEqual([x.kind for x in links], ["pagination"])

    def test_unrelated_article_not_downloaded_because_of_website_breadcrumb(self):
        article = Article("https://news.example/museum.html", "聊城市文化和旅游局 - 旅游景区 - 婚俗博物馆",
                          "展厅陈列婚俗与契约文物。", [])
        self.assertFalse(article_matches_targets(article, Plan("聊城东昌湖", targets=["东昌湖"])))
        article.text = "本文介绍东昌湖湖畔的夜游路线。"
        self.assertTrue(article_matches_targets(article, Plan("聊城东昌湖", targets=["东昌湖"])))

    def test_newly_discovered_news_pages_use_configured_https_without_changing_media_urls(self):
        site = dict(SITE, prefer_https=True)
        links = find_links("<a href='http://news.example/20261119/tower.html'>光岳楼</a>",
                           site["url"], site, Plan("聊城光岳楼", targets=["光岳楼"]))
        self.assertEqual(links[0].url, "https://news.example/20261119/tower.html")

    def test_news_article_body_and_print_icon(self):
        html = """<h1>光岳楼高精度数字档案即将建成</h1>
        <div class='wenzhang'><p>本文介绍聊城光岳楼高精度数字档案，记录城楼的结构和风貌。</p>
        <img src='/tower.jpg' alt='光岳楼'><img src='/static/imgs/dayinji.png'></div>
        <aside><img src='/recommended.jpg'></aside>"""
        article = extract_article(html, "https://news.example/20261119/tower.html")
        self.assertTrue(article.is_article)
        self.assertEqual(article.body_selector, ".wenzhang")
        self.assertEqual([m.url for m in article.media], ["https://news.example/tower.jpg"])

    def test_recommendation_image_is_not_article_media(self):
        html = """<html><head><title>光岳楼夜景</title></head><body>
        <nav><img src='/static/logo.png'></nav><main><article><h1>光岳楼夜景</h1>
        <time datetime='2026-11-18'></time><div class='article-content'>
        <p>聊城光岳楼在夜间灯光下展现古建筑的层叠屋檐与城楼轮廓。</p>
        <figure><img src='/media/tower.jpg' alt='光岳楼夜景'><figcaption>光岳楼夜景</figcaption></figure>
        </div></article><aside class='recommend'><img src='/media/unrelated.jpg'></aside></main>
        </body></html>"""
        article = extract_article(html, "https://news.example/20261118/tower.html")
        self.assertTrue(article.is_article)
        self.assertEqual([m.url for m in article.media], ["https://news.example/media/tower.jpg"])

    def test_irrelevant_title_not_promoted_by_neighboring_context(self):
        html = """<a href='/20261118/school.html' title='东昌府区新城小学开展阅读展示课'>学校新闻</a>
        <a href='/20261118/tower.html' title='光岳楼夜景文旅'>光岳楼夜景</a>"""
        plan = Plan("聊城东昌府区光岳楼文旅", districts=["东昌府区"], targets=["光岳楼"], themes=["文旅"])
        links = find_links(html, "https://news.example/", SITE, plan)
        self.assertEqual([x.title for x in links], ["光岳楼夜景文旅"])

    def test_homepage_discovery_accepts_serialized_request_plan(self):
        html = """<a href='/column/tourism/' title='文旅栏目'>文旅</a>
        <a href='/20261118/tower.html' title='聊城光岳楼夜景'>光岳楼</a>
        <a href='/20261118/school.html' title='东昌府区新城小学开展教学活动'>学校新闻</a>"""
        plan = {"raw_request": "聊城东昌府区光岳楼和东昌湖", "city": "聊城",
                "districts": ["东昌府区"], "targets": ["光岳楼", "东昌湖"],
                "themes": [], "exclude_terms": [], "media_type": "all"}
        links = find_links(html, "https://news.example/", SITE, plan)
        self.assertEqual({x.title for x in links}, {"文旅栏目", "聊城光岳楼夜景"})


if __name__ == "__main__":
    unittest.main()

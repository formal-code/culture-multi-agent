import unittest
from culture.ranking import SiteQueue, article_score, worth_opening
from culture.types import Candidate, Plan


class RankingTests(unittest.TestCase):
    def test_publisher_and_channel_words_do_not_inflate_article_score(self):
        plan = Plan("聊城光岳楼", targets=["光岳楼"])
        self.assertEqual(article_score("聊城市文化和旅游局 - 旅游景区 - 婚俗博物馆", plan)[0],
                         article_score("婚俗博物馆", plan)[0])

    def setUp(self):
        self.plan = Plan("聊城东昌府区的光岳楼和东昌湖相关文旅视频", districts=["东昌府区"],
                         targets=["光岳楼", "东昌湖"], themes=["文旅", "视频"], media_type="video")

    def test_explicit_landmark_outranks_district_only_news(self):
        landmark = article_score("光岳楼夜景与古城文旅", self.plan)[0]
        district_news = article_score("东昌府区新城小学开展阅读教学活动", self.plan)[0]
        self.assertGreater(landmark, district_news)
        self.assertFalse(worth_opening("东昌府区新城小学开展阅读教学活动", self.plan)[0])

    def test_excluded_item_never_opens(self):
        plan = Plan("聊城光岳楼旅游", targets=["光岳楼"], exclude_terms=["施工"])
        self.assertFalse(worth_opening("光岳楼景区施工公告", plan)[0])

    def test_site_rotation_and_rank_order(self):
        q = SiteQueue(["a", "b"], batch=2)
        q.add(Candidate("https://a.example/home", "home", "a", "home", 0))
        q.add(Candidate("https://a.example/low", "low", "a", "article", 3))
        q.add(Candidate("https://a.example/high", "high", "a", "article", 20))
        q.add(Candidate("https://b.example/one", "one", "b", "article", 1))
        self.assertEqual(q.pop().title, "high")
        self.assertEqual(q.pop().title, "low")
        self.assertEqual(q.pop().site_id, "b")


if __name__ == "__main__":
    unittest.main()

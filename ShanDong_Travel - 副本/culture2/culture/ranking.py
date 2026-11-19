"""按文章自己的标题评分；不把整页菜单文字算到每条链接上。"""
from collections import defaultdict
import re
from urllib.parse import urlsplit, urlunsplit

from config import NEWS_NOISE, TOURISM_TERMS
from culture.types import Candidate
from culture.intent import finalize_intent


def canonical_url(url):
    p = urlsplit(url)
    # 保留查询参数，避免丢失站内搜索和媒体签名。
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def page_key(url):
    """只对页面去重时合并 HTTP/HTTPS；媒体签名不经过本函数。"""
    p = urlsplit(canonical_url(url))
    return (p.hostname, p.path.rstrip("/"), p.query)


def content_title(title):
    """Exclude site and breadcrumb labels from title-based relevance."""
    parts = re.split(r"\s+[-|_>›»]\s+", str(title or "").strip())
    publisher = r"新闻网|人民政府|文化和旅游局|文旅局|官方网站"
    if len(parts) > 1 and re.search(publisher, parts[0]):
        return parts[-1]
    if len(parts) > 1 and re.search(publisher, parts[-1]):
        return " - ".join(parts[:-1])
    return str(title or "").strip()


def article_score(title, plan):
    plan = finalize_intent(plan)
    get = plan.get
    title = re.sub(r"\s+", "", content_title(title))
    if any(x in title for x in get("exclude_terms")):
        return -1000, "命中用户排除项"
    targets = [x for x in get("targets") if x in title]
    themes = [x for x in get("themes") if x in title]
    related_themes = [x for x in get("theme_keywords") if x in title]
    tourism = [x for x in TOURISM_TERMS if x in title]
    aliases = get("district_aliases", None)
    if aliases is None:
        aliases = [s[:-1] for s in get("districts") if len(s) > 2 and s.endswith(("区", "县", "市"))]
    districts = [x for x in (*get("districts"), *aliases) if x in title]
    noise = [x for x in NEWS_NOISE if x in title]
    # 对象命中明显高于区县命中；区县自身不会让无关新闻合格。
    score = 100 * len(targets) + 18 * len(themes) + min(32, 8 * len(related_themes)) + min(24, 6 * len(tourism))
    score += 4 if districts else 0
    score -= 35 * len(noise)
    if get("targets") and not targets:
        score -= 12
    if get("themes") and not get("targets") and not related_themes:
        score -= 30
    reason = "；".join(filter(None, ["对象：" + "、".join(targets) if targets else "",
        "主题：" + "、".join(themes or related_themes or tourism) if themes or related_themes or tourism else "",
        "区县辅助命中" if districts else "", "新闻事务降权" if noise else ""]))
    return score, reason or "缺少明确文旅线索"


def worth_opening(title, plan):
    score, reason = article_score(title, plan)
    # 不以区县两个字就打开学校、会议和政务新闻。
    return score > 0, score, reason


class SiteQueue:
    """每站连取最多 batch 页；站内先比分数，再比文章/栏目优先级。"""
    def __init__(self, site_ids, batch=5):
        self.order = list(site_ids)
        self.batch = batch
        self.index = 0
        self.used = 0
        self.pending = defaultdict(dict)
        self.visited = set()

    def add(self, candidate):
        key = page_key(candidate.url)
        if key in self.visited or candidate.site_id not in self.order:
            return
        old = self.pending[candidate.site_id].get(key)
        if old is None or candidate.score > old.score:
            self.pending[candidate.site_id][key] = candidate

    def pop(self):
        for _ in range(len(self.order) + 1):
            sid = self.order[self.index]
            if self.used >= self.batch or not self.pending[sid]:
                self.index = (self.index + 1) % len(self.order)
                self.used = 0
                continue
            kind_order = {"article": 3, "column": 2, "pagination": 2,
                          "search": 1, "search_entry": 1, "home": 0}
            candidate = max(self.pending[sid].values(), key=lambda x: (
                x.score, kind_order.get(x.kind, 0), -x.depth))
            key = page_key(candidate.url)
            del self.pending[sid][key]
            self.visited.add(key)
            self.used += 1
            return candidate
        return None

    def __len__(self):
        return sum(len(v) for v in self.pending.values())

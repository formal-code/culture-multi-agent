"""发现真实链接并定位正文；不推测下载地址，不把推荐图当作文章插图。"""
import re
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit, parse_qsl

from bs4 import BeautifulSoup

import config
from config import TOURISM_TERMS
from culture.ranking import canonical_url, worth_opening, content_title
from culture.types import Article, Candidate, Media, Plan
from culture.intent import finalize_intent

BODY_SELECTORS = (
    ".wenzhang", ".article-body", ".TRS_Editor", "#zoom", "#zoomcon", "#articleContent", "#article-content",
    ".article-content", ".article_content", "#article_content", ".article-cont", ".news_content", ".news-con",
    ".news-content", ".detail-content", ".content-article", ".content_main",
    "[itemprop=articleBody]", "article", "main", ".content", "#content",
)
NOISE_SELECTORS = (
    "nav,header,footer,aside,.nav,.footer,.header,.related,.recommend,"
    ".related-news,.recommend-list,.share,.copyright,.breadcrumb,.advertisement,"
    ".bdsharebuttonbox,.pagination,script,style,noscript"
)
SKIP_SUFFIX = re.compile(r"\.(?:jpg|jpeg|png|webp|gif|svg|pdf|docx?|xlsx?|zip|css|js|mp4|m3u8|mp3)(?:$|\?)", re.I)
ARTICLE_PATH = re.compile(r"(?:doc_[^/]+|/\d{6,8}/[^/]+|\.(?:s?html?))$", re.I)
SEARCH_FIELDS = {"q", "query", "keyword", "keywords", "searchword", "searchtext", "wd", "key"}


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def real_url(base, value):
    if not isinstance(value, str):
        return ""
    value = value.strip().replace("\\/", "/").replace("&amp;", "&")
    if not value or value.startswith(("#", "data:", "javascript:", "blob:", "mailto:", "tel:")):
        return ""
    url = urljoin(base, value)
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        return ""
    return canonical_url(url)


def normalize_plan(plan):
    # Request analysis currently returns a JSON-shaped dict for report serialization.
    # Normalize it at this boundary because the discovery helpers use Plan attributes.
    plan = finalize_intent(plan)
    return Plan(raw_request=plan.get("raw_request", ""), city=plan.get("city", "聊城"),
                districts=plan.get("districts", []), targets=plan.get("targets", []),
                themes=plan.get("themes", []), exclude_terms=plan.get("exclude_terms", []),
                media_type=plan.get("media_type", "all"))


def pagination_branch(url):
    """Keep each column/search chain's unproductive pagination count separate."""
    p = urlsplit(url)
    path = p.path if p.path.endswith("/") else p.path.rsplit("/", 1)[0] + "/"
    if "search" in p.path.lower():
        path = p.path
    query = [(key, value) for key, value in parse_qsl(p.query, keep_blank_values=True)
             if key.lower() not in {"page", "p", "pageno", "pagenum", "pageindex", "offset"}]
    return urlunsplit(("", p.netloc, path, urlencode(sorted(query)), ""))


def official_page_url(base, value, site):
    url = real_url(base, value)
    if not url or urlsplit(url).hostname not in site["hosts"]:
        return ""
    # Canonicalize newly found pages to the configured website scheme before first use.
    # This never retries denied URLs via an alternate scheme or changes media URLs.
    if site.get("prefer_https"):
        p = urlsplit(url)
        url = urlunsplit(("https", p.netloc, p.path, p.query, ""))
    return url


def is_article_url(url):
    path = urlsplit(url).path
    if re.fullmatch(r'/(?:list|category|channel)/\d+(?:/\d+)?\.(?:s?html?)', path, re.I):
        return False
    filename = path.rsplit("/", 1)[-1]
    return bool(ARTICLE_PATH.search(path)) and not re.match(
        r"^(?:index|list|search|channel)(?:[_-]\d+)?\.(?:s?html?)$", filename, re.I)


def pagination_link(link, title, url, page_url, page_kind):
    """Accept only real pagination URLs within the current list/search path."""
    if page_kind in {"home", "article", "search_entry"}:
        return False
    if page_kind is None and (urlsplit(page_url).path == "/" or is_article_url(page_url)):
        return False
    current, target = urlsplit(page_url), urlsplit(url)
    if current.hostname != target.hostname or current.path == "/":
        return False
    if (current.path, current.query) == (target.path, target.query):
        return False
    parent = current.path if current.path.endswith("/") else current.path.rsplit("/", 1)[0] + "/"
    if target.path != current.path and (parent == "/" or not target.path.startswith(parent)):
        return False
    label = re.sub(r"[\s<>›»→·\[\]]", "", title)
    rel_next = "next" in [str(x).lower() for x in link.get("rel", [])]
    next_label = label.lower() in {"下一页", "下页", "后页", "next"}
    numeric = bool(re.fullmatch(r"\d{1,4}", label))
    pager = link.find_parent(class_=re.compile(r"pag(?:e|ination)|fenye|turn", re.I))
    return rel_next or next_label or (numeric and pager is not None)


def find_links(html, page_url, site, plan, depth=0, *, page_kind=None):
    plan = normalize_plan(plan)
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for link in soup.select("a[href], a[data-href], a[data-url], button[data-href], button[data-url]"):
        url = official_page_url(page_url, link.get("href"), site)
        if not url:
            url = official_page_url(page_url, link.get("data-href") or link.get("data-url"), site)
        if not url or SKIP_SUFFIX.search(urlsplit(url).path):
            continue
        title = clean(link.get("title") or link.get_text(" ", strip=True))
        if not title:
            img = link.find("img")
            title = clean(img.get("alt", "")) if img else ""
        if pagination_link(link, title, url, page_url, page_kind):
            found[url] = Candidate(url, "栏目/搜索翻页：" + (title or "下一页"), site["id"],
                                   "pagination", 8, depth + 1, "页面实际提供的翻页链接",
                                   branch=pagination_branch(page_url))
            continue
        if len(title) < 2 or len(title) > 220:
            continue
        if site.get("search_enabled", True) and title in {"站内搜索", "搜索", "高级搜索", "全文搜索"}:
            found[url] = Candidate(url, title, site["id"], "search_entry", 9, depth + 1,
                                   "页面中的真实站内搜索入口")
            continue
        is_article = is_article_url(url)
        ok, score, reason = worth_opening(title, plan)
        if is_article and not ok:
            continue
        if not is_article:
            # 栏目只进入带主题/对象/区县或视频线索的入口，不穷举网站。
            hints = (*plan.targets, *plan.themes, *plan.theme_keywords, *plan.districts, *plan.district_aliases,
                     *TOURISM_TERMS, "视频", "视听", "影像", "全景")
            if not any(t in title for t in hints):
                continue
            score = max(score, 2)
            if plan.media_type == "video" and any(t in title for t in ("视频", "视听", "影像")):
                score += 20
            reason = reason if score > 2 else "文旅或视频栏目入口"
        candidate = Candidate(url, title, site["id"], "article" if is_article else "column",
                              score, depth + 1, reason)
        if url not in found or candidate.score > found[url].score:
            found[url] = candidate
    # 站内查询只使用页面实际提供的 GET 表单、字段和隐藏参数。
    if site.get("search_enabled", True) and page_kind not in {"search", "pagination"}:
        found.update({c.url: c for c in search_links(soup, page_url, site, plan, depth)})
    return sorted(found.values(), key=lambda c: (c.score, c.kind == "article"), reverse=True)[:config.MAX_LINKS_PER_PAGE]


def search_links(soup, page_url, site, plan, depth):
    plan = normalize_plan(plan)
    results = []
    queries = list(plan.targets)
    for target in plan.targets:
        if plan.districts:
            queries.append(plan.districts[0] + " " + target)
        queries.append(plan.city + " " + target)
    if not queries:
        prefix = plan.districts[:1] or [plan.city]
        terms = (plan.theme_keywords or ["文旅"])[:max(1, config.MAX_SEARCH_QUERIES // 2)]
        # Local-site articles often omit the city name; search both forms.
        queries = [query for term in terms for query in (term, " ".join([*prefix, term]))]
    queries = list(dict.fromkeys(queries))[:config.MAX_SEARCH_QUERIES]
    for form in soup.find_all("form"):
        if form.get("method", "get").lower() != "get":
            continue
        action = official_page_url(page_url, form.get("action") or page_url, site)
        if not action:
            continue
        field = next((x for x in form.select("input[name]")
                      if x.get("name", "").lower() in SEARCH_FIELDS), None)
        if field is None:
            continue
        hidden = [(x["name"], x.get("value", "")) for x in form.select('input[type="hidden"][name]')]
        for query in queries:
            p = urlsplit(action)
            params = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) + hidden
                      if k != field["name"]]
            params.append((field["name"], query))
            url = urlunsplit((p.scheme, p.netloc, p.path, urlencode(params), ""))
            results.append(Candidate(url, "站内搜索：" + query, site["id"], "search", 10,
                                     depth + 1, "页面提供的 GET 搜索表单"))
    return results[:config.MAX_SEARCH_QUERIES]


def _body_metrics(node):
    clone = BeautifulSoup(str(node), "html.parser")
    for tag in clone.select(NOISE_SELECTORS):
        tag.decompose()
    text = clean(clone.get_text(" ", strip=True))
    linked = sum(len(clean(a.get_text())) for a in clone.find_all("a"))
    density = linked / max(1, len(text))
    paragraphs = sum(len(clean(p.get_text())) >= 25 for p in clone.find_all("p"))
    return clone, text, density, paragraphs


def extract_article(html, url):
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1") or soup.select_one('#xl-headline h2, .post-title')
    title = clean(heading.get_text(" ")) if heading else ""
    if not title:
        meta = soup.find("meta", attrs={"property": "og:title"})
        title = clean(meta.get("content")) if meta else clean(soup.title.get_text() if soup.title else "")
    published = ""
    for tag in soup.select('meta[name="PubDate"],meta[name="pubdate"],meta[name="publishdate"],'
                           'meta[property="article:published_time"],time[datetime]'):
        published = clean(tag.get("content") or tag.get("datetime"))
        if published:
            break
    # 选择紧凑正文，避免包含站点导航的宽泛 content 容器。
    candidates = []
    for selector in BODY_SELECTORS:
        for node in soup.select(selector):
            clone, text, density, paragraphs = _body_metrics(node)
            if density > 0.35:
                continue
            media_count = len(clone.select("img,video,iframe,embed,object"))
            explicit = selector not in {"article", "main", ".content", "#content"}
            if len(text) < 50 and not (explicit and media_count):
                continue
            score = min(len(text), 3000) + paragraphs * 100 + (2500 if explicit else 0) - density * 6000
            candidates.append((score, selector, node, clone, text))
    if not candidates:
        return Article(url, title, "", [], published)
    _, selector, original, body, text = max(candidates, key=lambda row: row[0])
    # 正文选择器也可能套在栏目上：要求文章标题/文章URL/时间证据。
    article_path = is_article_url(url)
    is_article = bool(article_path or (heading and (published or selector == "article")))
    if not is_article:
        return Article(url, title, text, [], published, selector, False)
    media = extract_media(body, original, url)
    return Article(url, title, text, media, published, selector, True)


def article_matches_targets(article, plan):
    """Avoid model calls for media from articles with no evidence of the requested target."""
    plan = normalize_plan(plan)
    text = " ".join([content_title(article.title), article.text, *(m.caption for m in article.media)])
    if any(term in text for term in plan.exclude_terms):
        return False
    if plan.targets:
        return any(target in text for target in plan.targets)
    if plan.theme_keywords:
        return any(term in text for term in plan.theme_keywords)
    return True


def extract_media(body, original, url):
    results = {}
    for img in body.select("img"):
        src = ""
        for attr in ("data-original", "data-src", "data-actualsrc", "data-lazy-src", "src"):
            src = real_url(url, img.get(attr))
            if src:
                break
        srcset = img.get("data-srcset") or img.get("srcset")
        if srcset and not any(img.get(a) for a in ("data-original", "data-src", "data-actualsrc")):
            choices = []
            for entry in srcset.split(","):
                pieces = entry.strip().split()
                if pieces:
                    candidate = real_url(url, pieces[0])
                    size = float(re.sub(r"[^\d.]", "", pieces[-1]) or 1) if len(pieces) > 1 else 1
                    if candidate:
                        choices.append((size, candidate))
            if choices:
                src = max(choices)[1]
        if not src or urlsplit(src).path.lower().endswith((".svg", ".ico")):
            continue
        caption = clean(img.get("alt") or img.get("title"))
        figure = img.find_parent("figure")
        if figure and figure.find("figcaption"):
            caption = clean(figure.find("figcaption").get_text(" "))
        if not caption and img.parent and img.parent.name == "p":
            sibling = img.parent.find_next_sibling("p")
            if sibling:
                line = clean(sibling.get_text(" "))
                if len(line) <= 120:
                    caption = line
        if any(word in src.lower() for word in ("/logo", "icon.", "qrcode", "weixin", "banner-bg", "dayinji")):
            continue
        results[src] = Media(src, "image", caption[:300], "正文 " + img.name)
    for node in body.select("video[src],video source[src],a[href],embed[src],object[data]"):
        src = real_url(url, node.get("src") or node.get("href") or node.get("data"))
        if not src:
            continue
        # Some players put a poster image in src; it is not a video candidate.
        if re.search(r"\.(?:jpe?g|png|webp|gif|svg|ico|avif|bmp)$", urlsplit(src).path, re.I):
            continue
        if str(node.get("type", "")).lower().startswith("image/"):
            continue
        if node.name == "a" and not re.search(r"\.(?:mp4|webm|mov|m3u8)(?:\?|$)", src, re.I):
            continue
        if node.name in {"embed", "object"} and not re.search(r"\.(?:mp4|webm|mov|m3u8)(?:\?|$)", src, re.I):
            continue
        caption = clean(node.get("title") or node.get_text(" "))[:300]
        results[src] = Media(src, "video", caption, "正文播放器/附件")
    # 播放器地址常存在正文内脚本中，只解析明确的真实媒体字符串。
    for script in original.find_all("script"):
        content = script.get_text().replace("\\/", "/").replace("\\u0026", "&")
        for match in re.finditer(r'''["']([^"'<>\s]+\.(?:mp4|webm|mov|m3u8)(?:\?[^"'<>\s]*)?)["']''', content, re.I):
            src = real_url(url, match.group(1))
            if src:
                results[src] = Media(src, "video", "", "正文播放器脚本")
    return list(results.values())

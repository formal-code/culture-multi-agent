"""共享记录和明确的运行额度。"""
from dataclasses import dataclass, field
import time


class CollectionError(Exception):
    """单个页面或素材失败，可以继续其他候选。"""


class TransientReadError(CollectionError):
    """公开网页或规则文件的临时网络错误，可以在预算内重读一次。"""


class BudgetExceeded(CollectionError):
    """本轮总额度耗尽。"""


class ModelUnavailable(CollectionError):
    """密钥、模型权限或额度异常，停止后续付费调用。"""


@dataclass
class Plan:
    raw_request: str
    city: str = "聊城"
    districts: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    media_type: str = "all"

    @property
    def district_aliases(self):
        return [s[:-1] for s in self.districts if len(s) > 2 and s.endswith(("区", "县", "市"))]

    @property
    def theme_keywords(self):
        from culture.intent import finalize_intent
        return finalize_intent(self)["theme_keywords"]


@dataclass
class Candidate:
    url: str
    title: str
    site_id: str
    kind: str = "article"
    score: int = 0
    depth: int = 0
    reason: str = ""
    branch: str = ""


@dataclass
class Media:
    url: str
    media_type: str
    caption: str = ""
    locator: str = ""


@dataclass
class Article:
    url: str
    title: str
    text: str
    media: list[Media]
    published_at: str = ""
    body_selector: str = ""
    is_article: bool = False


@dataclass
class Budget:
    max_pages: int = 280
    max_candidates: int = 500
    max_calls: int = 501
    max_requests: int = 1800
    max_seconds: float = 5400
    max_download_bytes: int = 15 * 1024 ** 3
    download_bytes: int = 0
    pages: int = 0
    candidates: int = 0
    calls: int = 0
    requests: int = 0
    started: float = field(default_factory=time.monotonic)

    def check(self):
        if time.monotonic() - self.started >= self.max_seconds:
            raise BudgetExceeded("达到本轮采集时长上限")

    def take(self, kind):
        self.check()
        limit = {"pages": self.max_pages, "candidates": self.max_candidates,
                 "calls": self.max_calls, "requests": self.max_requests}[kind]
        if getattr(self, kind) >= limit:
            raise BudgetExceeded(f"达到{kind}总额度 {limit}")
        setattr(self, kind, getattr(self, kind) + 1)

    def take_download_bytes(self, size):
        self.check()
        if self.download_bytes + size > self.max_download_bytes:
            raise BudgetExceeded("达到本轮媒体下载总量上限")
        self.download_bytes += size

    @property
    def remaining_seconds(self):
        return max(0.1, self.max_seconds - (time.monotonic() - self.started))

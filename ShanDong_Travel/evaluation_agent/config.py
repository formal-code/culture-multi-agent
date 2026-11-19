"""Configuration only; importing this module makes no network requests."""
import os
from dataclasses import dataclass
from pathlib import Path


def load_env(path: Path, *, override: bool = False) -> None:
    """Read a small .env file without expansion or execution."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name.replace("_", "a").isalnum():
            raise ValueError(".env 格式错误，请使用 KEY=value")
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or name not in os.environ:
            os.environ[name] = value


def _integer(name: str, default: int, low: int, high: int) -> int:
    number = int(os.getenv(name, str(default)))
    if not low <= number <= high:
        raise ValueError(f"{name} 必须在 {low} 到 {high} 之间")
    return number


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    enable_thinking: bool
    llm_timeout: int
    llm_max_tokens: int
    max_llm_calls: int
    search_provider: str
    search_fallback_provider: str
    tavily_api_key: str
    max_queries: int
    results_per_query: int
    max_pages: int
    request_interval: float
    http_timeout: int
    trend_provider: str
    trend_api_url: str
    trend_api_key: str
    trend_max_age_hours: int
    research_window_days: int
    search_max_tool_calls: int
    research_date: str

    @classmethod
    def from_env(cls) -> "Settings":
        interval = float(os.getenv("REQUEST_INTERVAL_SECONDS", "1"))
        if not 0 <= interval <= 10:
            raise ValueError("REQUEST_INTERVAL_SECONDS 必须在 0 到 10 之间")
        search_provider = os.getenv("SEARCH_PROVIDER", "bailian_responses")
        fallback_provider = os.getenv("SEARCH_FALLBACK_PROVIDER", "auto")
        providers = {"bailian_responses", "tavily", "bing_rss"}
        if search_provider not in providers:
            raise ValueError("SEARCH_PROVIDER 仅支持 bailian_responses、tavily 或 bing_rss")
        if fallback_provider not in providers | {"auto", "none"}:
            raise ValueError("SEARCH_FALLBACK_PROVIDER 仅支持 auto、none 或已支持的搜索服务")
        return cls(
            api_key=os.getenv("DATAOPENAI_API_KEY", ""),
            base_url=os.getenv("BAILIAN_BASE_URL", "https://ws-96qfzo9ijhftw0rj.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"),
            model=os.getenv("BAILIAN_MODEL", "qwen3.8-max"),
            enable_thinking=os.getenv("ENABLE_THINKING", "true").lower() == "true",
            llm_timeout=_integer("LLM_TIMEOUT_SECONDS", 120, 10, 300),
            llm_max_tokens=_integer("LLM_MAX_OUTPUT_TOKENS", 6000, 500, 20000),
            max_llm_calls=_integer("MAX_LLM_CALLS", 16, 1, 20),
            search_provider=search_provider,
            search_fallback_provider=fallback_provider,
            tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
            max_queries=_integer("SEARCH_MAX_QUERIES", 12, 7, 30),
            results_per_query=_integer("SEARCH_RESULTS_PER_QUERY", 4, 1, 8),
            max_pages=_integer("MAX_PAGES", 16, 1, 40),
            request_interval=interval,
            http_timeout=_integer("HTTP_TIMEOUT_SECONDS", 15, 2, 60),
            trend_provider=os.getenv("TREND_PROVIDER", "spot_research"),
            trend_api_url=os.getenv("TREND_API_URL", ""),
            trend_api_key=os.getenv("TREND_API_KEY", ""),
            trend_max_age_hours=_integer("TREND_MAX_AGE_HOURS", 24, 1, 168),
            research_window_days=_integer("RESEARCH_WINDOW_DAYS", 90, 1, 730),
            search_max_tool_calls=_integer("SEARCH_MAX_TOOL_CALLS", 2, 1, 5),
            research_date=os.getenv("RESEARCH_DATE", ""),
        )

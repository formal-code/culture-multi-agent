import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReviewSettings:
    api_key: str = field(repr=False)
    base_url: str = "https://ws-96qfzo9ijhftw0rj.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen3.8-omni-flash"
    timeout: int = 180
    chunk_seconds: int = 20
    max_seconds: int = 300
    max_calls: int = 40

    @classmethod
    def from_env(cls):
        return cls(
            api_key=os.getenv("DATAOPENAI_API_KEY") or os.getenv("DASHSCOPE_API_KEY", ""),
            base_url=os.getenv("OMNI_BASE_URL") or os.getenv("BAILIAN_BASE_URL") or cls.base_url,
            model=os.getenv("OMNI_MODEL", cls.model),
            timeout=int(os.getenv("VIDEO_REVIEW_TIMEOUT", "180")),
            chunk_seconds=int(os.getenv("VIDEO_REVIEW_CHUNK_SECONDS", "20")),
        )

    def validate(self):
        if not self.api_key:
            raise ValueError("请在本地环境配置 DATAOPENAI_API_KEY（也兼容 DASHSCOPE_API_KEY）")
        if not self.base_url.startswith("https://"):
            raise ValueError("Omni 接口地址必须使用 HTTPS")
        if not 5 <= self.chunk_seconds <= 30:
            raise ValueError("VIDEO_REVIEW_CHUNK_SECONDS 必须在 5 到 30 之间")
        if not 10 <= self.timeout <= 300:
            raise ValueError("VIDEO_REVIEW_TIMEOUT 必须在 10 到 300 之间")

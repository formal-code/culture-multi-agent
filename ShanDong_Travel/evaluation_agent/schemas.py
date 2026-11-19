from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from urllib.parse import urlsplit


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Brief(Model):
    spot_name: str = Field(min_length=1, max_length=200)
    city: str = Field(default="", max_length=100)
    province: str = "山东"
    target_audience: str | None = Field(default=None, max_length=2000)
    communication_goal: str | None = Field(default=None, max_length=2000)
    target_platforms: list[str] = Field(default_factory=list, max_length=10)
    target_duration_sec: float | None = Field(default=None, gt=0, le=1800)
    subject_aliases: list[str] = Field(default_factory=list, max_length=10)


class Shot(Model):
    shot_id: str = Field(min_length=1, max_length=100)
    duration_sec: float = Field(gt=0, le=1800)
    visual: str = Field(min_length=1, max_length=3000)
    voiceover: str = Field(default="", max_length=5000)
    subtitle: str = Field(default="", max_length=3000)
    material_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    removable: bool | None = None


class Script(Model):
    script_id: str = Field(min_length=1, max_length=100)
    version: int = Field(default=1, ge=1)
    title: str = Field(min_length=1, max_length=300)
    core_idea: str = Field(default="", max_length=3000)
    hook: str = Field(default="", max_length=3000)
    declared_duration_sec: float | None = Field(default=None, gt=0, le=1800)
    shots: list[Shot] = Field(min_length=1, max_length=80)
    call_to_action: str = Field(default="", max_length=2000)


class Source(Model):
    source_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=1000)
    url: str | None = Field(default=None, max_length=3000)
    publisher: str | None = None
    excerpt: str = Field(default="", max_length=40000)
    search_snippet: str = Field(default="", max_length=6000)
    published_at: str | None = None
    retrieved_at: str | None = None
    origin: Literal["upstream", "web_search", "demo"] = "upstream"
    retrieval_status: str = "provided"
    raw_text_file: str | None = None
    publisher_verified: bool = False
    retrieved_via: str = "upstream"
    research_purposes: list[str] = Field(default_factory=list)
    subject_match: bool | None = None
    publication_date_basis: str | None = None
    quality_signals: list[str] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)
    is_upstream_excerpt: bool = False
    original_url: str | None = None
    retrieval_error: str | None = None

    @field_validator("url")
    @classmethod
    def http_source_url(cls, value):
        if value is None:
            return value
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError()
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("来源网址应为无账号密码的HTTP/HTTPS网址") from exc
        return value


class Fact(Model):
    fact_id: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4000)
    source_ids: list[str] = Field(default_factory=list)
    verification_status: str = "unverified"


class Material(Model):
    material_id: str = Field(min_length=1, max_length=100)
    type: Literal["image", "video", "audio"]
    description: str = Field(default="", max_length=2000)
    file_path: str | None = None
    url: str | None = None
    usage_status: Literal["approved", "pending", "reference_only", "unknown"] = "unknown"
    license_note: str = Field(default="", max_length=2000)


class InputPacket(Model):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    brief: Brief
    scripts: list[Script] = Field(min_length=1, max_length=6)
    sources: list[Source] = Field(default_factory=list, max_length=50)
    facts: list[Fact] = Field(default_factory=list, max_length=200)
    materials: list[Material] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def unique_ids(self):
        for name, key in (("scripts", "script_id"), ("sources", "source_id"),
                          ("facts", "fact_id"), ("materials", "material_id")):
            ids = [getattr(item, key) for item in getattr(self, name)]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{name} 内 {key} 必须唯一")
        for script in self.scripts:
            ids = [s.shot_id for s in script.shots]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{script.script_id} 内 shot_id 必须唯一")
        return self


# LLM responses are validated before being used.
class ExtractedClaim(Model):
    shot_id: str
    text: str = Field(min_length=1, max_length=1000)
    search_query: str = Field(min_length=1, max_length=300)


class Extraction(Model):
    claims: list[ExtractedClaim] = Field(default_factory=list, max_length=16)
    local_culture_queries: list[str] = Field(default_factory=list, max_length=3)
    topic_keywords: list[str] = Field(default_factory=list, max_length=10)


class Citation(Model):
    source_id: str
    quote: str = Field(min_length=6, max_length=1500)
    relation: Literal["supports", "contradicts", "context"]


class ClaimAssessment(Model):
    claim_id: str
    assessment: Literal["supported_by_sources", "conflicting_sources", "mixed", "insufficient_evidence"]
    explanation: str = Field(max_length=2000)
    citations: list[Citation] = Field(default_factory=list, max_length=5)


class Observation(Model):
    dimension: Literal["target_audience", "local_distinctiveness", "opening_hook", "material_feasibility", "topic_match"]
    shot_ids: list[str] = Field(default_factory=list, max_length=20)
    finding: str = Field(min_length=1, max_length=2000)
    limitation: str = Field(default="", max_length=1000)
    citations: list[Citation] = Field(default_factory=list, max_length=5)


class MappingResult(Model):
    fact_checks: list[ClaimAssessment] = Field(default_factory=list, max_length=16)
    observations: list[Observation] = Field(default_factory=list, max_length=25)

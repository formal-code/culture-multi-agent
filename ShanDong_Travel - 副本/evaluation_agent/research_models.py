"""Step one only: planner bundle -> sourced research -> handling suggestions."""
from typing import Literal
from pydantic import Field, model_validator
from .schemas import Model
from .standards_models import StandardsInput

DIMENSIONS = {
    "factual_reliability": "事实可靠性",
    "target_audience": "目标受众",
    "local_distinctiveness": "地方特色",
    "opening_hook": "开场吸引力",
    "material_feasibility": "素材可执行性",
    "topic_relevance": "热门话题与近期动态",
}
Dimension = Literal["factual_reliability", "target_audience", "local_distinctiveness",
                    "opening_hook", "material_feasibility", "topic_relevance"]


class ResearchInput(StandardsInput):
    """Existing planner JSON is supported; scripts may bundle their resources."""
    topic: str = Field(default="", max_length=300)
    research_requirements: str = Field(default="", max_length=5000)


class ScriptReference(Model):
    script_id: str
    location: str = Field(description="title/core_idea/call_to_action，或 SH001.voiceover/visual/subtitle")
    quote: str = Field(min_length=2, max_length=1500)


class ResearchQuestion(Model):
    dimension: Dimension
    question: str = Field(min_length=2, max_length=500)
    query: str = Field(min_length=2, max_length=300)
    script_references: list[ScriptReference] = Field(default_factory=list, max_length=5)
    material_ids: list[str] = Field(default_factory=list, max_length=10)


class Understanding(Model):
    summary: str = Field(min_length=1, max_length=2500)
    questions: list[ResearchQuestion] = Field(default_factory=list, max_length=12)


class EvidenceQuote(Model):
    evidence_id: str
    quote: str = Field(min_length=6, max_length=1500)
    relation: Literal["supports", "contradicts", "context"] = "context"


class Finding(Model):
    statement: str = Field(min_length=1, max_length=2000)
    basis_type: Literal["source_statement", "inference", "programmatic_check"]
    claim_status: Literal["有支持", "存在差异", "暂无法确认"] | None = None
    topic_basis: Literal["recent_source", "evergreen_inference"] | None = None
    reasoning_summary: str = Field(min_length=1, max_length=1500)
    applicability: str = Field(min_length=1, max_length=1000)
    limitations: str = Field(min_length=1, max_length=1500)
    script_references: list[ScriptReference] = Field(default_factory=list, max_length=6)
    citations: list[EvidenceQuote] = Field(default_factory=list, max_length=6)


class Recommendation(Model):
    action: str = Field(min_length=1, max_length=1500)
    rationale: str = Field(min_length=1, max_length=1500)
    based_on_findings: list[int] = Field(min_length=1, max_length=6,
                                        description="同一维度 findings 数组中从1开始的编号")
    prerequisites: str = Field(min_length=1, max_length=1000)
    limitations: str = Field(min_length=1, max_length=1000)


class DimensionSummary(Model):
    findings: list[Finding] = Field(default_factory=list, max_length=16)
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=8)
    open_questions: list[str] = Field(default_factory=list, max_length=6)


class SixDimensionSummary(Model):
    factual_reliability: DimensionSummary = Field(default_factory=DimensionSummary)
    target_audience: DimensionSummary = Field(default_factory=DimensionSummary)
    local_distinctiveness: DimensionSummary = Field(default_factory=DimensionSummary)
    opening_hook: DimensionSummary = Field(default_factory=DimensionSummary)
    material_feasibility: DimensionSummary = Field(default_factory=DimensionSummary)
    topic_relevance: DimensionSummary = Field(default_factory=DimensionSummary)


class ScriptDimensionAnalysis(Model):
    script_id: str
    summary: DimensionSummary = Field(default_factory=DimensionSummary)


class DimensionBatch(Model):
    analyses: list[ScriptDimensionAnalysis] = Field(default_factory=list, max_length=6)


class ScriptAnalysis(Model):
    script_id: str
    dimensions: SixDimensionSummary = Field(default_factory=SixDimensionSummary)


class FullAnalysis(Model):
    analyses: list[ScriptAnalysis] = Field(default_factory=list, max_length=6)

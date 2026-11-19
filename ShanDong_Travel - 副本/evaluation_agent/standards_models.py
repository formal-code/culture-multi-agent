"""Contracts for researching assessment criteria, without assessing scripts."""
from typing import Literal
from pydantic import Field, model_validator
from .schemas import Model, InputPacket, Source

Dimension = Literal["factual_reliability", "target_audience", "local_distinctiveness",
                    "opening_hook", "material_feasibility", "topic_relevance"]


class ResearchMaterial(Model):
    evidence_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    kind: Literal["comment", "visitor_account", "opening_example", "research",
                  "platform_guidance", "local_reference", "topic_record"]
    title: str = Field(min_length=1, max_length=1000)
    url: str = Field(max_length=3000)
    text: str = Field(min_length=1, max_length=20000)
    platform: str | None = Field(default=None, max_length=100)
    published_at: str | None = Field(default=None, max_length=100)
    collected_at: str | None = Field(default=None, max_length=100)
    context_title: str = Field(default="", max_length=1000)
    context_url: str | None = Field(default=None, max_length=3000)
    sampling_method: str = Field(default="未说明，不能视为代表性样本", max_length=2000)

    @model_validator(mode="after")
    def valid_urls(self):
        Source.http_source_url(self.url)
        Source.http_source_url(self.context_url)
        if self.kind == "comment" and not self.platform:
            raise ValueError("评论需填写 platform，并保留来源网址及原文")
        return self


class StandardsInput(InputPacket):
    research_materials: list[ResearchMaterial] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def unique_research_ids(self):
        values = [r.evidence_id for r in self.research_materials]
        if len(values) != len(set(values)):
            raise ValueError("research_materials 内 evidence_id 必须唯一")
        return self


class StandardCitation(Model):
    evidence_id: str
    quote: str = Field(min_length=2, max_length=1500)


class CandidateStandard(Model):
    name: str = Field(min_length=1, max_length=200)
    criterion: str = Field(min_length=1, max_length=1500)
    rationale: str = Field(min_length=1, max_length=1500)
    comparison_questions: list[str] = Field(min_length=1, max_length=5)
    applicability: str = Field(min_length=1, max_length=1500)
    limitation: str = Field(min_length=1, max_length=1500)
    citations: list[StandardCitation] = Field(min_length=1, max_length=5)


class StandardsProposal(Model):
    criteria: list[CandidateStandard] = Field(default_factory=list, max_length=3)

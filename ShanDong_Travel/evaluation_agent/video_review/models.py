from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Scene(Model):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    description: str = Field(min_length=1)
    on_screen_text: list[str] = Field(default_factory=list)


class VisualObservation(Model):
    accessed: bool
    scenes: list[Scene] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)


class AudioObservation(Model):
    accessed: bool
    transcript: str = ""
    sound_description: str = ""
    uncertainty: list[str] = Field(default_factory=list)


class Finding(Model):
    conclusion: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    nature: Literal["成片观察", "来源记载", "合理推断", "待核实"]
    uncertainty: str = ""


DIMENSIONS = {
    "opening": "开场表达",
    "audience": "受众与需求契合",
    "pacing": "节奏与音画表达",
    "local_culture": "地方特色与内容可信度",
    "sharing": "观看、收藏与分享动机",
    "platform_topic": "平台适配与话题关联",
}


class Analysis(Model):
    overall: str = Field(min_length=1)
    dimensions: dict[str, Finding]
    strengths: list[str]
    limitations: list[str]


class AnalysisDraft(Model):
    overall: str = Field(min_length=1)
    dimensions: dict[str, dict]
    strengths: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def check_visual(value: VisualObservation, seconds: float):
    if value.accessed and not value.scenes:
        raise ValueError("accessed=true 时必须提供实际画面观察")
    for scene in value.scenes:
        if scene.end <= scene.start or scene.end > seconds + .15:
            raise ValueError("画面时间必须在当前片段内，且结束时间不早于开始时间")


def check_audio(value: AudioObservation):
    if value.accessed and not (value.transcript.strip() or value.sound_description.strip()):
        raise ValueError("accessed=true 时须有实际转写或声音描述；不能把空结果当成已识别")


def check_analysis(value: Analysis, evidence: dict):
    if set(value.dimensions) != set(DIMENSIONS):
        raise ValueError("六维键必须恰为：" + ",".join(DIMENSIONS))
    for key, finding in value.dimensions.items():
        missing = set(finding.evidence_ids) - set(evidence)
        if missing:
            raise ValueError(f"{key} 引用了不存在的依据编号：{','.join(sorted(missing))}")
        kinds = {evidence[i]["kind"] for i in finding.evidence_ids}
        if finding.nature == "成片观察" and not kinds & {"video_observation", "audio_observation", "frame_observation"}:
            raise ValueError(f"{key} 的成片观察必须引用实际视频或音频观察")
        if finding.nature == "来源记载" and not kinds & {"source_body", "upstream_excerpt"}:
            raise ValueError(f"{key} 的来源记载必须引用正文或上游摘录；搜索摘要不算正文")

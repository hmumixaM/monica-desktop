"""What each model call must return."""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field


def _decoded(value):
    """Claude through LiteLLM's tool-call emulation sometimes returns a nested array as JSON text."""
    return json.loads(value) if isinstance(value, str) else value


Decoded = BeforeValidator(_decoded)


class CommunityName(BaseModel):
    label: str = Field(description="2 到 8 个字的中文名称，概括这群人因何相识，例如「UNC 校友」「上海高中同学」")
    reason: str = Field(description="一句话说明依据")


class Source(BaseModel):
    kind: Literal["post", "profile"]
    id: str


class Reconnect(BaseModel):
    reason: str = Field(description="一句话：为什么现在适合联系，基于给出的事实")
    opener: str = Field(description="一句自然的中文开场白，不要客套，不要提到数据或 AI")
    sources: Annotated[list[Source], Decoded] = Field(description="理由依据的动态或资料")


class LifeEvent(BaseModel):
    profile_id: str
    kind: Literal["new_job", "move", "travel", "wedding", "graduation", "new_baby", "other"]
    text: str = Field(description="一句话描述")
    post_id: str = Field(description="依据的动态 id")


class Suggestion(BaseModel):
    profile_id: str
    reason: str


class KnownFact(BaseModel):
    label: str = Field(description="要点名称，例如「常驻地」「公司」「学校」「生日」「家人」「兴趣」")
    value: str


class ProfileOverview(BaseModel):
    summary: str = Field(description="约 150 到 250 字中文人物 Memory：身份、关系、稳定背景和近期变化，只写有依据的内容")
    facts: Annotated[list[KnownFact], Decoded] = Field(description="输入里已经能确定的要点，最多 15 条")
    gaps: Annotated[list[str], Decoded] = Field(description="资料里缺少、值得补充的信息名称，例如「生日」「常驻地」")


class MemoryPart(BaseModel):
    summary: str = Field(description="简明归纳这一组来源，保留时间和不确定性，不推断未证实的人际关系")
    facts: Annotated[list[str], Decoded] = Field(description="这组来源中可核实的个人事实，缺乏信息则返回空列表")


class PersonWeekPart(BaseModel):
    summary: str = Field(description="一到两句归纳本组来源；普通分享只概括主题，不夸大为人生大事")
    sources: Annotated[list[str], Decoded] = Field(description="支撑摘要的来源短 ID，只能选输入中出现的 ID，最多 4 个")


class PersonWeekAnalysis(BaseModel):
    summary: str = Field(description="两到三句中文近况，概括一周中有信息量的变化；普通分享可以概括为分享了什么")
    sources: Annotated[list[str], Decoded] = Field(description="支撑这段近况的来源短 ID，最多 4 个")


UpdateKind = Literal["birthday", "residence", "hometown", "company", "job", "school", "email", "phone", "food", "interests",
                     "family", "meetup"]


class ProfileUpdate(BaseModel):
    kind: UpdateKind
    value: str = Field(description="建议写入的内容。birthday 写 MM-DD 或 YYYY-MM-DD；meetup 写一句标题，如「和 TA 在静安吃火锅」")
    date: str | None = Field(default=None, description="仅 meetup：见面或聚会的日期 YYYY-MM-DD")
    evidence: Annotated[list[str], Decoded] = Field(description="依据的行 id，例如 d12、g3、p2、c1；至少一个")
    quote: str = Field(description="最能说明问题的原话摘录，40 字以内")
    confidence: Literal["high", "medium"]


class ProfileUpdates(BaseModel):
    items: Annotated[list[ProfileUpdate], Decoded] = Field(description="新的或与现有资料不同的信息；没有就返回空列表")


class DigestNarrative(BaseModel):
    summary: str = Field(description="三到五句的中文周报总结，按事实写，不夸张")
    life_events: Annotated[list[LifeEvent], Decoded] = Field(description="从本周动态说明里识别出的人生大事，只写有明确依据的")
    suggestions: Annotated[list[Suggestion], Decoded] = Field(description="本周建议联系的人，最多 5 个")


ImageCategory = Literal["人像", "合照", "自拍", "食物", "风景", "城市街景", "室内", "宠物", "截图", "海报活动", "文档票据", "商品",
                        "交通出行", "运动", "其他"]


class ImageReading(BaseModel):
    index: int = Field(description="图的序号，和输入里的「图 N」一致")
    category: ImageCategory
    text: str = Field(description="图里有意义的文字，照抄原文，最多 120 字；没有就留空")
    description: str = Field(description="看得到的内容，最多 25 字；不猜测")


class PostMediaReading(BaseModel):
    summary: str = Field(description="结合配文和所有图，一句话说明这条动态，最多 40 字")
    items: Annotated[list[ImageReading], Decoded]

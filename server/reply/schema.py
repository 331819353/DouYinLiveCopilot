"""模型输出的回复卡片结构与解析。"""
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError


class Card(BaseModel):
    model_config = ConfigDict(extra="ignore")
    comment_ids: list[str]
    kind: Literal["question", "challenge", "resonance", "off_topic"]
    reply: str
    expand: str = ""
    discussion_question: str = ""
    bridge_back: str = ""
    anchor_paragraph_id: str = ""
    anchor_quote: str = ""
    values_note: str = ""
    risk: Literal["none", "sensitive", "escalate"]


class ReplyOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cards: list[Card] = []


def parse_output(text: str) -> ReplyOutput | None:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if m:
        t = m.group(1)
    try:
        return ReplyOutput.model_validate(json.loads(t))
    except (json.JSONDecodeError, ValidationError):
        return None

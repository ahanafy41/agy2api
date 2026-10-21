"""Pydantic models for the small OpenAI-compatible surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

try:
    from pydantic import ConfigDict
except ImportError:
    ConfigDict = dict  # type: ignore

# Backward compatibility bridge for Pydantic v1 & v2
if not hasattr(BaseModel, "model_validate"):
    @classmethod  # type: ignore
    def _model_validate(cls, obj: Any) -> Any:
        return cls.parse_obj(obj)

    BaseModel.model_validate = _model_validate  # type: ignore


class ChatMessage(BaseModel):
    role: str
    content: Any = ""

    class Config:
        extra = "ignore"


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    class Config:
        extra = "ignore"

    model: str = "gemini-2.5-pro"
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "agy2api"

    class Config:
        extra = "ignore"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]

    class Config:
        extra = "ignore"


class ErrorBody(BaseModel):
    error: dict[str, Any] = Field(default_factory=dict)

    class Config:
        extra = "ignore"


from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportRequest(StrictModel):
    text: str = Field(max_length=4 * 1024 * 1024)
    auto_extract: bool = True


class LoginRequest(StrictModel):
    password: str = Field(min_length=1, max_length=1024)


class SourceRequest(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["local", "inbox", "115"]
    config: dict
    enabled: bool = True
    interval: int = Field(default=60, ge=0, le=86400 * 30)


class TaskCommand(StrictModel):
    action: Literal["cancel", "retry", "approve", "hide", "download_local"]
    expected_version: int | None = None

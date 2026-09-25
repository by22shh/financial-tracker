from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Category(BaseModel):
    id: str
    label: str


class Sheet(BaseModel):
    id: int
    title: str


class Catalog(Sheet):
    categories: list[Category]
    dates: list[date]
    revision: str


class Expense(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_minor: int = Field(strict=True, gt=0, le=1_000_000_000_00)
    category_id: str
    date: date
    description: str = Field(min_length=1, max_length=300)


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["today", "yesterday", "period"]
    category_ids: list[str] = Field(default_factory=list, max_length=100)


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expenses: list[Expense] = Field(max_length=20)
    clarification: str | None
    report: ReportRequest | None = None


class Button(BaseModel):
    text: str
    data: str = Field(max_length=64)


class Reply(BaseModel):
    text: str
    buttons: list[list[Button]] = Field(default_factory=list)
    # Old cached responses remain plain text after an upgrade.
    parse_mode: Literal["HTML"] | None = None

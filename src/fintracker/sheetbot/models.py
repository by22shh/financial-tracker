from datetime import date

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


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expenses: list[Expense] = Field(max_length=20)
    clarification: str | None


class Reply(BaseModel):
    text: str
    buttons: list[list[dict[str, str]]] = Field(default_factory=list)

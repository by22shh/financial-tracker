"""Authenticated Apps Script bridge, with idempotency enforced in the sheet."""

from typing import Any

import httpx

from fintracker.sheetbot.config import SheetsSettings
from fintracker.sheetbot.models import Catalog, CategoryStatus, Expense, Sheet


class BridgeError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class SheetsBridge:
    def __init__(self, settings: SheetsSettings) -> None:
        self.settings = settings

    async def call(self, action: str, **payload: Any) -> dict[str, Any]:
        if not self.settings.bridge_url or not self.settings.bridge_secret.get_secret_value():
            raise BridgeError("Связь с таблицей ещё не настроена.")
        try:
            # Apps Script returns ContentService data through a Google redirect.
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                response = await client.post(
                    self.settings.bridge_url,
                    json={
                        "secret": self.settings.bridge_secret.get_secret_value(),
                        "action": action,
                        **payload,
                    },
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BridgeError("Таблица временно недоступна.", retryable=True) from exc
        if not body.get("ok"):
            raise BridgeError(
                str(body.get("error") or "Не удалось обратиться к таблице."),
                retryable=bool(body.get("retryable")),
            )
        return dict(body["result"])

    async def sheets(self) -> list[Sheet]:
        body = await self.call("sheets")
        return [Sheet.model_validate(item) for item in body["sheets"]]

    async def catalog(self, sheet_id: int) -> Catalog:
        return Catalog.model_validate(await self.call("catalog", sheet_id=sheet_id))

    async def latest_catalog(self) -> Catalog:
        # Apps Script returns visible worksheets in tab order, not by ID or title.
        sheets = await self.sheets()
        if not sheets:
            raise BridgeError("В таблице нет доступных листов расходов.")
        return await self.catalog(sheets[-1].id)

    async def write(self, *, key: str, catalog: Catalog, expenses: list[Expense]) -> dict[str, Any]:
        return await self.call(
            "write",
            key=key,
            sheet_id=catalog.id,
            revision=catalog.revision,
            expenses=[item.model_dump(mode="json") for item in expenses],
        )

    async def amend(self, **payload: Any) -> dict[str, Any]:
        return await self.call("amend", **payload)

    async def summary(self, **payload: Any) -> dict[str, Any]:
        return await self.call("summary", **payload)

    async def category_status(
        self, *, sheet_id: int, revision: str, category_ids: list[str]
    ) -> list[CategoryStatus]:
        statuses: list[CategoryStatus] = []
        for offset in range(0, len(category_ids), 20):
            chunk = category_ids[offset : offset + 20]
            body = await self.call(
                "category_status", sheet_id=sheet_id, revision=revision, category_ids=chunk
            )
            rows = [CategoryStatus.model_validate(item) for item in body["categories"]]
            if len(rows) != len(chunk) or {item.id for item in rows} != set(chunk):
                raise BridgeError("Не удалось получить все категории таблицы.")
            statuses.extend(rows)
        return statuses

    async def period(self, **payload: Any) -> dict[str, Any]:
        return await self.call("period", **payload)

    async def create_period(self, **payload: Any) -> dict[str, Any]:
        return await self.call("create_period", **payload)

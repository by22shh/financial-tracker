"""Provider contract checks without external requests."""

import json

import httpx
import pytest
from pydantic import SecretStr

from fintracker.config import AISettings
from fintracker.core.errors import ProviderUnavailable, ValidationFailed
from fintracker.infra.ai.openai_client import OpenAIResponsesProvider
from fintracker.sheetbot.models import Extraction


@pytest.mark.parametrize("invalid_first", [False, True])
async def test_provider_returns_validated_expense_and_repairs_invalid_json(
    monkeypatch, invalid_first
):
    requests = []
    expense = {
        "amount_minor": 125050,
        "category_id": "food",
        "date": "2026-09-25",
        "description": "Продукты",
    }

    def handle(request):
        requests.append(json.loads(request.content))
        output = json.dumps({"expenses": [expense], "clarification": None})
        if invalid_first and len(requests) == 1:
            output = "invalid JSON"
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": output}]}
                ]
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    provider = OpenAIResponsesProvider(AISettings(enabled=True, api_key=SecretStr("test")))
    result = await provider.structured(
        instructions="Распознай расход",
        input_items=[{"role": "user", "content": "Продукты 1250,50"}],
        response_model=Extraction,
        schema_name="sheet_expense_v1",
    )
    assert result.parsed.model_dump(mode="json") == {"expenses": [expense], "clarification": None}
    assert len(requests) == (2 if invalid_first else 1)
    assert requests[0]["model"] == "gpt-5.6-luna"
    assert requests[0]["reasoning"] == {"effort": "medium"}
    assert requests[0]["text"]["format"]["strict"] is True


@pytest.mark.parametrize(
    "status,error",
    [(400, ValidationFailed), (429, ProviderUnavailable), (503, ProviderUnavailable)],
)
async def test_provider_errors_remain_user_facing(monkeypatch, status, error):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    provider = OpenAIResponsesProvider(AISettings(enabled=True, api_key=SecretStr("test")))
    with pytest.raises(error) as caught:
        await provider.structured(
            instructions="Распознай расход",
            input_items=[],
            response_model=Extraction,
            schema_name="sheet_expense_v1",
        )
    assert caught.value.message

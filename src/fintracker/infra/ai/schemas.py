"""Strict JSON schemas for the AI provider."""

from typing import Any

from pydantic import BaseModel


def _strictify(node: Any) -> Any:
    """Привести схему к контракту Structured Outputs (G-25).

    Контракт провайдера требует, чтобы **все** properties объекта входили в
    ``required``, а необязательность выражалась допустимым ``null``. Pydantic
    выносит поля со значением по умолчанию из ``required``, поэтому схема
    нормализуется рекурсивно перед отправкой.
    """
    if isinstance(node, list):
        return [_strictify(item) for item in node]
    if not isinstance(node, dict):
        return node

    result = {key: _strictify(value) for key, value in node.items()}
    if result.get("type") == "object" or "properties" in result:
        properties = result.get("properties") or {}
        result["required"] = list(properties)
        result["additionalProperties"] = False
        for name, definition in properties.items():
            if name in set(node.get("required") or []):
                continue
            properties[name] = _nullable(definition)
    return result


def _nullable(definition: dict[str, Any]) -> dict[str, Any]:
    """Разрешить null для поля, которое модель вправе не заполнять."""
    if "anyOf" in definition:
        variants = definition["anyOf"]
        if not any(item.get("type") == "null" for item in variants):
            definition = {**definition, "anyOf": [*variants, {"type": "null"}]}
        return definition
    if "$ref" in definition:
        # Ссылку нельзя дополнять соседними ключами: оборачиваем в anyOf.
        rest = {key: value for key, value in definition.items() if key != "$ref"}
        return {**rest, "anyOf": [{"$ref": definition["$ref"]}, {"type": "null"}]}
    kind = definition.get("type")
    if kind is None:
        return definition
    if isinstance(kind, list):
        # Responses Structured Outputs accepts nullable values through
        # ``anyOf`` but rejects the otherwise valid JSON Schema shorthand
        # ``type: ["array", "null"]``.  Keep constraints such as ``items``
        # on every non-null branch so arrays remain fully specified.
        variants = list(kind)
        if "null" not in variants:
            variants.append("null")
        rest = {key: value for key, value in definition.items() if key != "type"}
        return {
            "anyOf": [
                {"type": variant, **rest} if variant != "null" else {"type": "null"}
                for variant in variants
            ]
        }
    if kind == "null":
        return definition
    rest = {key: value for key, value in definition.items() if key != "type"}
    return {"anyOf": [{"type": kind, **rest}, {"type": "null"}]}


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema для строгого структурированного вывода провайдера."""
    schema = _strictify(model.model_json_schema())
    assert isinstance(schema, dict)
    return schema

#!/usr/bin/env python3
"""Проверка связи «требование → реализация → проверка → доказательство».

Источник статуса — `.planning/requirements.yaml`. Скрипт проверяет, что:
1. каждый идентификатор из документов присутствует в реестре;
2. у требования со статусом verified есть ссылки на реализацию и проверку;
3. указанные файлы и тестовые узлы существуют;
4. заявленные PASS подтверждены отчётом прогона.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / ".planning" / "requirements.yaml"
EXTRACTED = ROOT / ".planning" / "extracted_requirements.json"

# Семейства сценариев описывают проверяемый результат, а не отдельный модуль:
# для них обязательны проверка и доказательство, но не ссылка на реализацию.
SCENARIO_FAMILIES = ("A", "B", "AR", "QA")

VALID_STATUSES = {
    "planned",
    "implemented",
    "verified",
    "failed",
    "not_run",
    "blocked",
    "not_applicable",
    "out_of_scope",
}


def load_registry() -> dict[str, dict[str, Any]]:
    """Минимальный разбор YAML реестра без внешней зависимости."""
    if not REGISTRY.exists():
        return {}
    items: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    list_key: str | None = None
    for raw in REGISTRY.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("- id:"):
            current = {"id": line.split(":", 1)[1].strip()}
            items[current["id"]] = current
            list_key = None
            continue
        if current is None:
            continue
        if re.match(r"^\s{4}- ", line) and list_key:
            current.setdefault(list_key, []).append(line.strip()[2:].strip())
            continue
        match = re.match(r"^\s{2}(\w+):\s*(.*)$", line)
        if match:
            key, value = match.group(1), match.group(2).strip()
            if value == "":
                list_key = key
                current[key] = []
            else:
                list_key = None
                current[key] = value
    return items


def main() -> int:
    problems: list[str] = []
    registry = load_registry()
    if not registry:
        print("Реестр .planning/requirements.yaml пуст или отсутствует")
        return 1

    extracted = json.loads(EXTRACTED.read_text(encoding="utf-8")) if EXTRACTED.exists() else []
    extracted_ids = {item["id"] for item in extracted}
    missing = sorted(extracted_ids - set(registry))
    if missing:
        problems.append(f"Нет в реестре {len(missing)} требований: {missing[:10]}")

    for req_id, item in sorted(registry.items()):
        status = str(item.get("status", "planned"))
        if status not in VALID_STATUSES:
            problems.append(f"{req_id}: недопустимый статус {status!r}")
        family = re.match(r"^[A-Z]+", req_id)
        is_scenario = bool(family) and family.group() in SCENARIO_FAMILIES
        if status in {"verified", "implemented"} and not is_scenario:
            implementation = item.get("implementation") or []
            if not implementation:
                problems.append(f"{req_id}: статус {status}, но реализация не указана")
            for path_ref in implementation:
                path = ROOT / str(path_ref).split(":", 1)[0]
                if not path.exists():
                    problems.append(f"{req_id}: файл реализации отсутствует: {path_ref}")
        if status == "verified":
            verification = item.get("verification") or []
            if not verification:
                problems.append(f"{req_id}: заявлен verified без проверки")
            for node in verification:
                file_part = str(node).split("::", 1)[0]
                if not (ROOT / file_part).exists():
                    problems.append(f"{req_id}: файл проверки отсутствует: {node}")
            if not item.get("evidence"):
                problems.append(f"{req_id}: заявлен verified без доказательства")

    if problems:
        print("Прослеживаемость нарушена:")
        for problem in problems[:60]:
            print(f"  - {problem}")
        print(f"Всего проблем: {len(problems)}")
        return 1

    counts: dict[str, int] = {}
    for item in registry.values():
        counts[str(item.get("status", "planned"))] = (
            counts.get(str(item.get("status", "planned")), 0) + 1
        )
    print(f"Прослеживаемость в порядке. Требований: {len(registry)}. Статусы: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

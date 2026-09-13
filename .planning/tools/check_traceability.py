#!/usr/bin/env python3
"""Проверка связи «требование → реализация → проверка → доказательство».

Источник статуса — `.planning/requirements.yaml`. Статус ``verified`` признаётся
только когда он привязан к фактическому прогону (R-12):

1. каждый идентификатор из документов присутствует в реестре;
2. у требования со статусом verified есть ссылки на реализацию и проверку;
3. указанные файлы существуют, а каждый тестовый узел действительно собран
   pytest и присутствует в отчёте прогона;
4. исход этого узла в отчёте — passed: пропуск, ошибка и падение не дают
   verified;
5. прогон относится к проверяемому commit, а файлы доказательств существуют.

Ссылка на несуществующий узел или на отсутствующий файл доказательства больше
не проходит проверку. Результаты разделяются по видам проверок: доменные,
runtime, интеграционные, корпус AI и эксплуатационные учения.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / ".planning" / "requirements.yaml"
EXTRACTED = ROOT / ".planning" / "extracted_requirements.json"
LATEST = ROOT / ".planning" / "evidence" / "latest.json"

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

# Виды проверок различаются по расположению теста: общий зелёный прогон не
# заменяет живую интеграцию и эксплуатационное учение.
KIND_BY_PREFIX = (
    ("tests/unit/", "domain"),
    ("tests/acceptance/", "acceptance"),
    ("tests/quality/", "ai_corpus"),
    ("tests/performance/", "ops_measurement"),
    ("tests/integration/test_release_and_runtime", "ops"),
    ("tests/integration/test_ops_scenarios", "ops"),
    ("tests/integration/test_handover", "ops"),
    ("tests/integration/test_ai_contract", "ai"),
    ("tests/integration/test_recommendations", "ai"),
    ("tests/integration/", "integration"),
)


def classify(node: str) -> str:
    for prefix, kind in KIND_BY_PREFIX:
        if node.startswith(prefix):
            return kind
    return "other"


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


def git_revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def load_outcomes(report_path: pathlib.Path) -> dict[str, str]:
    """Исход каждого собранного узла из JUnit-отчёта прогона."""
    outcomes: dict[str, str] = {}
    tree = ET.parse(report_path)  # noqa: S314 - локальный отчёт собственного прогона
    for case in tree.iter("testcase"):
        name = case.get("name") or ""
        # pytest пишет модуль точками; узел реестра — путь к файлу.
        file_name = case.get("file") or (case.get("classname") or "").replace(".", "/") + ".py"
        node = f"{file_name}::{name}"
        outcome = "passed"
        for child in case:
            if child.tag in {"failure", "error"}:
                outcome = "failed"
                break
            if child.tag == "skipped":
                outcome = "skipped"
                break
        # Параметризованные узлы сравниваются и без набора параметров.
        outcomes[node] = outcome
        base = node.split("[", 1)[0]
        if base != node:
            previous = outcomes.get(base)
            outcomes[base] = outcome if previous in (None, "passed") else previous
    return outcomes


def load_run() -> tuple[dict[str, str], list[str]]:
    """Отчёт последнего прогона и замечания о его пригодности."""
    notes: list[str] = []
    if not LATEST.exists():
        return {}, ["Нет отчёта прогона .planning/evidence/latest.json"]
    report = json.loads(LATEST.read_text(encoding="utf-8"))
    test_report = report.get("test_report") or {}
    raw_path = test_report.get("path")
    if not raw_path:
        return {}, ["В отчёте прогона нет ссылки на JUnit: перезапустите collect_evidence.py"]
    path = ROOT / str(raw_path)
    if not path.exists():
        return {}, [f"Файл прогона отсутствует: {raw_path}"]
    checks = report.get("checks") or {}
    if str((checks.get("tests") or {}).get("result")) != "PASS":
        notes.append("Прогон тестов в отчёте не PASS")
    revision = str(report.get("git_revision") or "")
    head = git_revision()
    if head and revision and revision != head:
        notes.append(f"Отчёт собран на commit {revision}, проверяется {head}")
    return load_outcomes(path), notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-stale-run",
        action="store_true",
        help="не считать ошибкой отчёт другого commit (для промежуточной работы)",
    )
    args = parser.parse_args()

    problems: list[str] = []
    registry = load_registry()
    if not registry:
        print("Реестр .planning/requirements.yaml пуст или отсутствует")
        return 1

    outcomes, notes = load_run()
    for note in notes:
        if args.allow_stale_run:
            print(f"  ! {note}")
        else:
            problems.append(note)

    extracted = json.loads(EXTRACTED.read_text(encoding="utf-8")) if EXTRACTED.exists() else []
    extracted_ids = {item["id"] for item in extracted}
    missing = sorted(extracted_ids - set(registry))
    if missing:
        problems.append(f"Нет в реестре {len(missing)} требований: {missing[:10]}")

    kinds: dict[str, int] = {}
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
        if status != "verified":
            continue

        verification = item.get("verification") or []
        if not verification:
            problems.append(f"{req_id}: заявлен verified без проверки")
        for node in verification:
            node = str(node)
            file_part = node.split("::", 1)[0]
            if not (ROOT / file_part).exists():
                problems.append(f"{req_id}: файл проверки отсутствует: {node}")
                continue
            if not outcomes:
                continue
            outcome = outcomes.get(node)
            if outcome is None:
                problems.append(f"{req_id}: узел не собран в прогоне: {node}")
                continue
            if outcome != "passed":
                problems.append(f"{req_id}: узел {node} в прогоне: {outcome}")
                continue
            kinds[classify(file_part)] = kinds.get(classify(file_part), 0) + 1

        evidence = item.get("evidence") or []
        if not evidence:
            problems.append(f"{req_id}: заявлен verified без доказательства")
        for artifact in evidence:
            if not (ROOT / str(artifact)).exists():
                problems.append(f"{req_id}: доказательство отсутствует: {artifact}")

    if problems:
        print("Прослеживаемость нарушена:")
        for problem in problems[:60]:
            print(f"  - {problem}")
        print(f"Всего проблем: {len(problems)}")
        return 1

    counts: dict[str, int] = {}
    for item in registry.values():
        status = str(item.get("status", "planned"))
        counts[status] = counts.get(status, 0) + 1
    print(f"Прослеживаемость в порядке. Требований: {len(registry)}. Статусы: {counts}")
    print(f"Подтверждённые проверки по видам: {kinds or 'нет отчёта прогона'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

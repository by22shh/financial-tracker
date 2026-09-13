#!/usr/bin/env python3
"""Связывает требования с реализацией и проверками по маркерам в коде.

Источник связи — упоминание идентификатора требования в docstring или
комментарии файла реализации и в имени/докстроке теста. Это обоснованная
связь, а не формальная таблица: маркер ставится там, где требование
действительно исполняется или проверяется.
"""

from __future__ import annotations

import pathlib
import re
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
TESTS = ROOT / "tests"

ID_PATTERN = re.compile(
    r"\b(?:FR-\d{2}|AI-\d{2}|TECH-\d{2}|QA-\d{2}|AR-\d{2}|ADR-\d{2}|CMD-\d{2}"
    r"|NFR-\d{2}|RET-\d{2}|SEC-\d{2}|FORM-\d{2}|OPS-\d{2}|LIM-\d{2}"
    r"|A\d{2,3}|B\d{1,2})\b"
)

# Слова, после которых идентификатор в тексте не считается ссылкой.
FALSE_POSITIVES = {"A4", "B1000"}


# Артефакты вне исходного кода, где живут решения о стеке и выпуске.
EXTRA_ARTIFACTS = (
    "pyproject.toml",
    "docker-compose.yml",
    "Makefile",
    ".github/workflows/ci.yml",
    ".env.example",
)


def scan(root: pathlib.Path, suffix: str = ".py") -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for path in root.rglob(f"*{suffix}"):
        if "migrations/versions" in str(path) and path.name.startswith("2026"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        relative = str(path.relative_to(ROOT))
        for match in ID_PATTERN.findall(text):
            if match in FALSE_POSITIVES:
                continue
            found[match].add(relative)
    return found


def scan_tests() -> dict[str, set[str]]:
    """Собрать проверки как «файл::функция» по маркерам в теле теста."""
    found: dict[str, set[str]] = defaultdict(set)
    for path in TESTS.rglob("test_*.py"):
        text = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(ROOT))
        current: str | None = None
        for line in text.splitlines():
            function = re.match(r"^(?:async )?def (test_\w+)", line)
            if function:
                current = function.group(1)
            if current is None:
                continue
            for match in ID_PATTERN.findall(line):
                if match in FALSE_POSITIVES:
                    continue
                found[match].add(f"{relative}::{current}")
    return found


def scan_extra() -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for name in EXTRA_ARTIFACTS:
        path = ROOT / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for match in ID_PATTERN.findall(text):
            if match in FALSE_POSITIVES:
                continue
            found[match].add(name)
    return found


def main() -> int:
    implementation = scan(SRC)
    for key, values in scan_extra().items():
        implementation[key].update(values)
    verification = scan_tests()
    registry_path = ROOT / ".planning" / "requirements.yaml"
    lines = registry_path.read_text(encoding="utf-8").splitlines()

    output: list[str] = []
    current_id: str | None = None
    skip_block = False
    evidence = ".planning/evidence/latest.json"

    for line in lines:
        if line.startswith("- id:"):
            current_id = line.split(":", 1)[1].strip()
            skip_block = False
            output.append(line)
            continue
        if current_id and line.strip() in {
            "implementation:",
            "verification:",
            "evidence:",
        }:
            key = line.strip().rstrip(":")
            skip_block = True
            items: set[str] = set()
            if key == "implementation":
                items = implementation.get(current_id, set())
            elif key == "verification":
                items = verification.get(current_id, set())
            elif key == "evidence" and verification.get(current_id):
                items = {evidence}
            if items:
                output.append(f"  {key}:")
                output.extend(f"    - {item}" for item in sorted(items))
            else:
                output.append(f"  {key}:")
            continue
        if skip_block and line.startswith("    - "):
            continue
        if skip_block and (line.startswith("  ") or line == ""):
            skip_block = False
        if current_id and line.strip().startswith("status:"):
            verified = bool(verification.get(current_id))
            implemented = bool(implementation.get(current_id))
            existing = line.split(":", 1)[1].strip()
            if existing in {"blocked", "not_applicable", "out_of_scope"}:
                output.append(line)
                continue
            if verified:
                output.append("  status: verified")
            elif implemented:
                output.append("  status: implemented")
            else:
                output.append("  status: planned")
            continue
        output.append(line)

    registry_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")

    covered = len({key for key in verification if verification[key]})
    print(f"Проверками покрыто идентификаторов: {covered}")
    print(f"Реализацией отмечено идентификаторов: {len(implementation)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

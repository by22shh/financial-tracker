#!/usr/bin/env python3
"""Сводка выполнения по этапам и семействам требований."""

from __future__ import annotations

import pathlib
import re
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from check_traceability import load_registry  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]


def family(req_id: str) -> str:
    match = re.match(r"^[A-Z]+", req_id)
    return match.group() if match else "?"


def main() -> int:
    registry = load_registry()
    by_family: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_stage: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    planned_ids: dict[str, list[str]] = defaultdict(list)

    for req_id, item in registry.items():
        status = str(item.get("status", "planned"))
        stage = str(item.get("stage", "P0"))
        by_family[family(req_id)][status] += 1
        by_stage[stage][status] += 1
        if status == "planned":
            planned_ids[family(req_id)].append(req_id)

    print("| Семейство | verified | implemented | planned | blocked | всего |")
    print("|---|---|---|---|---|---|")
    for name in sorted(by_family):
        row = by_family[name]
        total = sum(row.values())
        print(
            f"| {name} | {row.get('verified', 0)} | {row.get('implemented', 0)} | "
            f"{row.get('planned', 0)} | {row.get('blocked', 0)} | {total} |"
        )

    print()
    print("| Этап | verified | implemented | planned | blocked | всего |")
    print("|---|---|---|---|---|---|")
    for stage in sorted(by_stage):
        row = by_stage[stage]
        total = sum(row.values())
        print(
            f"| {stage} | {row.get('verified', 0)} | {row.get('implemented', 0)} | "
            f"{row.get('planned', 0)} | {row.get('blocked', 0)} | {total} |"
        )

    print()
    print("Требования без проверки и реализации:")
    for name in sorted(planned_ids):
        ids = sorted(planned_ids[name])
        print(f"  {name}: {len(ids)} — {', '.join(ids[:20])}{'…' if len(ids) > 20 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

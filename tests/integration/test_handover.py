"""Полнота передаваемого комплекта (OPS-06)."""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_ops06_handover_kit_is_complete() -> None:
    """OPS-06: отчёт содержит готовность, запуск, миграции, блокеры и доказательства."""
    report = (ROOT / "FINAL_REPORT.md").read_text(encoding="utf-8")
    for section in (
        "Степень готовности",
        "Измеренные показатели",
        "Внешние блокеры",
        "Запуск",
        "Миграции",
        "Доказательства",
    ):
        assert section in report, f"в отчёте нет раздела «{section}»"
    for blocker in ("BL-01", "BL-02", "BL-03", "BL-04"):
        assert blocker in report, f"блокер {blocker} не указан"

    registry = ROOT / ".planning" / "requirements.yaml"
    assert registry.exists(), "реестр требований передан"

    evidence = ROOT / ".planning" / "evidence"
    assert (evidence / "latest.json").exists(), "прогон проверок сохранён"
    for name in ("performance.json", "restore_drill.json", "extraction_accuracy.json"):
        path = evidence / name
        if not path.exists():
            continue
        json.loads(path.read_text(encoding="utf-8"))

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in ("up:", "migrate:", "check:", "evidence:", "trace:", "image:"):
        assert target in makefile, f"в Makefile нет цели {target}"

    # Единый артефакт на api/worker/scheduler передаётся вместе с кодом (ADR-01).
    dockerfile = ROOT / "Dockerfile"
    assert dockerfile.exists(), "Dockerfile приложения передан"
    body = dockerfile.read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["fintracker"]' in body, "образ запускает единую команду"
    assert "USER fintracker" in body, "приложение работает не от root"

    # Заявления о внешних адаптерах должны совпадать с кодом (BL-04).
    storage = (ROOT / "src/fintracker/infra/storage.py").read_text(encoding="utf-8")
    if 'settings.backend == "s3"' not in storage:
        assert "S3-совместимого адаптера хранилища и журнала доступа нет" in report

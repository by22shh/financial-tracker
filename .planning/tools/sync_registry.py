#!/usr/bin/env python3
"""Синхронизация requirements.yaml с составом документов.

Добавляет новые идентификаторы со статусом planned и сохраняет уже
проставленные статусы, реализацию, проверки и доказательства.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / ".planning" / "requirements.yaml"
EXTRACTED = ROOT / ".planning" / "extracted_requirements.json"

# Обязательные правила без авторского ID (см. .planning/REQUIREMENTS.md).
LOCAL_IDS: dict[str, tuple[str, str, str]] = {
    **{
        f"NFR-{i:02d}": (title, "docs/TZ.md §23", stage)
        for i, (title, stage) in enumerate(
            [
                ("Приём события p95 <= 1 с до долговечного сохранения", "P0"),
                ("Обратная связь <= 2 с после приёма", "P0"),
                ("Текст p95 <= 5 с до карточки", "P0"),
                ("Голос p95 <= 20 с для записи до 30 с", "P0"),
                ("Чек p95 <= 30 с для читаемого изображения", "P0"),
                ("Числовой отчёт p95 <= 2 с на 50 000 операций", "P0"),
                ("Нагрузка 5/с 10 мин и всплеск 20/с без потерь и дублей", "P0"),
                ("Совместность 10 бюджетов по 5 участников", "P0"),
                ("Доступность собственного API 99,5% в месяц", "P0"),
                ("Восстановление RPO <= 1 ч, RTO <= 4 ч", "P0"),
                ("Вложения <= 15 MB с проверкой типов и размеров", "P0"),
                ("Экспорт участникам, удаление бюджета администратором", "P0"),
                ("Мониторинг перечисленных метрик", "P0"),
                ("Деградация при недоступности AI", "P0"),
            ],
            start=1,
        )
    },
    **{
        f"RET-{i:02d}": (title, "docs/TZ.md §24", "P0")
        for i, title in enumerate(
            [
                "Журнал и ревизии хранятся пока существует пространство",
                "Исходное аудио до 24 ч после успешной обработки",
                "Нераспознанное нужное вложение до 7 дней",
                "Фото чеков 30 дней",
                "Технические логи 30 дней без финансового текста",
                "Резервные копии — окно 30 дней",
                "Файлы экспорта 24 ч",
                "Сырой текст, transcript и детальный разбор до 7 дней",
                "Очистка удалённого бюджета в течение 24 ч",
                "Непривязанные staging объекты очищаются через 24 ч",
            ],
            start=1,
        )
    },
    **{
        f"SEC-{i:02d}": (title, "docs/TZ.md §24, docs/ARCHITECTURE.md", "P0")
        for i, title in enumerate(
            [
                "Личность только из доверенного источника",
                "RLS ENABLE+FORCE, runtime роль без BYPASSRLS и не владелец",
                "Контекст RLS через set_config is_local=true",
                "Секрет приглашения хранится как HMAC-SHA-256",
                "Выдача файлов через авторизованный серверный endpoint",
                "Логи без токенов, ключей и финансового payload",
                "Проверка типа/размера/декодирования файла до дорогой обработки",
                "Экспорт защищён от formula injection",
                "Составные FK не позволяют связать объекты разных бюджетов",
                "Протокол SecurityChange для всех изменений доступа",
            ],
            start=1,
        )
    },
    **{
        f"FORM-{i:02d}": (title, "docs/TZ.md §10–11, §9.1.1", "P0")
        for i, title in enumerate(
            [
                "Формулы L, S, R, C, A по строке бюджета",
                "Процент использования и состояния нулевого/незаданного лимита",
                "Прогноз = факт + обязательства + гибкие, темп после 7 дней",
                "Свободные деньги по формуле раздела 11.2 (P1)",
                "Взнос в фонд = округление вверх недостающей суммы",
                "Календарный повтор от исходного anchor с ограничением месяца",
                "Фиксированный повтор в календарных днях",
                "Полуинтервал периода и включённые даты в интерфейсе",
                "Распределение скидки методом наибольших остатков",
                "Опережение обзора плана min(3, длительность-1)",
                "Порог раннего риска max(500 ₽, 10% лимита)",
            ],
            start=1,
        )
    },
    **{
        f"OPS-{i:02d}": (title, "docs/ARCHITECTURE.md §13, §15", "P0")
        for i, title in enumerate(
            [
                "Миграция отдельным заданием до нового кода",
                "Liveness отделён от readiness, AI не блокирует readiness",
                "Раздельные среды и секреты вне репозитория",
                "Совместимость соседних выпусков и schema_version",
                "Документированный откат приложения",
                "Полный передаваемый комплект результатов",
            ],
            start=1,
        )
    },
    **{
        f"LIM-{i:02d}": (title, "docs/TZ.md", "P0")
        for i, title in enumerate(
            [
                "Код приглашения 12 символов, 7 дней, 10 применений",
                "5 неуспешных вводов кода за 15 минут",
                "Голос до 3 минут с отказом до платной обработки",
                "Комментарий до 2000 символов без молчаливой обрезки",
                "Черновик активен 7 дней",
                "Не более 2 проактивных сообщений в день на получателя",
                "Тихие часы 22:00–09:00 в личном поясе",
                "Атомарный пакет импорта/слияния <= 5000 операций",
                "Изображение <= 40 Мп и <= 16384 px; альбом <= 10 файлов",
                "Telegram: сообщение <= 4096 символов, короткие callback",
                "Месячный лимит расходов AI с деградацией",
            ],
            start=1,
        )
    },
}

# Этап по семейству идентификаторов.
P1_REQUIREMENTS = {"FR-31", "FR-43", "FR-44", "FR-48", "FR-68"}
P1_SCENARIOS = {"A43", "A44", "A107", "A104", "A105"}


def stage_for(req_id: str) -> str:
    if req_id in P1_REQUIREMENTS or req_id in P1_SCENARIOS:
        return "P1"
    return "P0"


def parse_existing() -> dict[str, list[str]]:
    """Сохранить существующие блоки по ID."""
    if not REGISTRY.exists():
        return {}
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in REGISTRY.read_text(encoding="utf-8").splitlines():
        if line.startswith("- id:"):
            current = line.split(":", 1)[1].strip()
            blocks[current] = [line]
        elif current is not None and (line.startswith("  ") or line == ""):
            blocks[current].append(line)
    return blocks


def escape(value: str) -> str:
    cleaned = value.replace('"', "'").strip()
    return cleaned


def main() -> int:
    extracted = json.loads(EXTRACTED.read_text(encoding="utf-8"))
    existing = parse_existing()
    lines: list[str] = [
        "# Реестр обязательных требований. Единственный источник статуса.",
        "# status: planned | implemented | verified | failed | not_run | blocked |",
        "#         not_applicable | out_of_scope",
        "# Генерируется .planning/tools/sync_registry.py; статусы сохраняются.",
        "",
    ]
    seen: set[str] = set()

    def emit(req_id: str, title: str, source: str, stage: str) -> None:
        seen.add(req_id)
        if req_id in existing:
            lines.extend(existing[req_id])
            if lines and lines[-1] != "":
                lines.append("")
            return
        lines.extend(
            [
                f"- id: {req_id}",
                f'  title: "{escape(title)}"',
                f"  source: {source}",
                f"  stage: {stage}",
                "  status: planned",
                "  implementation:",
                "  verification:",
                "  evidence:",
                "",
            ]
        )

    for item in extracted:
        title = item.get("title", "")
        emit(item["id"], title, item["source"], stage_for(item["id"]))
    for req_id, (title, source, stage) in LOCAL_IDS.items():
        emit(req_id, title, source, stage)

    orphans = sorted(set(existing) - seen)
    if orphans:
        print(f"ВНИМАНИЕ: в реестре есть ID вне документов: {orphans[:10]}")
        for req_id in orphans:
            lines.extend(existing[req_id])

    REGISTRY.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Реестр обновлён: {len(seen)} требований")
    return 0


if __name__ == "__main__":
    sys.exit(main())

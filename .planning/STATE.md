# STATE — текущее состояние работы

Обновлено: после срезов S00–S18 (журнал, обзоры, напоминания, устойчивость).

## Среда

| Компонент | Состояние |
|---|---|
| Python | 3.13.15 (uv), venv `.venv` |
| PostgreSQL | 17.2 в Docker, `docker compose up -d postgres`, порт 55432 |
| Роли БД | `fintracker_owner` (миграции), `fintracker_api`, `fintracker_worker` (NOBYPASSRLS) |
| Зависимости | aiogram 3.31, FastAPI 0.115.14, Pydantic 2.13.5, SQLAlchemy 2.0.52, psycopg 3.3.5, Alembic 1.20, httpx 0.28.1, openpyxl 3.1.5, structlog 25.5 |
| Проверки | ruff 0.15.22, mypy 1.20.2 (strict), pytest 8.4.2, hypothesis 6.168 |
| Миграции | 0001 initial → 0002 RLS → 0003 bootstrap → 0004 admin trigger → 0005 invite lookup → 0006 порядок добавления операций |

## Сделано и проверено

| Область | Состояние | Доказательство |
|---|---|---|
| Денежное ядро и календарь | проверено | `tests/unit/` — 65 PASS, property-инварианты |
| Схема, RLS, ограничения | проверено | `tests/integration/test_schema_constraints.py` |
| Журнал денег: ревизии, возвраты, отмена, восстановление | проверено | `test_money_scenarios.py`, `test_ar_money_sequences.py` |
| Бюджет, периоды, переносы | проверено | `test_budget_math.py`, `test_period_automation.py` |
| Диалог: ввод, исправления, уточнения | проверено | `tests/acceptance/` |
| История с фильтрами и сортировкой | проверено | `test_journal_filters.py`, `test_history_filters.py` |
| Обзоры, итог периода, план следующего | проверено | `test_reviews.py`, `test_reviews_flow.py` |
| Правила классификации, личные настройки | проверено | `test_rules_and_preferences.py`, `test_settings_and_rules.py` |
| Напоминания о платежах и доставка | проверено | `test_reminders.py`, `test_delivery.py` |
| Устойчивость AR-01…AR-27 | проверено | `test_architecture_review.py` |
| Импорт и экспорт | проверено | `test_import_export.py` |
| AI-контракт и квоты | проверено | `test_ai_contract.py`, `test_recommendations.py` |

## В работе

- Измерение производительности (NFR-01, NFR-03, NFR-06, NFR-07, NFR-08, AR-34):
  `tests/performance/test_performance.py`, запуск `FINTRACKER_PERF=1`.
- Учение по восстановлению (AR-33, NFR-10): `.planning/tools/restore_drill.py`.
- Остаток P0: AI-04, AI-07, ADR-16, QA-02, QA-04, часть сценариев A.
- P1: FR-43, FR-44, FR-48, FR-68, FORM-04.

## Блокеры

См. `.planning/BLOCKERS.md`: BL-01 (ключ OpenAI), BL-02 (модель ASR),
BL-03 (токен Telegram и HTTPS), BL-04 (среда размещения).
BL-05 и BL-07 не блокируют разработку.

## Команды

```bash
make up        # PostgreSQL 17 + роли
make migrate   # миграции
make check     # формат, линтер, типы, проверки
make evidence  # собрать доказательства в .planning/evidence/
FINTRACKER_PERF=1 .venv/bin/pytest tests/performance -q   # измерение NFR
.venv/bin/python .planning/tools/restore_drill.py         # учение восстановления
```

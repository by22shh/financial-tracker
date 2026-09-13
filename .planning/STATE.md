# STATE — текущее состояние работы

Обновлено: 14 сентября 2026, после исправления замечаний V-01…V-09
последней независимой проверки. Подробности: `docs/IMPLEMENTATION_FIXES_2026-09-14.md`.

## Среда

| Компонент | Состояние |
|---|---|
| Python | 3.13.15 (uv), venv `.venv` |
| PostgreSQL | 17.2 в Docker, `docker compose up -d postgres`, порт 55432 |
| Роли БД | `fintracker_owner` (миграции), `fintracker_api`, `fintracker_worker` (NOBYPASSRLS) |
| Зависимости | aiogram 3.31, FastAPI 0.115.14, Pydantic 2.13.5, SQLAlchemy 2.0.52, psycopg 3.3.5, Alembic 1.20, httpx 0.28.1, openpyxl 3.1.5, structlog 25.5 |
| Проверки | ruff 0.15.22, mypy 1.20.2 (strict), pytest 8.4.2, hypothesis 6.168 |
| Миграции | 0001 initial → 0002 RLS → 0003 bootstrap → 0004 admin trigger → 0005 invite lookup → 0006 порядок операций → 0007 индексы → 0008 чтение схемы → 0009 служебные функции обслуживания → 0010 идентичность ввода и файлы → 0011 изолированный ответ автору → 0012 ограниченное восстановление доступа → 0013 попытки анализа и атомарная публикация |

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

## Аудит и повторная проверка

| Набор | Результат | Где |
|---|---|---|
| Диагностики первого аудита (21) | все проходят | `.planning/audits/2026-09-13/run_reproductions.py --commit HEAD` |
| Диагностики повторной проверки (36 = 21 + 15) | все проходят | `.planning/audits/2026-09-13-recheck-a63cxl89/run_recheck.py` |
| Исправления V-01…V-09 | 9/9 закрыты; дополнительная диагностика 11/11 | `docs/IMPLEMENTATION_FIXES_2026-09-14.md` |
| Полный сохранённый прогон | 546 passed, 0 skipped; Ruff и mypy PASS | `.planning/evidence/latest.json`, `latest-junit.xml` |
| Подмена доказательств | три испорченных варианта отвергнуты; 19 штатных тестов PASS | `tests/unit/test_traceability_evidence.py` |
| Те же инварианты в штатном наборе | `tests/integration/test_deep_audit.py`, `test_audit_regressions.py`, `test_audit_access_money.py`, `test_recheck_regressions.py` | без изоляций и подмен |

Ключевые исправления: атомарная идентичность пользовательского ввода
(`drafts.source_message_key`), проверка права исполнителя в транзакции записи
(`core/fencing.py`), полный жизненный цикл возврата, перепроверка получателя
отложенной доставки, replay состояния удаления и сверка доступа при старте,
исход rejected у SecurityChange, периодический анализ по календарю бюджета с
вызовом модели вне транзакции, маршрут таблиц в импорт, удаление файлов
вместе с данными бюджета, привязка `verified` к фактическому прогону.

## В работе

- Измерение производительности (NFR-01, NFR-03, NFR-06, NFR-07, NFR-08, AR-34):
  `tests/performance/test_performance.py`, запуск `FINTRACKER_PERF=1`.
- Учение по восстановлению (AR-33, NFR-10): `.planning/tools/restore_drill.py`.
- Реестр: 481 verified, 8 planned, 9 blocked, 2 implemented из 500.
- Не завершённые требования P1: FR-43, FR-68, FORM-04, A43, A44, A104,
  A105, A107; B7 и FR-31 реализованы, но не verified.
- QA-04 — внешний пилот, blocked до BL-03.
- Эталонный набор ТЗ §25.1 собран частично: 36 размеченных текстов и 19
  защитных входов вместо 250/100/100/50. Голос и чеки требуют BL-02 и BL-01.
  Текущее покрытие и разрыв записаны в `.planning/evidence/extraction_accuracy.json`.
- S3-совместимого адаптера хранилища и журнала доступа нет: рабочий backend —
  `filesystem`, `s3` явно отказывает (BL-04).
- NFR-10 (RPO) и AR-33 (restore из WAL/PITR) переведены в `blocked`: учение
  измеряет RTO и целостность копии, но без архива WAL не доказывает RPO.

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
FINTRACKER_PERF=1 .venv/bin/pytest tests/performance -q   # измерение NFR и учение восстановления
.venv/bin/python .planning/tools/check_traceability.py    # verified по фактическому прогону
docker build -t fintracker:local .                        # единый артефакт api/worker/scheduler
```

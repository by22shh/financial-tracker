# STATE — текущее состояние работы

Обновлено: после bootstrap и начала S01/S02.

## Среда

| Компонент | Состояние |
|---|---|
| Python | 3.13.15 (uv), venv `.venv` |
| PostgreSQL | 17.2 в Docker, `docker compose up -d postgres`, порт 55432 |
| Роли БД | `fintracker_owner` (миграции), `fintracker_api`, `fintracker_worker` (NOBYPASSRLS) |
| Зависимости | aiogram 3.31, FastAPI 0.115.14, Pydantic 2.13.5, SQLAlchemy 2.0.52, psycopg 3.3.5, Alembic 1.20, httpx 0.28.1, openpyxl 3.1.5, structlog 25.5 |
| Проверки | ruff 0.15.22, mypy 1.20.2 (strict), pytest 8.4.2, hypothesis 6.168 |

## Сделано и проверено

| Срез | Состояние | Доказательство |
|---|---|---|
| S00 Bootstrap | реализовано | миграции применены на PostgreSQL 17.2, 80 таблиц, 251 политика RLS, 7 constraint-триггеров |
| Денежное ядро `core/money.py` | проверено | `tests/unit/test_money.py` — 13 PASS, включая property-инвариант распределения |
| Календарь `core/calendar.py` | проверено | `tests/unit/test_calendar.py` — 18 PASS, A201–A211, A48/A49, A224 |
| Схема и RLS | проверено | `tests/integration/test_schema_constraints.py` — 11 PASS на настоящей PostgreSQL 17 |

## Реализовано, ещё не покрыто проверками

- `core/errors.py`, `core/ids.py`, `core/context.py`, `core/clock.py`, `core/logging.py`
- `config.py` с защитой профиля ADR-17 (проверено вручную, нужен тест)
- `db/session.py`, `db/uow.py`, `db/rls.py`
- `infra/security_log.py` — независимый журнал доступа
- `application/common.py`, `application/identity/actor.py`, `application/identity/security_change.py`
- `application/planning/periods.py` — ensure_periods
- `application/platform/queue.py` — очередь с арендой и fencing
- `application/ingestion/accept_update.py` — долговечный приём
- `api/app.py`, `api/routes/telegram.py`, `runtime/{cli,health,worker,scheduler}.py`

## Следующий шаг

1. Написать доменный сервис журнала `application/ledger/` (проведение, ревизии,
   распределения, движения счетов, возвраты, отмена, восстановление).
2. Написать `application/catalog/` (категории, люди, получатели, счета, метки).
3. Написать `application/onboarding/` (мастер и публикация бюджета).
4. Написать недостающие обработчики задач: `ingestion/process_event.py`,
   `delivery/dispatch.py`, `planning/rollover.py`, `maintenance/retention.py`.
5. Написать Telegram-адаптер `bot/` на aiogram 3.
6. Прогнать полный набор проверок, провести ревью среза S01/S02, обновить реестр.

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
```

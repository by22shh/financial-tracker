# Повторная проверка — доказательства и запуск

Отчёт: [IMPLEMENTATION_RECHECK_2026-09-13.md](/Users/Bayramov_N/Desktop/Other/financial-tracker/docs/IMPLEMENTATION_RECHECK_2026-09-13.md).

Проверен HEAD `47cad620b48e0fff9c6481d78c4c867eb500eb86` плюс изменения рабочей копии, зафиксированные в [scope.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/scope.json). Снимок приложения не исправлялся в ходе проверки. Новые тесты выполнялись отдельно от полного штатного прогона.

## Результаты

| Проверка | Итог | Артефакт |
|---|---|---|
| Форматирование, Ruff, mypy | PASS | [static.log](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/static.log) |
| Полный штатный pytest с performance и исходной таблицей | 482 passed, 0 failed, 0 skipped | [baseline-junit.xml](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/baseline-junit.xml) |
| Окончательная диагностика | 36 tests: **21 passed, 15 failed**, 0 errors, 0 skipped | [verified-recheck-junit.xml](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/verified-recheck-junit.xml), [лог](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/verified-recheck.log) |
| Проверка реестра | Принимает несуществующие test node/evidence | [traceability-probe.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/traceability-probe.json) |

Пройденные 21 случая — сценарии первого аудита. Пятнадцать отрицательных случаев расширяют проверку на гонки, редактирование сообщений, жизненный цикл возвратов, доставку после отзыва, расписание, импорт и очистку файлов.

## Повторить на текущей рабочей копии

Условия: существующий venv проекта и локальная PostgreSQL с тестовыми ролями, порт 55432. Другой локальный порт передаётся через `--pg-port`; пароль тестового PostgreSQL — через `FINTRACKER_TEST_PG_PASSWORD`.

```bash
/Users/Bayramov_N/Desktop/Other/financial-tracker/.venv/bin/python \
  /Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/run_recheck.py
```

[run_recheck.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/run_recheck.py) включает незакоммиченные изменения и новые файлы src/tests. Он копирует код во временный каталог, подключает venv и использует отдельную БД со случайным именем `fintracker_recheck_<uuid>`. Штатная pytest-фикстура удаляет эту тестовую БД после прогона. Рабочая финансовая БД не используется.

Настройки приложения `FINTRACKER_*` не наследуются; внешние AI/ASR выключены. В двух тестах задержки включаются только локальные ScriptedAIProvider. Telegram заменён RecordingSender, реальных сообщений нет. Для этих 36 сценариев приватная исходная таблица не требуется.

Каждый запуск создаёт новый каталог `run-<timestamp>-<suffix>` со scope, source hashes, pytest.log и JUnit. Временный снимок остаётся для просмотра. На проверенной версии pytest возвращает exit 1 с 15 диагностическими отказами. Это результат обнаружения ошибок, а не поломка runner. Для исправленной версии ожидаются проходящие инварианты; сам runner не принуждает результат к заранее заданным числам.

## Состав проверок

- [test_recheck_prior_v2.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/test_recheck_prior_v2.py) и [test_recheck_prior_v3.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/test_recheck_prior_v3.py): прежние утверждения, без RLS-подмены из первого аудита.
- [legacy_support.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/legacy_support.py): вспомогательные фабрики и прежние сценарии; копируется под именем `tests/integration/_recheck_legacy_support.py`.
- [extra_repros.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/extra_repros.py): 15 расширенных сценариев; копируется как `test_recheck_adversarial.py`.
- [prior-probe-integrity.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/prior-probe-integrity.json): сверка исходных прежних файлов с manifest первого аудита.
- [source-anchors.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/source-anchors.json): строки кода для проверки находок.

| Находка | Тесты |
|---|---|
| R-01 | `test_same_event_concurrently_creates_one_expense` |
| R-02 | `test_lease_lost_during_command_cannot_commit`, `test_expired_lease_cannot_be_renewed` |
| R-03 | `test_edited_message_does_not_add_second_expense` |
| R-04 | `test_deferred_reply_is_not_sent_after_member_removal` |
| R-05 | `test_restoring_old_refund_cannot_exceed_purchase`, `test_restored_refund_counts_in_remaining_limit` |
| R-06 | `test_new_budget_has_default_weekly_analysis` |
| R-07 | `test_background_slow_ai_does_not_break_transaction` |
| R-08 | `test_xlsx_message_reaches_import_handler` |
| R-09 | `test_runtime_retention_deletes_expired_receipt`, `test_budget_purge_also_removes_receipts` |
| R-10 | `test_restore_replays_deleted_workspace_state` |
| R-11 | `test_rejected_security_change_does_not_leave_fence`, `test_invite_expiring_after_preview_does_not_fence_budget` |
| R-12 | Отдельный `traceability-probe.json` |

Барьеры и monkeypatch в новых тестах используются для управляемого порядка гонок и наблюдения за маршрутизацией. Они не отключают проверки прав/RLS и не исправляют production-код. В R-10 имитируется откат строк состояния/ACL при сохранённом независимом журнале, не облачный PITR.

## Сохранённый контекст и ограничения

- [working-tree.patch](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/working-tree.patch) фиксирует отличия от HEAD.
- [captured-untracked/src/fintracker/application/intelligence/schedule.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/captured-untracked/src/fintracker/application/intelligence/schedule.py) сохраняет новый незакоммиченный модуль, который не входит в git diff.
- [performance.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/performance.json), [restore_drill.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/restore_drill.json), [extraction_accuracy.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/extraction_accuracy.json) скопированы из отдельного штатного прогона; пределы интерпретации описаны в отчёте.
- `recheck-junit.xml` и `recheck-final-junit.xml` — промежуточная работа над дополнительными проверками. Основной результат: **verified-recheck-junit.xml**. В промежуточных тестах исправлены проверка возврата, скрывавшая превышение из-за max(0, …), и имя вызываемой функции приглашения.
- Настоящие OpenAI/ASR/Telegram, хранилище в выбранной среде и промышленный PITR не проверялись.

Коммиты, push, deploy и исправления приложения в рамках повторной проверки не выполнялись.


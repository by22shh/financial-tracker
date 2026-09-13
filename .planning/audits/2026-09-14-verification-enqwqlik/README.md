# Материалы проверки 14 сентября 2026

Проверен commit `ff543194c6a0bc93e0e8b9d1388417b2018764b8`, исходная рабочая копия чистая. [Полный отчёт](/Users/Bayramov_N/Desktop/Other/financial-tracker/docs/IMPLEMENTATION_VERIFICATION_2026-09-14.md).

## Результат

| Набор | Файл | Результат |
|---|---|---|
| Формат, Ruff, mypy | `static.log` | PASS |
| Полный штатный pytest с измерениями | `baseline.log`, `baseline-junit.xml` | 499 passed, 0 skipped |
| Первый аудит | `first_audit.log`, `first_audit-junit.xml` | 21 passed |
| Второй аудит | `second_audit.log`, `second_audit-junit.xml` | 36 passed |
| Окончательный дополнительный набор | `verified-expanded.log`, `verified-expanded-junit.xml` | 10 failed, 1 passed; 0 errors/skipped |
| Прослеживаемость | `traceability-probes.json` | Контроль: exit 0; старая подмена: exit 1; пустой JUnit и отсутствие commit: ошибочный exit 0 |

Сводка отдельных test cases: `results-summary.json`. Контрольные суммы и версия исходников: `scope.json`, `manifest.json`. Точные места в проверенном коде: `source-anchors.json`.

## Соответствие новых проверок замечаниям

| Проверка в `test_verification3_cases.py` | Замечание | Исход |
|---|---|---|
| `test_immediate_reply_rechecks_revocation` | V-01 | FAIL |
| `test_repeated_photo_event_reuses_its_draft` | V-02 | FAIL: две карточки и две подтверждённые траты |
| `test_analysis_rechecks_authority_after_model[lease]` | V-03 | FAIL |
| `test_analysis_rechecks_authority_after_model[quarantine]` | V-03 | FAIL |
| `test_expired_lease_cannot_send_deferred_reply` | V-03 | FAIL |
| `test_analysis_resumes_after_process_interruption` | V-04 | FAIL |
| `test_api_start_does_not_require_migration_credentials` | V-05 | FAIL |
| `test_downgrade_keeps_previous_purge_function_usable` | V-06 | FAIL |
| `test_both_concurrent_executors_return_a_result` | V-07 | FAIL: одна трата, но второй исполнитель выбрасывает исключение |
| `test_successful_summary_emits_one_completion_event` | V-08 | FAIL |
| `test_edited_message_confirmation_updates_original_transaction` | Подтверждение R-03 | PASS: 450 → 600, одна операция |

V-09 подтверждается отдельными пробами checker в `traceability-probes.json`. Первичная копия окружения не содержала вспомогательных файлов реестра; это исправлено до окончательной пробы. В финальном результате неизменённые доказательства проходят, а старая подмена даёт ровно два целевых замечания. Ошибки подготовки не посчитаны дефектами приложения.

## Воспроизведение

Нужны `.venv` проекта и локальная PostgreSQL с ролями проекта на порту 55432. Запуск из корня репозитория:

```sh
.venv/bin/python .planning/audits/2026-09-14-verification-enqwqlik/run_additional_checks.py
.venv/bin/python .planning/audits/2026-09-14-verification-enqwqlik/run_traceability_probes.py
```

Первый runner копирует текущий код и тесты, включает незакоммиченные изменения и новые исходники, создаёт отдельную БД и запускает 11 дополнительных случаев. На проверенном commit ожидается 10 failed / 1 passed. После исправлений все проверяемые инварианты должны выполняться. Изменения внутреннего API могут потребовать адаптации вызовов тестовой обвязки; ослаблять утверждения не требуется.

Второй runner меняет только одноразовую копию реестра и доказательств. Ему нужен пригодный текущий baseline: если неизменённый checker уже отклоняет его, runner завершится с кодом 2. Код 1 runner означает, что одна из испорченных версий была ошибочно принята. После исправления ожидается код 0 runner и код 1 checker для каждой из трёх испорченных версий.

Предыдущие наборы запускаются их исходными runner-ами:

```sh
.venv/bin/python .planning/audits/2026-09-13/run_reproductions.py --commit HEAD
.venv/bin/python .planning/audits/2026-09-13-recheck-a63cxl89/run_recheck.py
```

Первый из этих runner-ов берёт commit; второй — текущую рабочую копию. В этой проверке рабочая копия была чистой. Набор на 36 случаев не подменяет RLS-загрузчик; исходный runner первого аудита сохранён без правки его исторической тестовой обвязки.

## Границы и сохранённые файлы

Все вызовы Telegram, модели и файлового скачивания в дополнительных тестах заменены локальными адаптерами. PostgreSQL, миграции, проверки доступа, расчёты и запись операций выполняются настоящим кодом. Подготовка тестовых данных использует OWNER; сами проверяемые пользовательские команды — обычные runtime-роли. Проверка V-05 отдельно обнаруживает использование OWNER внутри production-старта.

Набор проверяет устойчивость протоколов, а не качество OCR/ASR/GPT. В фото-пробе есть один вход Telegram и повтор после искусственного прерывания на отправке ответа; модель возвращает фиксированный корректный разбор. Обе карточки затем подтверждаются штатным сервисом, что выявляет повторное проведение.

`expanded.log` и `expanded-final.log` — промежуточные прогоны до расширения набора и усиления фото-сценария. Итоговым является только `verified-expanded.log` с 11 случаями. Его источник — сохранённый `test_verification3_cases.py`.

`performance.json`, `restore_drill.json`, `extraction_accuracy.json` получены новым полным прогоном. `container-inspection.json` — чтение существующего образа без сети; образ не пересобирался. Полный baseline использовал отдельный снимок и доступный локальный `source.xlsx` через ссылку только на чтение.

Продуктовые исходники, штатные тесты, прежние доказательства и git history не редактировались. Созданы только новый отчёт и файлы проверки. Настоящие финансовые данные в рабочей БД не изменялись.


## Исправления после аудита

Первоначальные результаты выше относятся к этапу независимой проверки до
исправлений. Все V-01…V-09 исправлены; отчёт —
`docs/IMPLEMENTATION_FIXES_2026-09-14.md`.

`run_fixed_checks.py` запускает `test_verification4_cases.py`: в двух сценариях
потери полномочий ожидается `TemporarilyUnavailable`, остальные утверждения
сохранены. Итог — `run-20260913T185005Z-7b48/`: 11 passed.

`run_traceability_probes.py` теперь копирует весь проверенный content manifest
в одноразовую копию: Git-only снимок не включал игнорируемые `.DS_Store`, хотя
manifest учитывает их байты. Проверки не отключаются. Итог —
`trace-run-20260913T185442Z.json`: контроль принят, три подмены отвергнуты.

Полный прогон исправленного кода: 546 passed, 0 skipped, checks и strict mypy
PASS. Образ пересобран и его 147 Python-файлов сопоставлены с исходниками.

# Доказательства аудита от 13 сентября 2026

Проверенный commit: `57ed972503abccc14238fe5951498e4b0e5ff78f`.

Основной результат: [полный отчёт](/Users/Bayramov_N/Desktop/Other/financial-tracker/docs/IMPLEMENTATION_AUDIT_2026-09-13.md).
Продуктовые исходники и штатные тесты не менялись. Проверки выполнялись в git archive с отдельными локальными тестовыми БД.

## Итоговые результаты

| Набор | Результат | Файлы |
|---|---|---|
| Форматирование/Ruff/mypy | PASS | [static.log](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/static.log) |
| Базовый pytest | 442 passed, 8 skipped | [baseline.log](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/baseline.log), [JUnit](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/baseline-junit.xml) |
| Все восемь первоначально пропущенных тестов | 8 passed | [performance-source.log](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/performance-source.log), [JUnit](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/performance-source-junit.xml) |
| Окончательный прогон v2/v3 через сохранённый runner | **21 failed, 0 passed, 0 errors, 0 skipped; 18,22 с** | [pytest.log](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/run-20260913T130021Z-69d1/pytest.log), [JUnit](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/run-20260913T130021Z-69d1/junit.xml) |
| Проверка фиктивных доказательств | Checker ошибочно принимает их | [traceability-probe.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/traceability-probe.json) |

Отрицательные результаты диагностик показывают нарушения ожидаемого поведения. После исправлений эти сценарии должны проходить с прежними проверками результата.

## Повторить диагностику

Нужны установленное окружение проекта `.venv`, доступ к указанному Git commit и локальная PostgreSQL из среды разработки. Скрипт использует localhost:55432 и роли существующей тестовой среды. Порт можно изменить параметром `--pg-port`, пароль — переменной `FINTRACKER_TEST_PG_PASSWORD`.

Из любого каталога:

```bash
/Users/Bayramov_N/Desktop/Other/financial-tracker/.venv/bin/python \
  /Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/run_reproductions.py
```

[run_reproductions.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/run_reproductions.py) создаёт архив commit во временном каталоге, подключает venv и копирует три диагностических файла под именами, нужными для импортов. Запускаются только v2/v3; первый файл используется ими как библиотека сценариев.

Тестовая БД получает случайное имя `fintracker_audit_repro_<uuid>` и удаляется штатной фикстурой после завершения. База приложения не используется. Наследуемые `FINTRACKER_*` настройки приложения отбрасываются; внешние AI/ASR отключены. Для проверки медленного AI включается только локальный ScriptedAIProvider. Отправка Telegram подменена RecordingSender. Приватная исходная таблица для этих 21 сценария не нужна.

Runner сохраняет новый каталог `run-<timestamp>-<suffix>` со scope, JUnit и pytest.log. Временный снимок кода остаётся для просмотра; его путь записан в scope. На исходном commit нормальный результат runner — **exit 1** с 21 диагностическим падением. Exit 0 или пропуски без ожидаемых результатов нельзя считать воспроизведением аудита.

После исправлений для проверки нового commit:

```bash
/Users/Bayramov_N/Desktop/Other/financial-tracker/.venv/bin/python \
  /Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/run_reproductions.py \
  --commit HEAD
```

Это проверяет только содержимое commit; незакоммиченные правки не включаются. Если внутренние интерфейсы изменятся, диагностические тесты потребуется адаптировать, сохраняя проверяемые инварианты. После исправления AUD-01 убрать подмену загрузчика в четырёх downstream-сценариях и проверять полный путь без неё.

## Привязка сценариев к находкам

Все названия ниже — pytest-функции в [repros_v2.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/repros_v2.py) или [repros_v3.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/repros_v3.py).

| Находка | Сценарии / доказательство |
|---|---|
| AUD-01 | `test_actual_worker_routes_accepted_start`, `test_actual_scheduler_discovers_active_budgets`, `test_runtime_retention_clears_expired_private_draft` |
| AUD-02 | `test_downstream_retry_no_duplicate` |
| AUD-03 | `test_expired_lease_is_invalid`, `test_downstream_stale_lease_cannot_post` |
| AUD-04 | `test_removed_member_cannot_finish_inflight_write` |
| AUD-05 | `test_bad_delete_confirmation_does_not_fence_budget` |
| AUD-06 | `test_completed_revoke_survives_restored_old_acl` |
| AUD-07 | `test_ai_latency_within_provider_timeout_does_not_kill_transaction` |
| AUD-08 | `test_transfer_semantics_survive_confirmation` |
| AUD-09 | `test_void_refund_restores_refundable_amount` |
| AUD-10 | `test_edit_versions_accept_third_update` |
| AUD-11 | `test_downstream_failed_reply_is_retried` |
| AUD-12 | `test_downstream_group_is_private` |
| AUD-13 | `test_import_export_actions[exp:xlsx]`, `[exp:csv]`, `[imp:start]` |
| AUD-14 | Статический анализ: `run_analysis` не вызывается в production-коде, в scheduler/registry нет соответствующего запуска |
| AUD-15 | `test_deleted_budget_financial_data_is_purged_after_deadline` |
| AUD-16 | `test_invite_secret_is_not_retained_in_global_job` |
| AUD-17 | `traceability-probe.json`, исходники генератора/проверяющего скрипта, тест A96 и определения метрик |
| AUD-18 | `test_quarantined_budget_does_not_show_financial_history` |

Четыре `test_downstream_*` в v2 обходят только ошибку первоначальной загрузки payload через установку нужного RLS-контекста в тестовом загрузчике. Остальная бизнес-логика не изменена. Это изоляция одной ошибки для проверки следующих, а не исправление приложения.

В AUD-06 откат имитируется возвратом строк доступа к состоянию до завершённого отзыва, при сохранённом независимом журнале. Облачный PITR в этой проверке не выполняется.

В окончательной проверке AUD-15 в прошлое перенесены и дата удаления Workspace, и срок purge_after в BudgetDeletionRecord. В проверке карантина устранена ложноположительная проверка суммы: теперь учитываются неразрывные пробелы форматирования. Исторический результат этого теста PASS не подтверждает защиту; финальный тест показывает раскрытие истории (AUD-18). Промежуточные логи v2/v3 и предыдущего runner сохранены; основным доказательством является окончательный совместный прогон, указанный выше.

## Дополнительные артефакты и пределы

- [performance.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/performance.json): локальные показатели отдельных тестовых путей, не задержка реального Telegram-бота.
- [restore_drill.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/restore_drill.json): небольшой локальный dump/restore. Поле RPO рассчитано некорректно; см. AUD-17.
- [extraction_accuracy.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/extraction_accuracy.json): 15 текстовых примеров детерминированного разбора и отдельные защитные входы; не оценка GPT, ASR или чеков.
- [source-anchors.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/source-anchors.json): строки исходного кода для сверки отчёта с commit.
- [scope.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/scope.json): исходный снимок и имя тестовой БД.
- [repros.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13/repros.py), `repros.log`, `repros-junit.xml`: первый исследовательский прогон. Его четыре проходивших downstream-теста не достигали нужного действия из-за AUD-01; использовать итоговые v2/v3.

Traceability probe сделан только в архивной копии: у A01 заменены ссылка на test node внутри существующего файла и ссылка на evidence на несуществующие, затем запущен `.planning/tools/check_traceability.py`. Получен exit 0. Исходный реестр копии после проверки восстановлен. Рабочий реестр не редактировался.

Внешние платные запросы, реальные сообщения, deploy, Git commit/push и изменения рабочих финансовых данных не выполнялись.

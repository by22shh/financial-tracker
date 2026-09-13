# Повторная проверка исправлений — 13 сентября 2026

**Результат:** прежние 21 диагностический сценарий проходят. Все 482 штатных теста также проходят. При расширении проверки подтверждены **12 оставшихся проблем: 1 P0 и 11 P1**. Они подтверждаются 15 дополнительными сценариями и отдельным экспериментом с реестром требований. Приёмку приложения пока закрывать нельзя.

Проверен HEAD `47cad620b48e0fff9c6481d78c4c867eb500eb86` **вместе с незакоммиченными изменениями** в retention.py, scheduler.py, worker.py и новым intelligence/schedule.py. Снимок зафиксирован 13 сентября в 14:34:28 UTC / 21:34:28 по Новосибирску. Описание и контрольные суммы: [scope.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/scope.json); изменения относительно HEAD: [working-tree.patch](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/working-tree.patch). Содержимое незакоммиченного нового модуля сохранено в каталоге доказательств.

Продуктовые исходники и штатные тесты в рабочей копии не менялись. Использовались отдельные снимки кода и уникальные тестовые БД в локальной PostgreSQL. OpenAI, ASR и настоящий Telegram не вызывались.

## 1. Что подтверждено проверками

| Набор | Результат | Доказательство |
|---|---|---|
| Форматирование, Ruff, mypy | PASS; 142 файла приложения прошли mypy | [static.log](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/static.log) |
| Полный штатный pytest, включая performance/restore и исходный XLSX | **482 passed, 0 failed, 0 skipped**; 340,87 с | [baseline-junit.xml](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/baseline-junit.xml), [baseline.log](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/baseline.log) |
| 21 сценарий предыдущего аудита | **21 passed**, без прежней подмены RLS-контекста | [verified-recheck-junit.xml](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/verified-recheck-junit.xml) |
| 15 расширенных сценариев | **15 failed**, 0 errors, 0 skipped | [verified-recheck-junit.xml](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/verified-recheck-junit.xml), [verified-recheck.log](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/verified-recheck.log) |
| Подмена ссылки на test node и evidence у A01 | Checker ошибочно возвращает exit 0 | [traceability-probe.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/traceability-probe.json) |

Последние два набора pytest выполнялись вместе: **36 tests, 21 passed, 15 failed**, 34,61 с. Ошибок подготовки тестов в окончательном прогоне нет. Отрицательные результаты — нарушения проверяемых инвариантов, а не преднамеренно перехваченные исключения, посчитанные успешными.

Исходники прежних трёх диагностических файлов сверены с manifest первого аудита: совпадают. В копиях для повторного прогона переименован вспомогательный модуль и убрана fixture, подменявшая RLS-загрузчик. Проверяемые утверждения старых тестов сохранены. Материалы: [prior-probe-integrity.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/prior-probe-integrity.json), [probe-scope.json](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/probe-scope.json).

Новые тесты используют настоящую БД и штатные production-сервисы. Для точного воспроизведения гонок тестовые барьеры задают порядок исполнения: например, аренда отзывается между предварительной проверкой и продолжением команды. Тест XLSX проверяет достижимость импортного обработчика через spy; он не выдаётся за успешную проверку полного парсинга. Медленный AI имитируется локальным провайдером с задержкой 11 секунд.

## 2. Оставшиеся проблемы

| ID | Приоритет | Результат проверки | Связь с первым аудитом |
|---|---|---|---|
| R-01 | P0 | Два параллельных исполнения одного входа создают две траты | AUD-02 |
| R-02 | P1 | Потеря аренды во время команды не предотвращает запись; истёкшая аренда продлевается | AUD-03 |
| R-03 | P1 | Правка Telegram-сообщения добавляет ещё одну трату | AUD-10, TECH-02 |
| R-04 | P1 | Отложенный ответ доставляет историю уже исключённому участнику | Новый путь после AUD-11 |
| R-05 | P1 | Восстановленный возврат не занимает лимит; суммарный возврат может превысить покупку | AUD-09 |
| R-06 | P1 | Анализ по расписанию не запускается при отсутствующей строке настроек | AUD-14 |
| R-07 | P1 | Фоновый AI-анализ снова ожидает модель внутри DB-транзакции | AUD-07, AUD-14 |
| R-08 | P1 | XLSX из чата не достигает нового обработчика импорта | AUD-13 |
| R-09 | P1 | Чеки не удаляются по сроку и остаются после purge бюджета | AUD-01, AUD-15 |
| R-10 | P1 | Восстановление ACL не применяет зафиксированное удаление бюджета | AUD-06 |
| R-11 | P1 | Отклонённое вступление/изменение доступа оставляет бюджет fenced | AUD-05 |
| R-12 | P1 | Реестр verified всё ещё принимает несуществующие доказательства | AUD-17 |

### R-01 · P0 · Проверка существующего черновика не обеспечивает конкурентную идемпотентность

**Код:** [service.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/service.py:365), [service.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/service.py:466).

Поиск готового Draft выполняется в первой короткой транзакции. Создание нового Draft — в другой, без повторной атомарной проверки идентичности входа. Два исполнителя успевают увидеть «черновика нет» и оба создают собственные кандидаты. Последовательное повторение после commit теперь безопасно, но это не устраняет конкурентный случай.

**Воспроизведение:** два вызова `record_free_text` с одним inbound_event_id; барьер поставлен перед созданием Draft, после обоих предварительных чтений. Получено **2 Transaction вместо 1**. Тест: `test_same_event_concurrently_creates_one_expense`.

**Нужно:** долговечный уникальный идентификатор результата исходного входа и атомарное получение/создание кандидатов; конфликт должен возвращать существующий результат. Уникальность случайных UUID кандидатов не заменяет уникальность пользовательского действия. Операция проведения и фиксация её идемпотентного результата должны согласованно завершаться в одной транзакции.

### R-02 · P1 · Проверка аренды до команды не защищает её последующий commit

**Код:** [process_event.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ingestion/process_event.py:376), [process_event.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ingestion/process_event.py:390), [queue.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/platform/queue.py:172).

Входной обработчик проверяет lease до `handle` и после его завершения. Финансовый commit находится между этими проверками в других транзакциях. При утрате lease во время выполнения последняя проверка лишь отказывается фиксировать результат входного события; деньги уже изменены.

**Воспроизведение:** после первой проверки сменить token, затем продолжить штатную бизнес-команду. Получена одна трата, хотя старый исполнитель уже потерял аренду. Тест: `test_lease_lost_during_command_cannot_commit`.

Отдельно `renew_lease` проверяет token и state, но не прежний lease_until. Тест `test_expired_lease_cannot_be_renewed` получает True для заведомо истёкшей аренды.

**Нужно:** проверка права на результат в транзакции его записи, согласованный порядок блокировок и запрет возобновления истёкшего lease. Идемпотентность R-01 остаётся необходимой независимо от механизма аренды.

### R-03 · P1 · Исправлен приём редакций сообщения, но не их финансовая семантика

**Код:** [accept_update.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ingestion/accept_update.py:206), [service.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/service.py:466).

Последовательные edited_message больше не конфликтуют по постоянной версии 1. Однако каждой редакции соответствует новый InboundEvent, а свободный ввод использует его ID как идентичность нового черновика. Связь с уже проведённой тратой исходного сообщения не применяется.

**Воспроизведение:** отправить «продукты 450», дождаться записи, затем изменить то же Telegram-сообщение на «продукты 600». В журнале **две операции**, то есть 1 050 ₽ суммарно. Тест: `test_edited_message_does_not_add_second_expense`. Здесь используется настоящий путь intake → claim → worker; RLS-подмена не нужна.

**Нужно:** редакция должна адресовать исходный логический ввод. После проведения — показать изменение связанной операции либо запросить подтверждение; не проводить новую независимую трату автоматически. Проверить несколько редакций и их перестановку при доставке.

### R-04 · P1 · Новый deliver_reply отправляет приватный текст без актуальной проверки доступа

**Код:** [process_event.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ingestion/process_event.py:299).

Отложенная доставка извлекает chat_id и готовые messages из Job.payload и сразу вызывает sender. Членство, generation, состояние бюджета и актуальное право на эту доставку не проверяются. Существующие ограничения NotificationDelivery не покрывают новый тип jobs.

**Воспроизведение:** участник запрашивает историю; немедленная отправка возвращает временную ошибку; задача deliver_reply забирается исполнителем; администратор исключает участника; доставщик продолжает работу. Исключённый участник получает дату, сумму **98 765 ₽** и категорию расхода. Тест: `test_deferred_reply_is_not_sent_after_member_removal`. Все данные синтетические, отправитель — RecordingSender.

**Нужно:** проверять адресата, актуальное членство/generation, quarantine/fence и право lease непосредственно перед отправкой; отменять такие доставки при отзыве. Предпочтительно использовать общую защищённую модель доставки. Финансовый текст в глобальном Job.payload также требует отдельной проверки изоляции и сроков хранения.

### R-05 · P1 · Исправление void возврата не завершает его жизненный цикл

**Код:** [service.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:597), [service.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:718), [service.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:764).

При void связь refund_of переводится в cancelled. При restore сама транзакция снова становится posted, однако связь обратно не активируется. Проверка доступного возврата по активным ссылкам перестаёт видеть восстановленный денежный эффект.

Два воспроизведения:

- Покупка 1 000 ₽ → возврат 1 000 ₽ → void → restore. Доступная сумма следующего возврата ошибочно равна **1 000 ₽ вместо 0**. `test_restored_refund_counts_in_remaining_limit`.
- Покупка 1 000 ₽ → первый возврат → void → второй возврат 1 000 ₽ → restore первого. В БД проведены возвраты на **2 000 ₽**. `test_restoring_old_refund_cannot_exceed_purchase`.

Во втором тесте проверяется сумма текущих проведённых ревизий возвратов. Проверять только неотрицательность refundable_minor недостаточно: это свойство ограничивает результат снизу нулём и скрывает превышение.

**Нужно:** при восстановлении под общей блокировкой проверить свободный лимит исходной покупки и согласовать posted/status/amount ссылок с ревизией. Исправления суммы возврата также должны обновлять эту связь. Отказ не должен оставлять частично восстановленный эффект.

### R-06 · P1 · Настройки анализа по умолчанию создаются только как незаписанный ORM-объект

**Код:** [schedule.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/intelligence/schedule.py:76), [intelligence.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/db/models/intelligence.py:200).

При отсутствии AnalysisPreference создаётся Python-объект, но не строка в БД. Его server_default значения ещё не применены: weekly_enabled, plan_preparation_enabled и closing_enabled равны None. Все ветки расписания пропускаются.

**Воспроизведение:** бюджет без строки preferences; воскресенье 20:00 в его часовом поясе, внутри действующего периода. Недельный анализ не появляется в due. Тест: `test_new_budget_has_default_weekly_analysis`.

**Нужно:** сохранять настройки при создании бюджета либо применять явные доменные defaults до обращения к БД. Проверить фактическую постановку jobs с настройками по умолчанию, сохранёнными пользовательскими настройками и после перезапуска.

### R-07 · P1 · Фоновый анализ повторяет уже исправленную ошибку долгой транзакции

**Код:** [schedule.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/intelligence/schedule.py:170), [schedule.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/intelligence/schedule.py:198), [analysis.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/intelligence/analysis.py:429).

Для текстового ввода ожидание AI вынесено из транзакции. Новый handle_run_analysis открывает WORKER-сессию и вызывает run_analysis внутри неё. run_analysis читает и пишет БД, затем ожидает провайдера в этой же транзакции.

**Воспроизведение:** бюджет с подтверждённой полнотой и расходами; корректный ScriptedAIProvider отвечает через 11 секунд. Завершение анализа падает с **IdleInTransactionSessionTimeout**. Тест: `test_background_slow_ai_does_not_break_transaction`. Проверка достигает вызова AI; это не отказ из-за недостаточных данных.

**Нужно:** подготовить и зафиксировать снимок/задание, закрыть транзакцию, вызвать AI, затем валидировать и сохранить результат в новой короткой транзакции с контролем версии/доступа/lease. Одинаковое правило должно применяться ко всем модельным путям.

### R-08 · P1 · XLSX всё ещё маршрутизируется в обработчик изображений

**Код:** [service.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/service.py:84), [media.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/media.py:57), [io_flow.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/io_flow.py:140).

Кнопки и handle_table_document добавлены. Но production-вызовов этого обработчика нет. Любой DOCUMENT идёт в handle_media; MIME XLSX отклоняется как неподдерживаемое изображение.

**Воспроизведение:** передать в штатный conversation.handle документ с XLSX MIME. Ответ: «Формат … пока не поддерживается. Поддерживаются JPEG, PNG и WebP». Импортный обработчик не вызывается. Тест: `test_xlsx_message_reaches_import_handler`.

**Нужно:** отдельный маршрут табличных документов до image-проверок, с общими ограничениями безопасности файлов и проверками членства. Затем проверить пользовательский цикл «Импорт» → файл → preview → подтверждение → сверка результата. Прохождение кнопки imp:start само по себе этот цикл не подтверждает.

### R-09 · P1 · Очистка финансовых таблиц не очищает фотографии чеков

**Код:** [retention.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/maintenance/retention.py:177), [retention.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/maintenance/retention.py:197), [0009_runtime_maintenance.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/db/migrations/versions/0009_runtime_maintenance.py:94).

Новая SECURITY DEFINER-функция исправляет очистку приватных Draft. Чтение/изменение Attachment остаётся в глобальной WORKER-сессии без контекста и не видит защищённые строки. Список таблиц purge_workspace_data также не содержит attachments; серверные объекты чеков в этом процессе не удаляются.

**Воспроизведения:**

- Просроченный на день чек после реального retention остаётся ready: `test_runtime_retention_deletes_expired_receipt`.
- Штатно удалить бюджет, перенести deleted_at и purge_after в прошлое, выполнить purge. Чек с исходным сроком 30 дней остаётся ready: `test_budget_purge_also_removes_receipts`.

**Нужно:** ограниченное фоновое перечисление вложений, удаление объектов с повтором и фиксацией состояния, отдельная обработка всех файлов удаляемого бюджета. Завершение purge подтверждать после очистки данных и объектов, а не только перечисленных SQL-таблиц. Обычным runtime-ролям не выдавать общий BYPASSRLS.

### R-10 · P1 · Replay применяет часть ACL-снимка, но пропускает состояние удалённого бюджета

**Код:** [security_change.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/identity/security_change.py:307), [security_change.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/identity/security_change.py:348).

Последний committed-снимок теперь читается, и исходный тест отзыва участника проходит. Но _apply_proven_access обновляет только поля членств, acl_revision и admin_user_id. snapshot.state игнорируется; восстановление признаков удаления и tombstone не реализовано этой функцией.

**Воспроизведение:** штатно удалить бюджет; вернуть Workspace к старому active/acl_revision=1; сохранить независимый журнал и вызвать replay. Workspace остаётся **active**, хотя доказанный снимок относится к удалению. Тест: `test_restore_replays_deleted_workspace_state`.

Replay при этом устанавливает quarantine. Поэтому тест не доказывает немедленную доступность истории после его вызова; нарушение — невосстановленное удаление и возможность ошибочно вернуть такой бюджет в работу при снятии карантина. Дополнительно поиск production-вызовов resume_or_quarantine находит только определение: автоматическая сверка перед запуском восстановленного приложения не подключена.

**Нужно:** воспроизводить весь необходимый контракт снимка, включая удаление и поколения членства; запускать сверку до доступа/очередей. Проверять это на завершённых отзывах, передаче администратора и удалении после точки восстановления. Облачный PITR в данном аудите не выполнялся.

### R-11 · P1 · Precheck удаления не заменяет обработку отказов протокола SecurityChange

**Код:** [security_change.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/identity/security_change.py:123), [security_change.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/identity/security_change.py:236), [invites.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/identity/invites.py:321), [invites.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/identity/invites.py:360).

Опечатка в названии теперь отклоняется до fence. Другие ожидаемые ошибки по-прежнему могут возникнуть после фиксации fence/prepared; терминального перехода отказа и снятия именно этой блокировки нет.

**Воспроизведения:**

- remove_member с отсутствующим target возвращает DomainError, но оставляет security_fence: `test_rejected_security_change_does_not_leave_fence`.
- Приглашение действительно на preview, затем истекает до apply. accept_invite корректно отказывает во вступлении, но **весь бюджет остаётся fenced**: `test_invite_expiring_after_preview_does_not_fence_budget`. Тест задаёт этот порядок событий; проверяемый протокол не подменяется.

**Нужно:** определить и реализовать исходы rejected/aborted и восстановление после каждого шага. Предварительная проверка полезна, но не устраняет изменение условий между проверкой и commit. При неопределённом внешнем commit нельзя снимать fence без доказательства исхода.

### R-12 · P1 · Прослеживаемость требований по-прежнему даёт ложный PASS

**Код:** [check_traceability.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/tools/check_traceability.py:99), [check_traceability.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/tools/check_traceability.py:107).

У A01 в копии реестра заменены ссылка на test node и путь evidence на несуществующие. Скрипт вернул exit 0, «Прослеживаемость в порядке», 483 verified. Исходный файл копии восстановлен. Рабочий реестр не редактировался.

Прежние ограничения оценок AI и restore также сохраняются: детерминированный корпус из 15 текстов не доказывает качество GPT/ASR/чеков, а длительность dump не измеряет RPO. Соответствующие инструменты не изменены относительно первого аудита.

**Нужно:** сверять test node с collection, outcome с фактическим прогоном, наличие/целостность evidence и commit проверенного кода. Разделять результаты модульных, runtime, живых интеграционных и эксплуатационных проверок. Общий зелёный pytest не закрывает отсутствующий пользовательский путь.

## 3. Что произошло с каждым пунктом первого аудита

«Пройден» ниже означает подтверждение конкретного прежнего сценария, а не автоматическое закрытие всех инвариантов функции.

| Первый аудит | Повторная проверка |
|---|---|
| AUD-01 | Вход, обнаружение бюджетов scheduler и очистка Draft пройдены. Для вложений остаётся R-09. |
| AUD-02 | Последовательный повтор после commit пройден. Параллельный случай нарушен: R-01. |
| AUD-03 | Старый token до начала команды и истёкший срок распознаются. Утрата во время команды и renew нарушены: R-02. |
| AUD-04 | Запись категории со старым Actor после исключения отвергается; проверенный дефект устранён. |
| AUD-05 | Неверное имя при удалении больше не оставляет fence. Другие отказы протокола: R-11. |
| AUD-06 | Завершённый отзыв участника воспроизводится. Удаление и включение replay в восстановление: R-10. |
| AUD-07 | Текстовый AI-разбор с задержкой 11 с проходит. Фоновый путь нарушен: R-07. |
| AUD-08 | Перевод больше не преобразуется в расход: неподдержанный путь безопасно отклоняется. |
| AUD-09 | Void освобождает лимит возврата. Restore нарушен: R-05. |
| AUD-10 | Последовательные редакции принимаются. Их финансовая обработка нарушена: R-03. |
| AUD-11 | Временный сбой оставляет отдельную долговечную доставку. Отзыв доступа для неё нарушен: R-04. |
| AUD-12 | Групповой вход получает нейтральный ответ; прежнее раскрытие списка бюджетов не воспроизводится. |
| AUD-13 | Все три кнопки подключены; отправка экспорта реализована. Приём XLSX остаётся недоступен: R-08. |
| AUD-14 | Добавлены scheduler/worker-связи с анализом, но настройки по умолчанию и долгий AI-путь нарушены: R-06/R-07. |
| AUD-15 | Прежний тест очистки Transaction проходит. Чеки после удаления остаются: R-09. |
| AUD-16 | Открытый код приглашения не хранится в Job; прежний сценарий пройден. |
| AUD-17 | Не устранён: R-12. |
| AUD-18 | Новый запрос истории при quarantine отклоняется. Для ранее подготовленной доставки нужен общий контроль R-04. |

## 4. Приоритет следующего исправления

1. **Деньги и исполнение:** R-01/R-02/R-03/R-05. Единая идемпотентность входа, контроль lease при commit, редакции исходного сообщения и полный цикл возврата.
2. **Доступ и восстановление:** R-04/R-10/R-11. Повторная проверка адресата доставки, полный replay и восстановимые отказы SecurityChange.
3. **Законченные пользовательские пути:** R-06/R-07/R-08/R-09. Сохранённое расписание, AI вне транзакции, доставка XLSX в импорт и удаление объектов чеков.
4. **Доказательства:** R-12. Проверка фактических результатов вместо одних ссылок и маркеров.

Архитектуру целиком переписывать не требуется. Исправления должны завершить общие правила транзакций, прав и жизненного цикла данных во всех вызывающих путях. Нельзя ограничиваться изменениями, достаточными только для прежнего конкретного теста.

## 5. Воспроизведение и границы выводов

[Инструкции и каталог доказательств](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/README.md) · [Изолированный runner](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/run_recheck.py) · [15 расширенных проверок](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-13-recheck-a63cxl89/extra_repros.py)

Runner включает текущие незакоммиченные изменения и новые файлы src/tests. Он создаёт отдельный снимок и уникальную тестовую БД, сохраняет исходники приложения без редактирования и запускает 36 диагностических случаев. На проверенном снимке ожидаются 21 passed и 15 failed. После исправлений утверждения о финансовых и защитных инвариантах должны проходить; изменения внутреннего API могут потребовать адаптации только тестового вызова.

Первичный расширенный прогон использовал недостаточную проверку остатка возврата и неверное имя функции в дополнительном тесте приглашения. Они исправлены; в окончательном verified-recheck-junit.xml подтверждены фактическая сумма проведённых возвратов и ожидаемый доменный отказ приглашения с оставшимся fence. Промежуточные логи не используются как основание окончательного статуса.

Реальные устройства Telegram, внешний OpenAI/ASR, выбранное облачное хранилище и промышленный PITR остаются за пределами выполненных проверок. Отсутствие новой находки по другой функции не означает доказательства её полной корректности. Исправления в продуктовый код в ходе этой проверки не вносились.


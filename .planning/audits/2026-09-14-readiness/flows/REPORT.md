# Проверка сквозных пользовательских сценариев

Проверен `7a56769` в `/Users/Bayramov_N/Desktop/Other/financial-tracker`. Исходники и штатные тесты не изменены. Локальных `.codex/skills` / `.agents/skills` нет. Scope: S01/S03/S05/S06/S07/S09 и их Telegram-потребители по плоскому `.planning/ROADMAP.md`; каталог фаз отсутствует.

**Вердикт: не готово. 16 новых диагностик воспроизводят нарушения, включая частичное сохранение пакета, изменение другой операции и выдачу файла исключённому участнику.** Ниже 10 групп BLOCKER и одна WARNING. Приоритет P1/P2 — срочность аудита, а не этап продукта.

## Доказательства

| Окончательный набор | Результат | Материалы |
|---|---|---|
| Основные пользовательские цепочки | 11 failed; 0 errors/skipped; 8.15 с | `tests.log`, `junit.xml` |
| Экспорт, передача роли, лимит | 3 failed; 0 errors/skipped; 3.75 с | `additional-tests.log`, `additional-junit.xml` |
| Подтверждение неполного пакета | 1 failed; 0 errors/skipped; 2.12 с | `batch-final-tests.log`, `batch-final-junit.xml` |
| Смешанный чек | 1 failed; 0 errors/skipped; 2.04 с | `receipt-tests.log`, `receipt-junit.xml` |

Все 16 разных случаев находятся в [test_flow_readiness.py](/Users/Bayramov_N/Desktop/Other/financial-tracker/.planning/audits/2026-09-14-readiness/flows/test_flow_readiness.py). Assertions описывают требуемый результат; xfail нет. PostgreSQL, миграции, RLS, runtime-сессии и бизнес-команды настоящие. OWNER используется для подготовки/проверки синтетических данных. Telegram и AI — локальные адаптеры; оплаченных запросов нет. Каждый запуск создаёт и удаляет отдельную БД с именем `fintracker_readiness_flows_260914_0N`.

Даты фикстур: обычные `BotUser` отправляют сообщения с `received_at=2026-09-12 09:00 UTC`; бюджет — 10 сентября–9 октября 2026, timezone `Asia/Novosibirsk`. Плановый платёж имеет дату 12 сентября 2026. Проверка reply-to использует время запуска для принятых update. Выдача экспорта и вычисление сегодняшней даты выполняются production-кодом на 14 сентября 2026 по timezone бюджета. Реальные OCR/ASR, качество распознавания и живые Telegram-клиенты не проверялись; scripted-ответы доказывают ошибку последующей интеграции даже при корректном распознавании.

`batch-tests.log` / `batch-junit.xml` — предварительная проба детерминированного распознавания, которая остановилась раньше проверки атомарности: второй пункт без суммы не попал в пакет. В итоговые 16 она **не включена**. Окончательный сценарий использует допустимый ответ ScriptedAIProvider из двух кандидатов и достигает реального подтверждения/commit.

Повтор всех окончательных диагностик:

```sh
FINTRACKER_TEST_DB=fintracker_readiness_flows_reproduce_260914 .venv/bin/python -m pytest -p tests.conftest -p tests.acceptance.conftest .planning/audits/2026-09-14-readiness/flows/test_flow_readiness.py -q --tb=short
```

## Подтверждённые разрывы

### F-01 · P1 · BLOCKER: неполный пакет частично проводится, хотя ответ утверждает обратное

Сценарий: AI вернул «Бензин 3000» и «Продукты, сумма неизвестна»; пользователь нажимает предложенную кнопку «Записать». В ответе: «У кандидата нет суммы или даты. Запись не проведена, черновик сохранён». В БД уже есть проведённая операция 3000 ₽; один кандидат `posted`, второй `needs_clarification`.

[entry.py:718](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/entry.py:718) последовательно проводит кандидатов. После ошибки второго [sections.py:428](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/sections.py:428) ловит `ValidationFailed` **внутри** общей `session_scope`, возвращается нормально и тем самым фиксирует первую операцию. Перед записью всего пакета нет общей валидации или отката через savepoint.

Тест: `test_confirm_incomplete_batch_never_partially_posts`. Требования: **FR-11, FR-20, A05**, атомарность пакета. Штатный `test_a05_incomplete_batch_stays_draft` лишь читает бюджет, не нажимая подтверждение, и не проверяет этот путь. Отдельно отсутствует пользовательское исключение одного кандидата: UI содержит только действия над всем draft; `excluded` читается в `post_draft`, но нигде не присваивается пользовательским обработчиком.

### F-02 · P1 · BLOCKER: ответ на старую карточку правит другую операцию

Сценарий проходит через настоящий приём Telegram update → очередь → `handle_process_inbound_event` → `RecordingSender`: записать 450 ₽, затем 800 ₽, ответить **на карточку 450 ₽** «Добавь комментарий: первая покупка». Результат: 450 ₽ остаются без комментария, комментарий появляется у 800 ₽. Никакого запроса выбора нет.

[process_event.py:382](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ingestion/process_event.py:382) получает Telegram `message_id`, но не сохраняет его связь с отображаемой операцией. [corrections.py:121](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/corrections.py:121) ищет ответ только среди `NotificationDelivery`; у немедленной авторской карточки такой строки нет. Затем [corrections.py:188](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/corrections.py:188) безусловно выбирает последнюю операцию автора. Карточки истории отправляются тем же немедленным путём.

Тест: `test_reply_to_old_author_card_does_not_edit_latest_record`. Требования: **FR-33, FR-87, A124**. Команды отмены и переноса категории используют тот же выбор target; их опасность следует из общего пути, отдельно они здесь не исполнялись.

### F-03 · P1 · BLOCKER: экспорт отправляется после завершённого отзыва доступа

Сценарий: участник нажимает экспорт CSV; после построения снимка администратор штатным `remove_member` исключает его, SecurityChange завершается с новой ACL revision; обработчик продолжает работу и отправляет этому уже исключённому участнику файл с финансовой операцией. Ответ сообщает об успешной выдаче.

[io_flow.py:84](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/io_flow.py:84) проверяет membership только до снимка. После завершения сессии [io_flow.py:87](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/io_flow.py:87) сериализует данные и [io_flow.py:96](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/io_flow.py:96) напрямую вызывает `send_document`. Исправленный контроль немедленных текстовых ответов сюда не применяется.

Тест: `test_export_rechecks_member_after_snapshot`. Подмена ставит контролируемую границу после настоящего `build_snapshot`, затем выполняет настоящее исключение участника; результат отправителя не подменяется на успех вручную. Требования: **SEC-05, SEC-08, FR-67**, актуальная проверка доступа при выдаче. Наличие отдельного HTTP download endpoint не требуется и само по себе не является замечанием.

### F-04 · P1 · BLOCKER: один смешанный чек превращается в несколько покупок

Сценарий: чек 1400 ₽ с двумя распознанными распределениями — продукты 1000 ₽, дом 400 ₽. Нажать «Записать». Ответ: «Записано операций: 2». В БД два `Transaction`, у каждого по одному `Allocation`, вместо одной операции и двух распределений.

[media_pipeline.py:391](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/intelligence/media_pipeline.py:391) создаёт отдельный `CandidateFields` на каждую строку распределения, а [entry.py:723](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/entry.py:723) проводит каждый кандидат отдельной транзакцией. Проверенная сумма совпадает, но единое событие покупки потеряно: отдельно меняются число операций, отмена и связи с возвратом.

Тест: `test_mixed_receipt_is_one_transaction_with_two_allocations`. Требования: **FR-14, FR-16, A22**. В реестре FR-16 подтверждён тестом чистой функции распределения, без прохождения media → candidates → ledger.

### F-05 · P1 · BLOCKER: «Повторить», «Да, оплачено» и «Изменить» не обслуживают черновик

[media_pipeline.py:297](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/intelligence/media_pipeline.py:297) и [media_pipeline.py:327](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/intelligence/media_pipeline.py:327) показывают `dr:retry` / `dr:paid`. [keyboards.py:111](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/keyboards.py:111) показывает `dr:edit`. Но [_draft_action:543](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/callbacks.py:543) реализует только `post` и `cancel`; остальные действия отвечают общим текстом и не сохраняют состояние ввода.

Подтверждено:

- После отказа AI кнопка повторения не вызывает AI повторно; draft остаётся `failed_retryable`.
- После вопроса об оплате invoice кнопка «Да, оплачено» оставляет draft в `needs_clarification`, кандидатов по-прежнему нет.
- У черновика 450 ₽ нажать «Изменить», отправить 600: создаётся другой draft; подтверждение исходной карточки всё ещё проводит 450 ₽.

Тесты: `test_media_retry_button_reprocesses_saved_draft`, `test_invoice_paid_button_produces_confirmable_record`, `test_ready_draft_edit_updates_same_draft`. Требования: **FR-13, FR-17, FR-20, A20, A30**. V-02 исправлял повтор доставки исходного события, не пользовательскую кнопку повторного разбора; это другой разрыв.

### F-06 · P1 · BLOCKER: мастер теряет обязательства/цели, а действия их создания после мастера недоступны

Мастер просит «Аренда = 20000 = 20.09» и «Отпуск = 100000» ([onboarding_flow.py:265](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/onboarding_flow.py:265)). Обработчик [onboarding_flow.py:445](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/onboarding_flow.py:445) игнорирует оба текста и просто переходит дальше. После успешной публикации в БД нет ни `ScheduledItem`, ни `Goal`.

Обходного пользовательского пути тоже нет: [goals_flow.py:45](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/goals_flow.py:45) выдаёт `goal:new`, но dispatcher не содержит ветки `goal`; ответ — «Кнопка устарела». Напоминание предлагает `pay:new`, а [payments_flow.py:84](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/payments_flow.py:84) сразу отвергает отсутствие ID существующего экземпляра. В conversation нет вызовов `create_goal` / `create_schedule`.

Тесты: `test_wizard_keeps_commitment_and_goal_inputs`, `test_goal_creation_button_enters_wizard`, `test_new_payment_button_enters_form`. Требования: **FR-84, FR-45, FR-49, CMD-20, CMD-21**; без этих связей недоступны и дальнейшие FR-50/51. Штатный `create_budget` в acceptance всегда пропускает оба шага.

### F-07 · P1 · BLOCKER: «Оплачено» записывает расход, но не закрывает обязательство

Для существующего ожидаемого платежа 1000 ₽ нажать «Оплачено» и по подсказке отправить «оплатил интернет 1000». Расход успешно проводится; `Occurrence.state` остаётся `planned`, `settled_minor=0`. Обязательство продолжает уменьшать доступный ресурс и попадать в напоминания.

[payments_flow.py:155](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/payments_flow.py:155) только выводит обещание последующего закрытия, не связывая ожидаемый экземпляр с будущим вводом. В conversation/ledger нет потребителя `settle_occurrence`.

Тест: `test_payment_done_links_record_to_occurrence`. Подготовлен существующий платёж штатным сервисом, пользовательское действие исполняется runtime API. Требования: **FR-46, CMD-20, A227**. Денежные ошибки внутри самого `settle_occurrence` проверяет другой ревьюер.

### F-08 · P1 · BLOCKER: управление категориями не завершает rename / restore / limit

- Нажать «Архив» после архивирования категории: `cat:archive` не обрабатывается; ответ «Действие недоступно». `apply_category_restore` существует, но не импортируется и не вызывается разговорным маршрутизатором.
- «Переименовать» → «Кафе»: название остаётся прежним.
- «Задать лимит» → 8000: прежний лимит 5000 остаётся; число уходит в обычный ввод трат.

[category_flow.py:88](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/category_flow.py:88), [category_flow.py:117](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/category_flow.py:117) создают кнопки. [callbacks.py:415](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/callbacks.py:415) перечисляет rename/limit, но [callbacks.py:447](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/callbacks.py:447) только просит текст, не сохраняя выбранную категорию/действие.

Тесты: `test_category_archive_is_accessible_and_can_restore`, `test_category_rename_button_saves_name`, `test_limit_button_changes_existing_limit`. Требования: **FR-22, FR-35, CMD-12, A116, A119, A12**. Отрицательная проверка A12 доказывает лишь отсутствие изменения *до* подтверждения, но не возможность завершить изменение.

### F-09 · P1 · BLOCKER: передачу администрирования нельзя принять через интерфейс

Администратор выполняет видимый путь «Участники → Выйти → Передать роль → участник»; получает «Предложение отправлено». У получателя нет уведомления или кнопки принятия. [membership.py:368](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/identity/membership.py:368) только сохраняет `AdminTransferProposal`, без outbox event. [sections.py:141](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/sections.py:141) не показывает pending proposal. Во всём `src` нет генератора `ws:acceptadmin` — только обработчик. Стандартный A173 вручную отправляет несуществующую в UI кнопку `member.press("ws:acceptadmin:x")`.

Тест: `test_admin_transfer_is_actionable_by_recipient`. Требования: **FR-82, A173**. Дополнительный статический разрыв того же раздела: `remove_member` / `allow_rejoin` не имеют conversation-потребителей, а `/members` содержит лишь приглашение/выход; **FR-81** не доступен администратору через Telegram.

### F-10 · P1 · BLOCKER: полный набор фильтров людей/контекста не подключён

[history_flow.py:28](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/history_flow.py:28) содержит только «Мои записи», «Потратил я», «С комментарием», текущий период/импорт/возвраты. Нет выбора другого автора, покупателя, получателя или «Без комментария». Создаваемый здесь [FilterSpec:109](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/history_flow.py:109) не передаёт beneficiary/tag-фильтры. Категории [history_flow.py:241](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/history_flow.py:241) ограничены первыми четырьмя, без перехода дальше.

Это подтверждено статической трассой всех dispatch-веток и вызовов, отдельный динамический тест отсутствия всей возможности не добавлялся. Требования: **FR-07, FR-89**. Нижележащие `FilterSpec`/SQL умеют больше, но их интеграционные тесты напрямую вызывают сервисы и не доказывают пользовательскую доступность.

### F-11 · P2 · WARNING: поиск по комментарию теряется при переходе на следующую страницу

Создать 9 операций с комментарием «отпуск» и одну без него; `/history отпуск` показывает 9, «Ещё →» уже показывает «9–10 из 10» без поискового условия. [JournalView:39](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/history_flow.py:39) не хранит `note_query`; [history_action:280](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/history_flow.py:280) повторно вызывает `journal_view` без него. Сортировка и переход к фильтрам также теряют запрос.

Тест: `test_comment_search_survives_page_navigation`. Требования: **FR-07, FR-89**. Первая страница WIRED; сохранение поиска между экранами BROKEN.

## Карта provides → consumes

| Поставщик | Потребитель | Статус |
|---|---|---|
| S01 `publish_wizard` | wizard publish → Workspace/план/финальный ответ | WIRED для обычного мастера; воспроизведён как setup через UI |
| S01 invites/join | inv:new → /join → shared workspace | WIRED; пройдено в export/transfer сценариях |
| S03 ledger | text draft → dr:post → ledger → карточка | WIRED для одной обычной траты |
| S03 categories | cat:arch → remove_category → история | WIRED; архивирование и сохранение операции пройдены |
| S03 category lifecycle | rename/restore/limit → разговорный ввод | BROKEN F-08 |
| S05 draft pipeline | batch confirmation → атомарный ledger commit | BROKEN F-01 |
| S05 correction target | Telegram reply ID → нужная Transaction | BROKEN F-02 |
| S06 receipt pipeline | одна покупка → несколько Allocation | BROKEN F-04 |
| S06 media recovery | retry/paid/edit callbacks → draft transition | BROKEN F-05 |
| S01 wizard → S07 | поля обязательств/целей → ScheduledItem/Goal | BROKEN F-06 |
| S07 commitments → S03 | pay:done → запись → settle_occurrence | BROKEN F-07 |
| S01 membership lifecycle | proposal → доставка → accept callback | BROKEN F-09 |
| S03 context → S08 journal | UI → все FilterSpec-поля | BROKEN F-10 |
| S03 context → S08 journal | note_query → paging/sort/filter | BROKEN F-11 |
| S09 export → S01 access | snapshot → актуальная проверка → send_document | BROKEN F-03 |

Охват таблицы: **4 WIRED / 11 BROKEN ожидаемых связей**. Это количественный итог указанного scope, не инвентаризация всех экспортов репозитория. HTTP API-инвентарь и webhook delivery проверяет основной аудитор. Для этих сценариев внешняя граница — Telegram, а внутренние «API» — `dispatch_callback` и разговорный роутер; наличие только backend-команды не считается подключением.

Номинальный импорт прослежен: DOCUMENT → `media.is_table_document` → `io_flow.handle_table_document` → `parse_workbook` → `build_preview` → `imp:commit` → `commit_import` → итог. Прежний R-08 отсутствует в текущем маршрутизаторе. Полный новый импорт с реальным источником в этом scope не запускался; утверждение о полной готовности импорта не делается. `io_flow.import_action` разрешает отмену уже применённого batch, но деньги здесь не проверялись — не включено как подтверждённая отдельная находка.

Дополнительная проверка достижимости специальных денежных команд: [entry.build_spec:659](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/entry.py:659) отвергает перевод/возврат/заём/смешанную оплату с текстом об «отдельной команде». [manual_form.py:98](/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/manual_form.py:98) реализует только расход. Разговорных потребителей `post_refund`, `post_mixed_payment`, `settle_receivable`, `settle_occurrence`, `record_reconciliation` и `accept_reconciliation` нет. Наличие этих сервисов и их прямых unit/integration-проверок не обеспечивает пользовательский путь; денежные инварианты внутри них принадлежат отдельному аудиту.

## Requirements Integration Map

| Требования | Путь | Статус | Замечание |
|---|---|---|---|
| FR-01…05, FR-78 | создание обычного бюджета / приглашение / join | WIRED (проверенный основной путь) | Основной пользовательский setup работает |
| FR-11, FR-20, A05 | пакет → подтверждение → commit | BROKEN | F-01; отсутствие исключения кандидата |
| FR-33, FR-87, A124 | reply к карточке → target → revision | BROKEN | F-02 |
| SEC-05, SEC-08, FR-67 | snapshot → актуальные полномочия → файл | BROKEN | F-03 |
| FR-14, FR-16, A22 | смешанный чек → единая покупка | BROKEN | F-04 |
| FR-13, FR-17, FR-20, A20, A30 | кнопки media → повтор/уточнение/правка | BROKEN | F-05 |
| FR-84 | ввод плановых трат/целей → публикация мастера | BROKEN | F-06 |
| FR-45, FR-49…51, CMD-20, CMD-21 | создание/изменение обязательств и целей из UI | BROKEN | F-06 |
| FR-46, A227 | факт платежа → закрытие ожидания | BROKEN | F-07 |
| FR-22, FR-35, CMD-12, A116, A119, A12 | категория/лимит → изменение → отображение | BROKEN | F-08 |
| FR-81, FR-82, A173 | управление участниками → доступные действия | BROKEN | F-09 |
| FR-07, FR-89 | UI контекстных фильтров → SQL → переходы | BROKEN | F-10/F-11 |

**Требования с отсутствующим межсрезовым подключением:** для управления из Telegram `FR-45`, `FR-49…51`, `FR-81` представлены в основном внутренними сервисами, без требуемых пользовательских вызовов. FR-16 проверяет нижний алгоритм, но связь его результата с моделью единой покупки нарушена. Эти случаи не являются достаточным основанием для `verified` всего пользовательского требования.

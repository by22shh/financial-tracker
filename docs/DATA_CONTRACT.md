# Контракт данных и команд

Версия 0.7, 12 сентября 2026 года. Нормативное дополнение к [ТЗ](TZ.md) и [архитектуре](ARCHITECTURE.md) для реализации P0; AIProfile фиксирует Luna Medium через прямой OpenAI API по ADR-17. Схема ниже задаёт обязательные типы, ограничения и поведение; исполняемые SQL миграции и OpenAPI создаются и проверяются при реализации. Наличие таблицы для будущего сценария не включает его в P0.

## 1 Общие типы и соглашения

| Область | Контракт |
|---|---|
| ID | UUID v4 для доменных сущностей; внешний Telegram ID — BIGINT, отдельно от собственного user_id |
| Деньги | BIGINT minor units; валюта CHAR(3), число десятичных знаков из фиксированного справочника валют |
| Размер суммы | Одна обычная сумма >0 и ≤10^15 minor units; сумма всех частей проверяется до сохранения; агрегаты PostgreSQL SUM(BIGINT) читаются без потери точности |
| API денег | amount_minor — строка целого числа и currency; вход пользовательского текста отдельно парсится из десятичной строки |
| Даты | DATE для локальной даты/интервала, TIMESTAMPTZ для технического события; IANA timezone в версии политики |
| Версии | BIGINT >0; entity_version для конкуренции, отдельные счётчики финансов/плана/календаря/контекста/полноты для актуальности |
| Состояния | TEXT + CHECK по разрешённым значениям; переходы через команды, изменения списка — миграцией |
| Структура | Деньги, роли, даты и внешние ключи — типизированные столбцы; JSONB только для ограниченных payload, настроек и снимков со schema_version |
| Область | Каждая финансовая строка содержит NOT NULL workspace_id; дочерняя ссылка — FK (workspace_id, object_id) |
| Удаление | RESTRICT для финансовых зависимостей; обычная отмена не DELETE; физическая очистка только при принятом удалении бюджета |
| Неизвестность | NULL/отдельное состояние неизвестности; нулевой лимит и нулевые подтверждённые расходы не равны незаданным |

Округление денег выполняется только на границе преобразования по конкретному правилу ТЗ. Распределение скидки использует детерминированный метод остатков, сохраняющий исходную сумму. Никаких двоичных float в финансовом пути. В P0 одна валюта бюджета и его счетов; после появления денежных записей или денежного плана её нельзя изменить заменой кода валюты. В пустом мастере пересоздаются денежные настройки. Неподдерживаемый валютный чек требует фактически списанной суммы в валюте бюджета либо остаётся черновиком.

Отрицательный остаток категории или счёта сам по себе не запрещает запись реально совершённой покупки. CHECK balance≥0 для журнала не используется: перерасход показывается пользователю, а не скрывается отказом принять факт. Нулевой результат SUM пустой известной выборки оформляется отдельно от неизвестной полноты всей области. [Типы и пустые выборки агрегатов PostgreSQL](https://www.postgresql.org/docs/17/functions-aggregate.html)

Каждая таблица с ID и workspace_id имеет UNIQUE(workspace_id, id), пригодный для составного FK. Ссылки на ревизии содержат также transaction_id и revision. Проверять только UUID без бюджета недостаточно.

## 2 Таблицы и ограничения

### 2.1 Бюджет, люди и доступ

| Таблицы | Существенные поля и ограничения |
|---|---|
| users | id, telegram_user_id UNIQUE, locale, status; личные настройки отделены от общего бюджета |
| workspaces | id, name, currency, state, version, admin_user_id, security_fence, acl_revision, data_revision и счётчики областей |
| memberships | workspace_id, user_id, role, status, generation UUID, access_version; UNIQUE(workspace_id,user_id) |
| membership_history | Неизменяемые переходы состояния/поколения, инициатор, SecurityChange ID |
| user_budget_contexts | user_id PK, workspace_id nullable, version; выбор проверяет членство; чужой выбор не изменяется |
| budget_setup_drafts | owner_user_id, version, state, published_workspace_id UNIQUE nullable, schema_version, payload; личный до публикации |
| budget_invites | id, workspace_id, secret_digest UNIQUE, digest_key_version, expires_at, max_uses>0, used_uses≥0, revoked_at; used_uses≤max_uses |
| admin_transfer_proposals | workspace_id, from_user_id, to_user_id, expected_acl_revision, expires_at, state |
| security_changes | workspace_id, operation_id UNIQUE, expected_acl_revision, proposed_acl_revision, state, digest; ссылка на независимый журнал |
| people / beneficiaries | Один бюджет; Person.user_id nullable, имя не является ключом; beneficiary kind person/common; common не подменяет неизвестного |

Partial unique index на memberships(workspace_id) WHERE role='admin' AND status='active' обеспечивает **не более одного** администратора. Дополнительно deferred constraint trigger проверяет **ровно одного** для active бюджета и совпадение с workspaces.admin_user_id при окончании транзакции. Одна только уникальность не запрещает отсутствие администратора.

Принятие приглашения блокирует бюджет, проверяет существующее членство и запрет повторного входа, затем код/срок/квоту. Повтор уже состоявшегося вступления не расходует новое использование. Смена поколения выполняется только при реальном новом вступлении. Передача admin в одной транзакции сначала снимает прежнюю роль и назначает новую, а deferred проверка видит итог без пустого администратора.

### 2.2 Справочники, календарь, планы

| Таблицы | Существенные поля и ограничения |
|---|---|
| categories | workspace_id, parent_id, name, normalized_name, archived_at, version; родитель в том же бюджете |
| category_aliases / classification_rules | Ключ правила, scope workspace/member, owner, priority, version, typed condition/action; цель в своём бюджете |
| tags / transaction_tags | Уникальное нормализованное имя активной метки в бюджете; UNIQUE(workspace_id,transaction_id,revision,tag_id) |
| period_policies | workspace_id, version, anchor_date, anchor_day, mode, interval>0, first_end_exclusive, effective_from, timezone |
| recurring_plan_templates | workspace_id, version, effective_from, enabled, approval_actor, base amounts и правило дохода; факта в шаблоне нет |
| budget_periods | workspace_id, id, policy_id/version, sequence, start_date, end_exclusive, state, transition; start<end |
| budget_versions / budget_lines | period_id, version, plan_status, baseline/working, template_version, approved_by; category/beneficiary в бюджете; nullable limit |
| income_sources / income_plans | exact/estimate/range, currency, monthly_basis, period_basis, датированные ожидания или подтверждённая сумма периода |
| budget_transfers / rollovers | from/to line, amount, source/destination period, basis_revision, status; UNIQUE логического принятого переноса |

Название категории нормализуется по Unicode NFKC, регистру и внешним пробелам; исходное отображение сохраняется. Активное имя уникально среди детей одного родителя, включая корень через NULLS NOT DISTINCT. Одинаковые названия в разных ветках допустимы, AI получает полный путь. Архивные категории не допускают новых обычных назначений до восстановления; старые ревизии сохраняют ссылки. Циклы проверяются рекурсивным запросом под блокировкой бюджета. [Уникальные индексы и NULL](https://www.postgresql.org/docs/17/indexes-unique.html)

У периода UNIQUE(workspace_id,start_date), UNIQUE(workspace_id,policy_id,sequence), EXCLUDE USING gist по workspace_id WITH = и daterange(start_date,end_exclusive,'[)') WITH &&; используется btree_gist. Ограничение действует только внутри бюджета. Последовательность/смежность проверяются сервисом и deferred trigger при изменении периодов. [Диапазоны PostgreSQL](https://www.postgresql.org/docs/17/rangetypes.html)

Массовое объединение категорий имеет preview с числом операций, суммой, правилами и версиями основы; apply не меняет базовый денежный эффект. Для P0 атомарный apply ограничен 5 000 затронутых операций, крупный диапазон делится на явно выбранные части. Будущая пакетная миграция без этого предела потребует отдельного протокола видимости.

### 2.3 Журнал, счета и связанные движения

| Таблицы | Существенные поля и ограничения |
|---|---|
| accounts | workspace_id, currency, mode full_tracking/reference, type, opening_cutoff nullable, balance_kind, archived_at, version |
| transactions | workspace_id, id, source_candidate_id nullable UNIQUE в бюджете, created_by, current_revision, status, occurred_sort_date; проекция сортировки обновляется с ревизией |
| transaction_revisions | workspace_id, transaction_id, revision, previous_revision, change_kind, changed_by, amount_minor, type, date/interval, granularity, spender_person_id, note; неизменяемые |
| allocations | workspace_id, transaction_id, revision, allocation_id, stable_line_id, economic_role, category_id, beneficiary_id, amount_minor>0, related_object_id |
| cash_legs | Ревизия, leg_id, account_id nullable, signed_minor, coverage_mode tracked/reference/unknown/included_in_opening; направление реального внешнего потока |
| account_entries | account_id, effect_id, transaction_id/revision либо opening_adjustment_id, effective_date/at, signed_minor≠0, reverses_entry_id nullable UNIQUE; неизменяемые |
| financial_effects | workspace_id, transaction_id, effect_id, source_revision, replaced_effect_id; одна актуальная связь эффекта у операции |
| transaction_links / refund_links | Источник и целевая операция, конкретный stable_line_id/ревизия, тип, amount_minor, статус; обе стороны в бюджете |
| receivables / receivable_entries | Исходная возмещаемая доля, контрагент, изменения остатка и связи с оплатой/возвратом |
| liabilities / liability_entries | P1; до поддержки долга соответствующие команды недоступны |
| audit_events | Автор исходной команды/системная причина, тип, ID, до/после или ссылки на ревизии, correlation_id |

Гранулярность individual/daily_aggregate/period_aggregate независима от экономического типа. Для агрегата периода хранится интервал, он не получает выдуманную дату покупки. В запросе по части интервала сумма не распределяется по дням автоматически: результат показывает нераспределённый агрегат и ограничение точности. Журнал может сортировать агрегат по началу интервала только для отображения, с явной меткой.

CashLeg описывает откуда/куда ушли деньги даже при неизвестном либо частично учитываемом счёте. AccountEntry создаётся только для tracked части после opening_cutoff. Для reference счёта возможна история известных платежей без вычисления «фактического остатка». Included_in_opening означает, что движение уже входит в начальный остаток и повторно не проводится по счёту.

Начальная точка: пользователь выбирает остаток **на начало даты** либо точный момент с часовым поясом. Если исходные операции имеют только дату и внутри дня невозможно определить положение относительно точного момента, связь с остатком требует уточнения; порядок не угадывается. Импорт старой истории не уменьшает начальный остаток ещё раз.

Перевод между двумя tracked счетами даёт -S и +S, сумма AccountEntry равна нулю. Если один счёт вне полного охвата, нулю равна сумма CashLeg, а изменение известных остатков ограничено tracked сторонами. Внешнее пополнение из личных денег вне охвата имеет type=external_funding: участвует в полученном ресурсе бюджета, показывается отдельно от earned income и не увеличивает показатель заработка. Ожидаемая зарплата остаётся планом, пока не записан факт.

### 2.4 Денежные инварианты

| Тип / действие | Инвариант |
|---|---|
| expense | Сумма allocations с role expense = S; сумма CashLeg=-S; подтверждённые tracked движения соответствуют своим leg |
| income / external_funding | CashLeg=+S; заработанный доход увеличивает только income; расходных allocations нет |
| transfer | Две стороны одной валюты, разные счета, CashLeg суммарно 0; отсутствует потребительский расход |
| mixed_payment | Сумма допустимых expense + receivable_increase (+ liability_decrease только P1) = S; CashLeg=-S |
| receivable_settlement | CashLeg=+S, погашение требования=S; не больше открытого остатка; дохода нет |
| refund | CashLeg=+S; положительные части expense_refund / receivable_reversal ссылаются на возвращаемые части, их сумма=S; потребление уменьшает только expense_refund |
| adjustment | Явная причина и счёт; меняет баланс, не потребление/заработок; отдельное подтверждение |
| void / restore | Повтор команды не добавляет эффект; void обращает текущий эффект, restore заново проверяет зависимости |
| legacy_unclassified_flow | Сумма и источник сохранены; неизвестный смысл не маскируется под расход, доход или движение реального счёта |
| Цель / лимит | Резервирование и изменение лимита не создают CashLeg или AccountEntry без отдельного реального движения |

Service слой проверяет эти правила до записи. NOT NULL, CHECK, UNIQUE, FK и EXCLUDE защищают простые ограничения. Deferred constraint triggers проверяют суммы частей/движений, целостность текущей ревизии и непротиворечивость связанных возвратов к концу commit. CHECK с запросом SUM по другой таблице не используется: PostgreSQL не гарантирует его корректность при изменении других строк. [Ограничения PostgreSQL](https://www.postgresql.org/docs/17/ddl-constraints.html)

Изменяемый указатель current_revision ссылается на существующую ревизию той же операции. Прямая UPDATE/DELETE исторической ревизии или AccountEntry runtime роли запрещена. Обратная запись имеет ровно противоположную сумму исходной, тот же счёт и уникальный reverses_entry_id. Undo исправления — новая ревизия с проверкой expected_version.

Пример, все числа в minor units: начальный остаток 10000; покупка 1000 → 9000; исправление на 800 добавляет +1000 и -800 → 9200; комментарий → 9200; отмена добавляет +800 → 10000; восстановление добавляет -800 → 9200. Отчёт текущих расходов показывает 800, а не сумму всех исторических ревизий.

### 2.5 Возвраты, цели и плановые платежи

Возврат ссылается на конкретные части исходной покупки. При нескольких возвратах их активная сумма по части не превышает её текущую возвращаемую сумму. Проверка и запись проходят под блокировкой бюджета; два параллельных возврата не получают один и тот же остаток. Изменение/слияние категории сохраняет stable_line_id, чтобы связь не терялась. Исправление покупки с удалением уже возвращённой части требует плана изменения зависимостей.

Для возмещаемой доли source role=receivable_increase возврат уменьшает ещё открытое требование через receivable_reversal и не уменьшает потребительский расход. Если соответствующая доля уже возмещена, возникает зависимость от расчёта с контрагентом: P0 оставляет конфликт на уточнении и не создаёт отрицательное требование или скрытый новый долг. Связанная expense часть может возвращаться отдельно. Для обратной проводки датированного эффекта effective_date совпадает с исходной датой: это исправление, а не новый расход сегодня.

По умолчанию чистый расход уменьшается в периоде фактического возврата. Получатель, категория и разрез «кто потратил» следуют возвращаемой части актуального подтверждённого контекста покупки; создатель возврата хранится отдельно. Отдельный режим отнесения к исходной дате явно называется иначе. Счёт получения может отличаться от исходного.

Если покупка оплачивалась из резерва цели, при подтверждении возврата выбирается «Вернуть резерв» или «Оставить свободными». До выбора возврат остаётся черновиком, а общая оценка отмечает ожидающий разбор; деньги не объявляются одновременно свободными и защищёнными. Изменение резерва и запись подтверждённого возврата атомарны, дополнительного дохода/банковского движения нет.

| Таблицы | Инвариант |
|---|---|
| scheduled_items / schedule_versions | Стабильное расписание и версии правила, effective_from; собственная периодичность |
| occurrences | workspace_id, schedule_id, occurrence_slot, original_due_date, due_date, state; UNIQUE(schedule_id, original_due_date, occurrence_slot); версия — атрибут, не способ дублирования |
| occurrence_settlements | Связь с активным денежным эффектом/частью, amount>0; отмена или правка эффекта меняет исполнение атомарно |
| goals / goal_movements | План, резерв, использование, освобождение раздельны; каждая связь с финансовым эффектом уникальна |
| cash_reservations | Непересекающийся источник защиты, сумма, назначение и связь с occurrence/goal; одна сумма не вычитается из доступного ресурса дважды |

Occurrence: planned / partially_settled / settled / skipped / cancelled. Просрочка вычисляется отдельно по due_date и remaining_amount; она не уничтожает частичную оплату. При плане 1000 и платеже 600 остаток 400, в следующем периоде он остаётся просроченным один раз. Платёж 1200 на ожидаемые 1000 требует выбора: изменить ожидаемую сумму с версией либо связать 1000, а 200 оставить отдельной несвязанной частью факта. Отрицательного остатка не возникает.

Правка расписания имеет scope=this_occurrence либо this_and_future. Уже исполненные экземпляры не переписываются. При изменении даты будущие неисполненные экземпляры явно замещаются новой версией с сохранением связи; два экземпляра одного обязательства не остаются действующими. Skip/cancel имеет причину и историю, не создаёт дохода. Совпадение суммы/магазина — предложение связать факт, не доказательство оплаты.

Сверка и полнота:

- CoverageRecord имеет workspace, область людей/счетов/дат, status, основание и basis_revision.
- Reconciliation имеет account, cutoff, balance_kind, observed_balance, известный расчёт, difference, author, basis_revision.
- Денежная правка до cutoff или смена охвата делает затронутую сверку stale; комментарий не меняет баланс. Переклассификация может сделать stale аналитику по категориям.
- Равенство балансов не доказывает полноту встречных потоков. Пополнение 1000 и расход 1000 могут одновременно отсутствовать.

### 2.6 Приём, фоновые задачи и идемпотентность

| Таблицы | Ключ / правило |
|---|---|
| inbound_events | UNIQUE(bot_id,update_id); chat/message/edit version, принятый контекст, owner, timestamps |
| inbound_payloads | Защищённое содержимое отдельно от глобальной очереди; delete_after; invite secret заменён digest |
| logical_messages / message_parts | UNIQUE(bot_id,chat_id,message_id,edit_version); media group key со стабильным порядком частей |
| drafts / candidates | workspace, owner, base source, state, version; постоянный candidate ID для каждого предполагаемого события |
| clarifications | draft/candidate/field, asked_message_id, expected_version, expires_at, state |
| parse_attempts | candidate/source version, profile_version, requested/returned model, effort, service_tier, schema/prompt versions, lease_token, result_status, provider_request_id, usage и стоимость |
| command_results | scope=(user,workspace,command,idempotency_key), canonical_body_hash, entity/result ID; UNIQUE scope |
| jobs | logical_key UNIQUE для логического запуска, payload_version, state, available_at, lease_until/token, attempts, deadline |
| outbox_events / consumer_receipts | event_id, event_seq, aggregate/revision, type/schema_version; UNIQUE(event_id,consumer_name) |
| notification_deliveries | UNIQUE(event_id,recipient,generation,channel), state, retries, telegram_message_id |
| threshold_events / recipient_day_quotas | UNIQUE(workspace,period,line,threshold); отдельно UNIQUE(recipient,local_day) для двух проактивных сообщений по всем бюджетам |
| ai_cost_reservations | service/workspace/UTC calendar month, request ID, reserved/actual currency amount NUMERIC(24,8), state; агрегат квот блокируется при резерве |
| attachments / export_files | Состояние staging/ready/deleting/deleted, checksum, owner/workspace, visibility owner/workspace, delete_after; без публичного storage URL |
| import_batches / import_rows | source hash и mapping version, нормализованные строки, стабильные source keys, decision/status |
| analytics_snapshots / analysis_runs | Точные границы, filter_hash, revision vector, method; у запуска profile_version, requested/returned model, effort, service_tier, usage; UNIQUE логического запуска |

Команда требует expected_version изменяемого объекта и Idempotency-Key. Хэш считается по каноническому типизированному содержимому команды, не по случайному порядку JSON полей. Один ключ с другим содержимым → 409. Проверка доступа выполняется до возврата сохранённого результата, включая после выхода и повторного вступления. Долгоживущие финансовые source keys и результаты команд сохраняются до удаления бюджета; тяжёлый raw payload удаляется отдельно.

Одно сообщение может дать несколько Candidate; уникальность накладывается на candidate, не запрещает две реальные покупки в одном сообщении. Все варианты ввода одного кандидата сходятся на одном source_candidate_id в transactions. Обычный новый текст той же суммы имеет другой candidate и не удаляется без решения пользователя.

## 3 Индексы и правила запросов

Перечень — исходный физический план; эффективность подтверждается EXPLAIN (ANALYZE, BUFFERS) на синтетических/обезличенных 50 000 операций, включая перекос категорий и длинную историю. Индексы добавляются миграциями, не произвольно по каждому полю.

| Таблица / запрос | Начальный индекс |
|---|---|
| memberships: мои бюджеты | (user_id,status,workspace_id), отдельно admin partial unique |
| transactions: текущий журнал | (workspace_id,occurred_sort_date DESC,id DESC) для текущей проекции полей; состояние и дата проекции обновляются с current_revision атомарно |
| allocations: категория/получатель | (workspace_id,category_id,transaction_id,revision); (workspace_id,beneficiary_id,transaction_id,revision) |
| transaction revisions: история | UNIQUE(workspace_id,transaction_id,revision) |
| account_entries: баланс/сверка | (workspace_id,account_id,effective_date,id) |
| tags | UNIQUE связи ревизия/метка и (workspace_id,tag_id,transaction_id) |
| occurrences: ближайшие/просроченные | (workspace_id,due_date,id) WHERE state IN ('planned','partially_settled') |
| jobs: готовые | (queue_class,available_at,id) WHERE state IN ('queued','retry_wait'); отдельный lease_until для running |
| outbox / delivery | (state,available_at,id); UNIQUE ключи предметной обработки |
| attachments / retention | (delete_after,id) WHERE state IN ('ready','deleting') |
| все активно используемые FK | Индексы дочерних ссылок по реальным join/delete/restrict запросам |

Категория+дата не объявляется индексом одной таблицы, если эти поля физически разделены. Начальный запрос соединяет текущую ревизию и allocations с фильтром бюджета и дат; отдельная перестраиваемая проекция по распределениям вводится только при измеренной необходимости.

Поиск заметки P0: параметризованный ILIKE внутри бюджета по **текущим** ревизиям, ограничение длины и страницы, литеральные '%'/'_' экранируются для режима поиска подстроки. На целевой нагрузке проверяется самый широкий запрос. pg_trgm GIN допускается следующей миграцией после измерения; старые удалённые заметки не находятся обычным поиском.

Для чека 1400 с частями Продукты/Софа 1000 и Дом/Ниджат 400 фильтр Продукты+Ниджат возвращает 0. Фильтр Продукты возвращает amount_matched=1000, transaction_total=1400, transaction_count=1. Итоги distinct покупок по категориям нельзя складывать как независимые события. API и CSV содержат раздельные поля; выбор нескольких меток не умножает сумму.

## 4 Карта команд и прав

Обозначения: M — любой активный member или admin, A — admin, O — автор личного объекта с текущим доступом. Изменение требует idempotency key; E — expected_version; P — принятый preview с версией основы. GET не создаёт денежного факта. Таблица описывает серверные команды P0, даже если HTTP интерфейс пока используется только ботом.

Все строки ниже, кроме явно помеченных global, имеют префикс **/v1/budgets/{budget_id}**. В таблице ТЗ короткие маршруты являются суффиксами этого контракта. Каждая команда проверяет чужие ID, current membership generation, state и security_fence. Общая нотификация означает доменное событие для delivery с личными настройками, а не обязательный шум каждому по каждой технической правке.

| ID | HTTP путь / действие | Право | Версия / подтверждение | Побочный результат |
|---|---|---|---|---|
| CMD-01 | global POST /v1/telegram/webhook | Проверенный Telegram | Уникальный update | Inbox+Job до 2xx |
| CMD-02 | global GET /v1/budgets; POST/PATCH /v1/budget-setups; POST /v1/budget-setups/{id}/publish | Свой user / O | E; публикация P | Личный мастер; SecurityChange создания |
| CMD-03 | POST /activate | M | Версия своего контекста | Только личный выбор |
| CMD-04 | GET /members; POST /invites; POST /invites/{id}/revoke | M для списка, A для кода | E; срок/квота | Аудит доступа |
| CMD-05 | global POST /v1/invites/preview и /accept | Проверенный user | Код; E из preview при принятии | Членство+квота; общая история |
| CMD-06 | POST /leave; POST /members/{user}/remove и /allow-rejoin | Сам M / A | E; подтверждение конкретного действия | SecurityChange, отмена личных доставок |
| CMD-07 | POST /admin-transfers; global POST /v1/admin-transfers/{id}/accept | A / адресат | E+P, TTL | SecurityChange, смена обеих ролей |
| CMD-08 | POST /deletion-preview и /delete | A | E+P, подтверждение названия | Закрытие доступа, очистка после протокола |
| CMD-09 | GET/POST /categories; PATCH /categories/{id}; POST /categories/{id}/removal-preview,/remove,/restore,/merge | M | E; P для удаления/слияния | Общая ревизия и инвалидирование аналитики |
| CMD-10 | GET /drafts; GET/PATCH /drafts/{id}; POST /drafts/{id}/confirm,/cancel,/retry-parse | O | E; подтверждение режима | Только личная карточка до проведения |
| CMD-11 | POST /transactions; PATCH /transactions/{id}; POST /transactions/{id}/void,/restore | M | E для существующих; текстовая правка с P | Ledger commit и общая история |
| CMD-12 | GET /transactions; GET /transactions/{id}; GET /transactions/{id}/revisions | M | Read-only | Только данные общего журнала |
| CMD-13 | GET/POST /people,/beneficiaries,/tags; PATCH /people/{id},/beneficiaries/{id},/tags/{id} | M | E | Общие справочники, без приглашения |
| CMD-14 | GET/POST /accounts; PATCH /accounts/{id}; POST /accounts/{id}/archive | M | E; P для начальной точки/смены режима | История; нет удаления связанных движений |
| CMD-15 | GET/POST /reconciliations; POST /reconciliations/{id}/accept; GET/POST /coverage; PATCH /coverage/{id} | M | E+P для сверки/полноты | Область, основание, stale зависимых данных |
| CMD-16 | GET /periods; GET /periods/{id}/status | M | Read-only с ensure_periods | Однократная системная материализация календаря |
| CMD-17 | POST /period-policy/preview,/accept | A | E+P | Новая будущая версия и общая история |
| CMD-18 | GET/PATCH /plan-template; GET/PATCH /income-plan | M | E; явный scope периода/будущего | Общая история; факт дохода не создаётся |
| CMD-19 | POST /budget-proposals; POST /budget-proposals/{id}/accept | M | E+P, revision vector | Атомарный принятый план/перенос |
| CMD-20 | GET/POST /schedules; PATCH /schedules/{id}; GET /occurrences; PATCH /occurrences/{id}; POST /occurrences/{id}/skip,/cancel,/link-payment | M | E; scope и P для серии | Ожидания и связь с ledger; без выдуманной оплаты |
| CMD-21 | GET/POST /goals; PATCH /goals/{id}; POST /goals/{id}/movements | M | E; P для использования/освобождения | Резерв отдельно от счёта; общая история |
| CMD-22 | GET /receivables; POST /receivables/{id}/settlements | M | E+P; уникальная связь факта | Проведение через ledger |
| CMD-23 | GET /reports,/recommendations; POST /analysis-runs | M | Точный диапазон/метод, квота | Числовой снимок; задача по запросу |
| CMD-24 | POST /recommendations/{id}/feedback | M | E; scope личный/общее действие | Личный feedback не меняет план автоматически |
| CMD-25 | GET/PATCH /analysis-preferences | M читает общие, A меняет | E | Общее расписание/направления анализа |
| CMD-26 | GET/PATCH /my/notification-preferences,/my/input-preferences | M, только свои | E | Не меняет чужую доставку/автозапись |
| CMD-27 | GET/POST /classification-rules; PATCH /classification-rules/{id}; POST /classification-rules/{id}/archive | M для общих, O для личных | E; явный scope | Новая версия; без ретроправки трат |
| CMD-28 | POST /imports/preview; GET /imports/{id}; POST /imports/{id}/commit,/revert | Автор пакета M | E+P | Атомарный ограниченный пакет; общая история |
| CMD-29 | POST /exports; GET /exports/{id}; GET /files/{id}/content | M; O для личного сырого файла | Доступ повторно при выдаче | Snapshot, TTL, серверная выдача |
| CMD-30 | PATCH /settings | A | E+P для существенного изменения | Валюта только в пустом бюджете; пояс через новую политику |
| CMD-31 | global GET/PATCH /v1/me; POST /v1/me/deletion-preview,/delete | Только свой проверенный user | E+P для удаления; сначала разрешить роли admin | Личный пояс/язык; отзыв всех членств по SecurityChange и допустимое обезличивание |

Возврат создаётся CMD-11 с type=refund, ссылками на части и подтверждением; он не проходит обходным UPDATE исходной покупки. Начальная корректировка проходит ledger, даже если инициирована CMD-14/15. Редактирование полей Tag и Person не даёт права удалять операции. Архивирование профиля/метки реализуется PATCH с E; история остаётся.

Удаление аккаунта не объединяет денежные транзакции разных бюджетов: это возобновляемый процесс последовательного отзыва членств. До завершения всех отзывов пользователь не получает ложное подтверждение полного удаления. Совместные операции сохраняются по политике ТЗ, переданные административные роли не возвращаются.

Общие безопасные показатели ожидания: GET /pending-summary (M) возвращает только count и достоверную известную сумму по правилам ТЗ; никаких text/audio/индивидуальных черновиков. Владение исходным материалом не меняется при вступлении другого человека.

P1 маршруты /v1/session/telegram, /simulations/purchase, кредитные счета и Sheets connections не становятся доступными раньше реализации соответствующих требований. Публичный endpoint произвольного запуска Job отсутствует.

Ответ ошибки: code, message, correlation_id, retryable, field_errors при валидации; без stack trace и чужих данных. HTTP: 400 неверный формат, 401 непроверенная личность, 403 запрет действия в доступном бюджете, 404 неизвестный/недоступный объект, 409 конфликт версии/ключа, 422 смысловая ошибка, 429 квота, 503 временная недоступность или fence. Telegram переводит те же коды в понятный текст и разрешённые кнопки.

## 5 Наблюдаемая завершённость команды

Сервер возвращает command_id, status и предметную entity_revision. Для длительного разбора ответ accepted означает «сохранено для обработки», а не «трата проведена». Записано означает commit ledger. Для SecurityChange завершено означает окончание всех шагов независимого журналирования. Для экспорта ready означает готовый файл, а не доставку в Telegram. Для рекомендации proposed не означает принятый план.

Числовые ответы возвращают актуальность и полноту. В интерфейсе не требуется показывать ID, SQL или номера аренды; пользователю нужны бюджет, даты, сумма, статус и доступное действие. Техническая трассировка остаётся в диагностическом контракте.

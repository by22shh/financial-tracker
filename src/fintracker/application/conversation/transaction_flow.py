"""Explicit transaction edits and missing refund/transfer details."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select

from fintracker.application.conversation import corrections, views
from fintracker.application.conversation.entry import CandidateFields, load_draft
from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.pending import Pending, clear_pending, set_pending
from fintracker.application.conversation.types import Reply
from fintracker.application.ledger.service import load_current_spec
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.core.money import Money
from fintracker.db.models.access import Workspace
from fintracker.db.models.catalog import Account, Category
from fintracker.db.models.ledger import Transaction, TransactionRevision
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.domain.parsing.dates import resolve_date_expression


def resolve_scoped_id(prefix: str, ids: Iterable[uuid.UUID]) -> uuid.UUID:
    """Resolve compact callback IDs only within an already authorised collection."""
    matches = [value for value in ids if len(prefix) >= 8 and value.hex.startswith(prefix)]
    if len(matches) != 1:
        raise NotFound("Кнопка устарела или неоднозначна. Откройте запись заново.")
    return matches[0]


async def edit_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
    action: str,
    rest: list[str],
) -> list[Reply]:
    wid = actor.require_workspace()
    await clear_pending(settings, user_id=actor.user_id, workspace_id=wid)
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=wid
    ) as session:
        transaction, _, spec = await load_current_spec(
            session, workspace_id=wid, transaction_id=transaction_id
        )
        version = transaction.entity_version
        if action == "edit":
            code = short(transaction_id)
            return [
                Reply(
                    text="✏️ Что изменить в операции?",
                    buttons=(
                        (
                            Button("💰 Сумма", callback("tx", "amount", code)),
                            Button("📅 Дата", callback("tx", "date", code)),
                        ),
                        (
                            Button("🗂 Категория", callback("tx", "cat", code)),
                            Button("💬 Комментарий", callback("tx", "note", code)),
                        ),
                        (Button("✕ Отмена", callback("tx", "open", code)),),
                    ),
                )
            ]
        if action == "cat":
            code = short(transaction_id)
            if len(spec.allocations) != 1:
                return [
                    Reply(
                        text=(
                            "ℹ️ У этой покупки несколько категорий\n\nЕё категории "
                            "меняются по частям — это пока доступно только при вводе."
                        ),
                        buttons=((Button("← К записи", callback("tx", "open", code)),),),
                    )
                ]
            page = max(0, int(rest[0])) if rest and rest[0].isdigit() else 0
            rows = (
                await session.scalars(
                    select(Category)
                    .where(Category.workspace_id == wid, Category.archived_at.is_(None))
                    .order_by(Category.name, Category.id)
                )
            ).all()
            per_page = 8
            chunk = rows[page * per_page : page * per_page + per_page]
            buttons = [
                tuple(
                    Button(
                        c.name[:28],
                        callback("fix", "cat", code, str(version), short(c.id)),
                    )
                    for c in chunk[index : index + 2]
                )
                for index in range(0, len(chunk), 2)
            ]
            paging: list[Button] = []
            if page:
                paging.append(Button("← Назад", callback("tx", "cat", code, str(page - 1))))
            if len(rows) > (page + 1) * per_page:
                paging.append(Button("Далее →", callback("tx", "cat", code, str(page + 1))))
            if paging:
                buttons.append(tuple(paging))
            buttons.append((Button("✕ Отмена", callback("tx", "open", code)),))
            return [Reply(text="🗂 Выберите категорию для этой записи", buttons=tuple(buttons))]
    await set_pending(
        settings,
        user_id=actor.user_id,
        workspace_id=wid,
        kind="transaction_edit",
        payload={"transaction_id": str(transaction_id), "version": version, "action": action},
    )
    code = short(transaction_id)
    if action == "note":
        prompt = "💬 Новый комментарий\n\nОтправьте текст — он заменит текущий."
        prompt_rows: tuple[tuple[Button, ...], ...] = (
            (
                Button("🗑 Удалить комментарий", callback("tx", "nodel", code)),
                Button("✕ Отмена", callback("tx", "open", code)),
            ),
        )
    elif action == "amount":
        prompt = "💰 Новая сумма\n\nОтправьте число, например 1250."
        prompt_rows = ((Button("✕ Отмена", callback("tx", "open", code)),),)
    else:
        prompt = "📅 Новая дата\n\nОтправьте дату: «вчера», «20.09» или «20.09.2026»."
        prompt_rows = ((Button("✕ Отмена", callback("tx", "open", code)),),)
    return [Reply(text=prompt, buttons=prompt_rows)]


async def edit_input(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, pending: Pending, text: str
) -> list[Reply]:
    tid = uuid.UUID(str(pending.payload["transaction_id"]))
    version = int(pending.payload["version"])
    if pending.payload["action"] == "note":
        return await corrections.apply_note(
            settings,
            actor=actor,
            workspace=workspace,
            transaction_id=tid,
            expected_version=version,
            note=None if text.lower() == "удалить комментарий" else text,
        )
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=actor.require_workspace()
    ) as session:
        tx, _, spec = await load_current_spec(
            session, workspace_id=actor.require_workspace(), transaction_id=tid
        )
        if tx.entity_version != version:
            raise ConflictError("Запись уже изменена. Откройте её заново.")
    import re

    action = str(pending.payload["action"])
    amount_match = re.fullmatch(r"\d[\d\s]*(?:[.,]\d{1,2})?", text)
    if action in {"amount", "edit"} and amount_match:
        amount = Money.from_decimal(text.replace(" ", "").replace(",", "."), spec.amount.currency)
        if amount.minor <= 0:
            return [
                Reply(
                    text="✍️ Сумма должна быть больше нуля. Отправьте её ещё раз.", retry_input=True
                )
            ]
        return [
            Reply(
                text=(
                    f"✏️ Сумма: {spec.amount.format()} → {amount.format()}\n\nПодтвердите изменение."
                ),
                buttons=(
                    (
                        Button(
                            "✅ Подтвердить",
                            callback(
                                "fix", "apply", short(tid), str(version), str(amount.minor), "-"
                            ),
                        ),
                        Button("✕ Отмена", callback("tx", "open", short(tid))),
                    ),
                ),
            )
        ]
    if action == "amount":
        return [
            Reply(
                text="✍️ Не понял сумму. Отправьте только число, например 1250.",
                retry_input=True,
            )
        ]
    if action == "date":
        parsed = resolve_date_expression(
            text,
            reference=corrections.session_local_date(workspace.timezone),
        )
        if parsed is None:
            return [
                Reply(
                    text="📅 Не понял дату. Отправьте, например, «вчера» или «20.09».",
                    retry_input=True,
                )
            ]
        return [
            Reply(
                text=(
                    f"📅 Дата: {views.format_date(spec.occurred_date, with_year=True)} → "
                    f"{views.format_date(parsed.value, with_year=True)}\n\n"
                    "Подтвердите изменение."
                ),
                buttons=(
                    (
                        Button(
                            "✅ Подтвердить",
                            callback(
                                "fix",
                                "apply",
                                short(tid),
                                str(version),
                                "-",
                                parsed.value.isoformat(),
                            ),
                        ),
                        Button("✕ Отмена", callback("tx", "open", short(tid))),
                    ),
                ),
            )
        ]
    result = await corrections._propose_amount_or_date(
        settings, actor=actor, workspace=workspace, transaction_id=tid, text=text
    )
    if not any(reply.buttons for reply in result):
        return [Reply(text=result[0].text, retry_input=True)]
    return result


async def special_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    draft_id: uuid.UUID,
    action: str = "details",
    rest: list[str] | None = None,
    candidate_prefix: str | None = None,
    expected_version: int | None = None,
) -> list[Reply] | None:
    """Persist explicit choices on the draft; posting remains a separate confirmation."""
    from fintracker.application.ledger.operations import refundable_parts

    wid = actor.require_workspace()
    rest = rest or []
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=wid
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(wid, actor=actor)
        draft, candidates = await load_draft(
            session, workspace_id=wid, draft_id=draft_id, owner_id=actor.user_id
        )
        if draft.state == "posted":
            return None
        if draft.state in {"cancelled", "expired"}:
            raise NotFound("Черновик больше не активен")
        active = [c for c in candidates if c.state not in {"excluded", "cancelled", "posted"}]
        special = [c for c in active if c.fields.get("kind") in {"refund", "transfer"}]
        if not special:
            return None
        candidate = next(
            (
                c
                for c in special
                if not c.fields.get("operation_details", {}).get("details_confirmed")
            ),
            special[0],
        )
        if candidate_prefix is not None:
            candidate_id = resolve_scoped_id(candidate_prefix, (c.id for c in special))
            candidate = next(c for c in special if c.id == candidate_id)
            if expected_version != candidate.version:
                raise ConflictError("Запись уже изменена. Нажмите «Записать» на черновике снова.")
        elif action != "details":
            raise NotFound("Кнопка устарела. Нажмите «Записать» на черновике снова.")
        fields = CandidateFields.from_payload(dict(candidate.fields))
        evidence = fields.operation_details
        if action == "kind" and rest and rest[0] in {"expense", "income"}:
            # «Перевёл Маше» или «вернули долг» — не перевод и не возврат: запись
            # становится обычным расходом или доходом и проходит своё подтверждение.
            fields.kind = rest[0]
            fields.operation_details = {}
            if fields.kind == "expense" and fields.category_id is None and fields.description:
                from fintracker.application.conversation.entry import _match_category

                fields.category_id, _ = await _match_category(
                    session, workspace_id=wid, text=fields.description, actor=actor
                )
            candidate.fields = fields.to_payload()
            candidate.version += 1
            draft.version += 1
            kind_changed = True
        else:
            kind_changed = False
    if kind_changed:
        from fintracker.application.conversation.sections import draft_reply

        return await draft_reply(settings, actor=actor, workspace=workspace, draft_id=draft_id)
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=wid
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(wid, actor=actor)
        draft, candidates = await load_draft(
            session, workspace_id=wid, draft_id=draft_id, owner_id=actor.user_id
        )
        candidate = next(c for c in candidates if c.id == candidate.id)
        fields = CandidateFields.from_payload(dict(candidate.fields))
        evidence = fields.operation_details
        if action in {"account", "edit"}:
            if action == "account" and fields.kind != "transfer":
                raise ValidationFailed("Счёт создаётся из перевода")
            await set_pending(
                settings,
                user_id=actor.user_id,
                workspace_id=wid,
                kind="transfer_account" if action == "account" else "draft_edit",
                payload={
                    "draft_id": str(draft_id),
                    "candidate_id": str(candidate.id),
                    "candidate_version": candidate.version,
                },
            )
            return [
                Reply(
                    text=(
                        "🏦 Новый счёт\n\nКак его назвать? Например: «Карта» или «Наличные». "
                        "Счёт нужен только для учёта переводов, к банку он не подключается."
                        if action == "account"
                        else (
                            "✍️ Отправьте новую сумму числом для выбранной записи. /cancel — отмена."
                        )
                    )
                )
            ]
        if action == "details" and all(
            c.fields.get("operation_details", {}).get("details_confirmed") for c in special
        ):
            return None
        if action != "details":
            candidate.version += 1
            draft.version += 1
            evidence.pop("details_confirmed", None)
        # Persist before any early return (including source pagination).
        candidate.fields = fields.to_payload()

        def choice(action_name: str, value: str | None = None) -> str:
            args = [short(draft_id), candidate.id.hex[:8], str(candidate.version)]
            if value is not None:
                args.append(value)
            return callback("dr", action_name, *args)

        if fields.amount_minor is None or fields.occurred_date is None:
            return [
                Reply(
                    text="✍️ Сначала укажите сумму и дату записи.",
                    buttons=((Button("✏️ Уточнить", choice("edit")),),),
                )
            ]
        cancel = (Button("✕ Отменить", callback("dr", "cancel", short(draft_id))),)
        if fields.kind == "refund":
            if action == "sources":
                evidence.pop("refund_source", None)
                evidence.pop("refund_line", None)
                evidence.pop("details_confirmed", None)
            if action == "source":
                source_ids = (
                    await session.scalars(
                        select(Transaction.id).where(
                            Transaction.workspace_id == wid, Transaction.status == "posted"
                        )
                    )
                ).all()
                source = resolve_scoped_id(rest[0], source_ids)
                tx, rev, _ = await load_current_spec(
                    session, workspace_id=wid, transaction_id=source
                )
                if tx.status != "posted" or rev.transaction_type not in {
                    "expense",
                    "mixed_payment",
                }:
                    raise ValidationFailed("Выберите действующую покупку")
                evidence["refund_source"] = str(source)
                evidence.pop("refund_line", None)
            if not evidence.get("refund_source"):
                page = max(0, int(rest[0])) if action == "sources" and rest else 0
                rows = (
                    await session.execute(
                        select(Transaction, TransactionRevision)
                        .join(
                            TransactionRevision,
                            (TransactionRevision.transaction_id == Transaction.id)
                            & (TransactionRevision.workspace_id == Transaction.workspace_id)
                            & (TransactionRevision.revision == Transaction.current_revision),
                        )
                        .where(
                            Transaction.workspace_id == wid,
                            Transaction.status == "posted",
                            TransactionRevision.transaction_type.in_(["expense", "mixed_payment"]),
                        )
                        .order_by(Transaction.created_at.desc(), Transaction.id)
                        .offset(page * 6)
                        .limit(7)
                    )
                ).all()
                buttons = [
                    (
                        Button(
                            (
                                f"{rev.occurred_date:%d.%m} · "
                                f"{views.money(rev.amount_minor, rev.currency)}"
                                f" · {(rev.description or 'Покупка')[:25]}"
                            ),
                            choice("source", tx.id.hex[:8]),
                        ),
                    )
                    for tx, rev in rows[:6]
                ]
                if page:
                    buttons.append((Button("← Назад", choice("sources", str(page - 1))),))
                if len(rows) > 6:
                    buttons.append((Button("Далее →", choice("sources", str(page + 1))),))
                buttons.append((Button("💰 Это доход, а не возврат", choice("kind", "income")),))
                return [
                    Reply(
                        text="↩️ Возврат денег за покупку\n\nЗа какую покупку вернули деньги? "
                        "Возврат уменьшит расход её категории."
                        if rows
                        else (
                            "↩️ Возврат денег за покупку\n\nЗаписанных покупок пока нет. "
                            "Если это не возврат, а поступление денег, запишите его как доход."
                        ),
                        buttons=(*buttons, cancel),
                    )
                ]
            parts = await refundable_parts(
                session, workspace_id=wid, transaction_id=uuid.UUID(evidence["refund_source"])
            )
            if action == "part":
                evidence["refund_line"] = str(
                    resolve_scoped_id(rest[0], (part.stable_line_id for part in parts))
                )
            eligible = [
                part
                for part in parts
                if part.refundable_minor >= fields.amount_minor
                and part.currency == (fields.currency or workspace.currency)
            ]
            if not evidence.get("refund_line"):
                candidate.fields = fields.to_payload()
                if len(parts) == 1 and eligible:
                    evidence["refund_line"] = str(eligible[0].stable_line_id)
                else:
                    return [
                        Reply(
                            text=(
                                "↩️ За какую часть покупки вернули деньги? "
                                "Для нескольких частей оформите отдельные возвраты."
                            )
                            if eligible
                            else (
                                "⚠️ Сумма возврата больше, чем осталось вернуть по этой "
                                "покупке. Измените сумму или выберите другую покупку."
                            ),
                            buttons=tuple(
                                [
                                    (
                                        Button(
                                            (
                                                f"Часть {parts.index(p) + 1} · доступно "
                                                f"{views.money(p.refundable_minor, p.currency)}"
                                            ),
                                            choice("part", p.stable_line_id.hex[:8]),
                                        ),
                                    )
                                    for p in eligible
                                ]
                                + [
                                    (
                                        Button(
                                            "✏️ Изменить сумму",
                                            choice("edit"),
                                        ),
                                    ),
                                    (
                                        Button(
                                            "← Другая покупка",
                                            choice("sources", "0"),
                                        ),
                                    ),
                                    cancel,
                                ]
                            ),
                        )
                    ]
            selected = next(
                (p for p in eligible if str(p.stable_line_id) == evidence["refund_line"]), None
            )
            if selected is None:
                raise ValidationFailed(
                    "Остатка выбранной части недостаточно. Измените сумму возврата."
                )
            evidence["details_confirmed"] = "yes"
            text = (
                "↩️ Возврат "
                f"{views.money(fields.amount_minor, fields.currency or workspace.currency)}"
                "\n\nИсходная покупка выбрана. Категория возврата совпадёт "
                "с выбранной частью покупки; доход не увеличится."
            )
        else:
            accounts = (
                await session.scalars(
                    select(Account)
                    .where(
                        Account.workspace_id == wid,
                        Account.archived_at.is_(None),
                        Account.currency == (fields.currency or workspace.currency),
                    )
                    .order_by(Account.name)
                )
            ).all()
            if action in {"from", "to"}:
                selected_id = resolve_scoped_id(rest[0], (a.id for a in accounts))
                evidence[f"{action}_account"] = str(selected_id)
                if action == "from":
                    evidence.pop("to_account", None)
            ids = {str(a.id): a for a in accounts}
            if evidence.get("from_account") and evidence["from_account"] not in ids:
                raise NotFound("Счёт списания недоступен")
            candidate.fields = fields.to_payload()
            if not evidence.get("from_account") or not evidence.get("to_account"):
                outgoing = not evidence.get("from_account")
                options = [
                    a for a in accounts if outgoing or str(a.id) != evidence.get("from_account")
                ]
                buttons = [
                    (
                        Button(
                            a.name,
                            choice("from" if outgoing else "to", a.id.hex[:8]),
                        ),
                    )
                    for a in options
                ]
                buttons += [
                    (Button("＋ Новый счёт", choice("account")),),
                    (Button("💸 Это расход, а не перевод", choice("kind", "expense")),),
                    cancel,
                ]
                return [
                    Reply(
                        text=(
                            (
                                "🔄 Перевод между своими счетами\n\nС какого счёта переводите?"
                                if accounts
                                else "🔄 Перевод между своими счетами\n\nСчетов пока нет: "
                                "добавьте, откуда и куда переводите деньги."
                            )
                            if outgoing
                            else "🔄 На какой счёт переводите?"
                        )
                        + "\n\nЕсли вы заплатили другому человеку, это расход.",
                        buttons=tuple(buttons),
                    )
                ]
            source_account, target = evidence["from_account"], evidence["to_account"]
            if target not in ids or source_account == target:
                raise ValidationFailed("Выберите два разных доступных счёта")
            evidence["details_confirmed"] = "yes"
            text = (
                "🔄 Перевод "
                f"{views.money(fields.amount_minor, fields.currency or workspace.currency)}"
                f"\n\n{ids[source_account].name} → {ids[target].name}"
                "\n\nПеревод не увеличит расходы и доходы."
            )
        candidate.fields = fields.to_payload()
        if action == "details":
            candidate.version += 1
            draft.version += 1
    return [
        Reply(
            text=text,
            buttons=((Button("✅ Записать", callback("dr", "post", short(draft_id))),), cancel),
        )
    ]


async def account_input(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, pending: Pending, text: str
) -> list[Reply]:
    from fintracker.application.catalog.directory import create_account

    wid = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=wid
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(wid, actor=actor)
        draft, candidates = await load_draft(
            session,
            workspace_id=wid,
            draft_id=uuid.UUID(str(pending.payload["draft_id"])),
            owner_id=actor.user_id,
        )
        if draft.state in {"cancelled", "expired", "posted"}:
            raise NotFound("Черновик больше не активен")
        transfer = next(
            (
                c
                for c in candidates
                if str(c.id) == pending.payload.get("candidate_id")
                and c.state not in {"excluded", "cancelled", "posted"}
                and c.fields.get("kind") == "transfer"
            ),
            None,
        )
        if transfer is None:
            raise ValidationFailed("Создать счёт можно из черновика перевода")
        if transfer.version != pending.payload.get("candidate_version"):
            raise ConflictError("Запись уже изменена. Откройте перевод заново.")
        currency = (
            CandidateFields.from_payload(dict(transfer.fields)).currency or workspace.currency
        )
        await create_account(
            session, uow, actor=actor, name=text, currency=currency, mode="reference"
        )
        transfer.version += 1
        draft.version += 1
        candidate_prefix = transfer.id.hex[:8]
        expected_version = transfer.version
    return (
        await special_action(
            settings,
            actor=actor,
            workspace=workspace,
            draft_id=draft.id,
            candidate_prefix=candidate_prefix,
            expected_version=expected_version,
        )
        or []
    )

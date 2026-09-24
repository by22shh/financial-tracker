"""Встроенные подсказки категорий без AI (FR-23, NFR-14).

Категория бюджета относится к группе по собственному названию («Продукты
питания» → продукты), а трата — по словам описания («пятёрочка», «молоко»).
Подсказка применяется только после правил участника, правил бюджета и
совпадения с названием: явный выбор людей всегда важнее встроенного словаря.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable

from fintracker.application.catalog.normalize import normalize_name

# Группа: (основы в названии категории, основы в описании траты).
_GROUPS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "groceries": (
        ("продукт", "еда дома", "супермаркет", "бакале"),
        (
            "продукт",
            "пятерочк",
            "пятёрочк",
            "магнит",
            "перекрест",
            "перекрёст",
            "лента",
            "ашан",
            "вкусвилл",
            "дикси",
            "азбука вкуса",
            "самокат",
            "окей",
            "метро кэш",
            "fix price",
            "фикс прайс",
            "молок",
            "хлеб",
            "овощ",
            "фрукт",
            "мясо",
            "куриц",
            "сыр",
            "яйц",
            "кефир",
            "крупа",
            "бакале",
            "супермаркет",
            "гипермаркет",
            "рынок",
        ),
    ),
    "eating_out": (
        ("ресторан", "кафе", "еда вне", "общепит", "кофе", "обед"),
        (
            "кофе",
            "кофей",
            "капучино",
            "латте",
            "кафе",
            "ресторан",
            "обед",
            "ужин",
            "завтрак",
            "бар",
            "пицц",
            "суши",
            "роллы",
            "бургер",
            "макдон",
            "вкусно и точка",
            "kfc",
            "ростикс",
            "шаурм",
            "шаверм",
            "столов",
            "доставка еды",
            "яндекс еда",
            "delivery club",
        ),
    ),
    "transport": (
        ("транспорт", "авто", "машин", "проезд", "такси", "бензин"),
        (
            "такси",
            "метро",
            "автобус",
            "трамва",
            "троллейбус",
            "электрич",
            "маршрутк",
            "проезд",
            "бензин",
            "заправ",
            "топливо",
            "дизел",
            "парковк",
            "каршеринг",
            "самокат аренд",
            "яндекс го",
            "uber",
            "мойка",
            "шиномонтаж",
            "автосервис",
            "штраф гибдд",
        ),
    ),
    "housing": (
        ("жиль", "квартир", "дом", "коммунал", "жкх", "аренд"),
        (
            "аренд",
            "квартплат",
            "коммунал",
            "жкх",
            "электричеств",
            "свет за",
            "газ за",
            "вода за",
            "отоплени",
            "ипотек",
            "управляющ",
        ),
    ),
    "telecom": (
        ("связь", "интернет", "телефон", "мобильн"),
        (
            "связь",
            "интернет",
            "мобильн",
            "телефон",
            "сотов",
            "мтс",
            "билайн",
            "мегафон",
            "теле2",
            "tele2",
            "ростелеком",
            "роутер",
        ),
    ),
    "health": (
        ("здоров", "медицин", "аптек", "лекарств"),
        (
            "аптек",
            "лекарств",
            "таблетк",
            "витамин",
            "врач",
            "клиник",
            "стоматолог",
            "зубн",
            "анализ",
            "больниц",
            "медицин",
            "массаж",
        ),
    ),
    "clothes": (
        ("одежд", "обув", "гардероб"),
        (
            "одежд",
            "обув",
            "кроссовк",
            "ботинк",
            "куртк",
            "пальто",
            "джинс",
            "футболк",
            "плать",
            "носки",
            "zara",
            "uniqlo",
            "lamoda",
        ),
    ),
    "leisure": (
        ("досуг", "развлеч", "отдых", "хобби"),
        (
            "кино",
            "театр",
            "концерт",
            "музей",
            "выставк",
            "боулинг",
            "квест",
            "игр",
            "развлеч",
            "книг",
            "хобби",
            "аттракцион",
        ),
    ),
    "subscriptions": (
        ("подписк", "сервис"),
        (
            "подписк",
            "netflix",
            "spotify",
            "кинопоиск",
            "яндекс плюс",
            "icloud",
            "youtube premium",
            "okko",
            "иви",
            "ivi",
            "apple music",
            "chatgpt",
        ),
    ),
    "gifts": (
        ("подар",),
        ("подарок", "подарк", "цветы", "букет", "день рождени"),
    ),
    "kids": (
        ("дет", "ребен", "ребён"),
        ("детск", "игрушк", "подгузн", "садик", "детсад", "кружок", "секци"),
    ),
    "pets": (
        ("питом", "живот", "кот", "собак"),
        ("корм", "ветеринар", "зоомагазин", "наполнитель", "груминг"),
    ),
    "beauty": (
        ("красот", "уход"),
        ("парикмахер", "стрижк", "маникюр", "косметик", "салон красоты", "барбер"),
    ),
    "education": (
        ("образован", "учеб", "учёб", "курс"),
        ("курсы", "обучени", "репетитор", "учебник", "школ", "универс"),
    ),
}


def _fold(value: str) -> str:
    return normalize_name(value).replace("ё", "е")


def _contains(text: str, stems: Iterable[str]) -> str | None:
    folded = _fold(text)
    for stem in stems:
        if re.search(rf"(?<![а-яa-z]){re.escape(_fold(stem))}", folded):
            return stem
    return None


def category_group(category_name: str) -> str | None:
    """К какой встроенной группе относится категория по её названию."""
    normalized = normalize_name(category_name)
    best: tuple[str, int] | None = None
    for group, (name_stems, _) in _GROUPS.items():
        stem = _contains(normalized, name_stems)
        if stem is not None and (best is None or len(stem) > best[1]):
            best = (group, len(stem))
    return best[0] if best else None


def suggest_category(text: str, categories: Iterable[tuple[uuid.UUID, str]]) -> uuid.UUID | None:
    """Категория бюджета по словам описания, если подсказка однозначна."""
    normalized = normalize_name(text)
    if not normalized:
        return None
    by_group: dict[str, list[uuid.UUID]] = {}
    for category_id, name in categories:
        group = category_group(name)
        if group is not None:
            by_group.setdefault(group, []).append(category_id)
    best: tuple[uuid.UUID, int] | None = None
    ambiguous = False
    for group, ids in by_group.items():
        stem = _contains(normalized, _GROUPS[group][1])
        if stem is None:
            continue
        if len(ids) != 1:
            # Две категории одной группы: выбор остаётся за человеком.
            continue
        if best is None or len(stem) > best[1]:
            best = (ids[0], len(stem))
            ambiguous = False
        elif len(stem) == best[1] and ids[0] != best[0]:
            ambiguous = True
    if best is None or ambiguous:
        return None
    return best[0]


__all__ = ["category_group", "suggest_category"]

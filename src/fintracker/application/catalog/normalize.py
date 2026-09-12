"""Нормализация имён справочников (DATA_CONTRACT §2.2)."""

from __future__ import annotations

import unicodedata


def normalize_name(value: str) -> str:
    """Unicode NFKC, регистр и внешние пробелы; исходное написание сохраняется."""
    folded = unicodedata.normalize("NFKC", value).strip()
    collapsed = " ".join(folded.split())
    return collapsed.casefold()


def clean_display_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split())

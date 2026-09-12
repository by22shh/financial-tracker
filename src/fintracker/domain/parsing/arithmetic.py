"""Ограниченный арифметический парсер (FR-10).

``eval`` и выполнение кода недопустимы. Поддерживаются только + - * / и
скобки над десятичными числами, с ограничением длины и числа операторов.
"""

from __future__ import annotations

import re
from decimal import Decimal, DivisionByZero, InvalidOperation

from fintracker.core.errors import ValidationFailed

MAX_EXPRESSION_CHARS = 64
MAX_OPERATORS = 8
_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?|[()+\-*/]|\s+")


class ArithmeticError_(ValidationFailed):
    """Некорректное арифметическое выражение."""


def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        match = _TOKEN_RE.match(expression, position)
        if match is None:
            raise ArithmeticError_("В выражении есть недопустимый символ")
        token = match.group()
        position = match.end()
        if token.strip():
            tokens.append(token.replace(",", "."))
    return tokens


def evaluate(expression: str) -> Decimal:
    """Вычислить выражение точными десятичными числами."""
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise ArithmeticError_("Выражение слишком длинное")
    tokens = _tokenize(expression)
    operators = sum(1 for token in tokens if token in "+-*/")
    if operators > MAX_OPERATORS:
        raise ArithmeticError_("Слишком много операций в выражении")
    if not tokens:
        raise ArithmeticError_("Пустое выражение")

    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str:
        nonlocal position
        token = tokens[position]
        position += 1
        return token

    def parse_expression() -> Decimal:
        value = parse_term()
        while peek() in {"+", "-"}:
            operator = take()
            right = parse_term()
            value = value + right if operator == "+" else value - right
        return value

    def parse_term() -> Decimal:
        value = parse_factor()
        while peek() in {"*", "/"}:
            operator = take()
            right = parse_factor()
            if operator == "*":
                value = value * right
            else:
                if right == 0:
                    raise ArithmeticError_("Деление на ноль")
                value = value / right
        return value

    def parse_factor() -> Decimal:
        token = peek()
        if token is None:
            raise ArithmeticError_("Выражение оборвано")
        if token == "-":
            take()
            return -parse_factor()
        if token == "+":
            take()
            return parse_factor()
        if token == "(":
            take()
            value = parse_expression()
            if peek() != ")":
                raise ArithmeticError_("Не закрыта скобка")
            take()
            return value
        take()
        try:
            return Decimal(token)
        except (InvalidOperation, DivisionByZero) as exc:
            raise ArithmeticError_(f"Не удалось разобрать число {token!r}") from exc

    result = parse_expression()
    if position != len(tokens):
        raise ArithmeticError_("Лишние символы в выражении")
    return result


def looks_like_expression(text: str) -> bool:
    """Похоже ли на арифметику: есть оператор между числами."""
    return bool(re.search(r"\d\s*[+\-*/]\s*\(?\d", text))

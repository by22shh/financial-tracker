"""Ошибки распознавания, которые бот может показать пользователю."""


class DomainError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ValidationFailed(DomainError):
    """Некорректный запрос или ответ модели."""


class ProviderUnavailable(DomainError):
    """Сервис распознавания временно недоступен."""

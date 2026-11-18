"""Ошибки проверки содержимого."""


class ContentDenied(Exception):
    """Модерация отклонила содержимое."""


class FilterUnavailable(Exception):
    """Служба модерации не смогла проверить содержимое."""

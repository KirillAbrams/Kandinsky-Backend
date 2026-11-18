# Здесь один и тот же ключ входа переводится между строкой телефона и строкой основного сервера.
# Обе формы кодируют одинаковые байты, поэтому перевыпуск ключа при каждом запросе не нужен.

"""Перевод ключа сессии между форматом телефона и основного сервера."""

import base64
import re

from fastapi import HTTPException


# Извлекает байты из строки ключа основного сервера и записывает их
# шестнадцатеричными цифрами. Так телефон получает ключ в привычном ему виде.
def pack_token(token):
    return base64.urlsafe_b64decode(token + "=").hex()


# Извлекает ключ из заголовка, проверяет длину и допустимые знаки, затем собирает строку для
# основного сервера. Неподходящий ключ отклоняется до отправки запроса.
def token_from(headers):
    value = headers.get("authorization", "").removeprefix("Bearer ")
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise HTTPException(401, "Войдите в аккаунт")
    return base64.urlsafe_b64encode(bytes.fromhex(value)).decode().rstrip("=")

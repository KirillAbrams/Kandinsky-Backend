# Выполняет запросы к проверочному стенду и регистрирует временного владельца с настоящим письмом.
# Сценарии используют общую сверку ответов и не повторяют разбор соединения.

"""Запросы и вход для ручных проверок на отдельном стенде."""

import json
from getpass import getpass
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class DeploymentClient:
    # Убирает лишнюю косую черту в конце адреса и сохраняет срок ожидания. Запросы могут добавлять
    # путь одним способом и не ждать недоступный стенд бесконечно.
    def __init__(self, origin, *, timeout=15):
        self.origin = origin.rstrip("/")
        self.timeout = timeout

    # Собирает запрос с выбранным входом и ключом повтора, затем сверяет ожидаемый статус.
    # Возвращает поля ответа или байты файла, чтобы тот же клиент мог проверить отказ и
    # скачивание.
    def request(self, path, body=None, token=None, key=None, method=None, *, expected=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if key:
            headers["Idempotency-Key"] = key
        request = Request(
            self.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status, content = response.status, response.read()
        except HTTPError as error:
            if expected is None:
                raise
            with error:
                status, content = error.code, error.read()
        if expected is None:
            return json.loads(content)
        assert status == expected, (path, status, content[:300])
        try:
            return json.loads(content)
        except (ValueError, UnicodeDecodeError):
            return content

    # Запрашивает письмо для проверочного адреса и принимает скрытый ввод цифрового кода.
    # Проверяет его длину и завершает регистрацию только после получения письма.
    def signup(self, email, password):
        if not email:
            raise ValueError("Укажите свободный адрес проверочного ящика")
        self.request("/v1/auth/register/request", {"email": email}, expected=202)
        code = getpass(f"Код регистрации для {email}: ").strip()
        if len(code) != 8 or not code.isascii() or not code.isdigit():
            raise ValueError("В письме должен быть код из восьми цифр")
        return self.request(
            "/v1/auth/register",
            {"email": email, "password": password, "code": code},
            expected=201,
        )

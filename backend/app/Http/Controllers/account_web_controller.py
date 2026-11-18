# Здесь находится страница удаления аккаунта через браузер. Операция требует подтверждения и
# пароля. Страница сверяет выданную отметку с полученной, чтобы запрос нельзя было подставить с
# чужого сайта.

import secrets
from html import escape
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from app.Http.Requests.accounts import Credentials

COOKIE = "__Host-kandinsky-account"
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
}
STYLE = """
    body{font:1rem/1.5 system-ui,sans-serif;color:#18211d;background:#fafcfb;margin:0}
    main{max-width:36rem;margin:6vh auto;padding:1.5rem}
    form{display:grid;gap:1rem;margin-top:1.5rem}
    label:not(.confirm){display:grid;gap:.25rem}
    input,button{font:inherit}
    input:not([type=checkbox]),button{padding:.65rem;border:1px solid #75887d;border-radius:.5rem}
    button{background:#245841;color:#fff;font-weight:600;cursor:pointer}
"""


# Выдаёт форму со случайной отметкой или страницу уже выполненного удаления. На странице
# результата стирает прежнюю отметку, чтобы браузер больше не использовал её при отправке формы.
# Одна и та же отметка попадает в форму и в защищённую запись браузера, чтобы сервер мог их
# сверить.
def deletion_page(message="", *, status=200, csrf=None, deleted=False):
    token = csrf or secrets.token_urlsafe(32)
    title = "Аккаунт удалён" if deleted else "Удаление аккаунта Kandinsky"
    form = (
        ""
        if deleted
        else f"""
      <p>После удаления пропадут история, запросы и медиа на сервере.
      Все активные сеансы завершатся. Вернуть данные нельзя.</p>
      <form action="/account/delete" method="post">
        <input type="hidden" name="csrf" value="{escape(token, quote=True)}">
        <label>Почта <input name="email" type="email" autocomplete="username" maxlength="254" required></label>
        <label>Пароль <input name="password" type="password"
          autocomplete="current-password" maxlength="128" required></label>
        <label class="confirm"><input name="confirm" type="checkbox" value="yes" required>
          Удалить аккаунт и его данные навсегда</label>
        <button type="submit">Удалить аккаунт</button>
      </form>
    """
    )
    note = f'<p role="status">{escape(message)}</p>' if message else ""
    response = HTMLResponse(
        f"""<!doctype html><html lang="ru"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{title}</title><style>{STYLE}</style>
        <main><h1>{title}</h1>{note}{form}</main></html>""",
        status_code=status,
        headers=HEADERS,
    )
    if deleted:
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
    else:
        response.set_cookie(
            COOKIE, token, max_age=900, secure=True, httponly=True, samesite="strict"
        )
    return response


# Ограничивает размер формы, сверяет отметку из формы и требует явного согласия. Затем
# проверяет пароль через основной сервер и запускает удаление, чтобы одна открытая страница не
# могла удалить аккаунт. Если удаление после входа сорвалось, обработчик пробует закрыть временный
# сеанс.
async def delete_from_browser(request: Request, adapter):
    if (
        request.headers.get("content-type", "").split(";", 1)[0]
        != "application/x-www-form-urlencoded"
    ):
        return deletion_page("Откройте форму удаления заново.", status=400)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8192:
            return deletion_page("Запрос слишком большой.", status=413)
        body.extend(chunk)
    try:
        fields = parse_qs(body.decode("utf-8"), max_num_fields=4, strict_parsing=True)
        token = fields["csrf"][0]
        cookie = request.cookies.get(COOKIE, "")
        if not cookie or not secrets.compare_digest(token.encode(), cookie.encode()):
            return deletion_page("Откройте форму удаления заново.", status=400)
        if fields.get("confirm") != ["yes"]:
            return deletion_page("Подтвердите удаление аккаунта.", status=400, csrf=token)
        credentials = Credentials(email=fields["email"][0], password=fields["password"][0])
    except (KeyError, ValueError):
        return deletion_page("Проверьте почту и пароль.", status=400)

    profile = None
    try:
        profile = await adapter.studio.request(
            "POST", "/v1/auth/login", json=credentials.model_dump(mode="json")
        )
        await adapter.delete_account(profile["user_id"], profile["token"], credentials.password)
    except HTTPException as error:
        if profile is not None:
            try:
                await adapter.studio.request("POST", "/v1/auth/logout", profile["token"])
            except HTTPException:
                pass
        message = (
            "Проверьте почту и пароль."
            if error.status_code in (401, 403, 422)
            else "Не удалось подтвердить удаление. Попробуйте позже."
        )
        return deletion_page(message, status=error.status_code)
    return deletion_page("Активные сеансы завершены.", deleted=True)

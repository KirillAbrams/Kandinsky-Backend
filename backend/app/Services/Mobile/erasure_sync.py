# Здесь служба связи забирает у основного сервера сведения об удалённых чатах и файлах.
# Подтверждение отправляется после местной очистки, чтобы запись не потерялась при обрыве связи.

"""Подтверждает очистку только после стирания местного содержимого."""

import asyncio
import uuid
from collections import defaultdict

from app.Http.Resources.Mobile.messages import preview_blob_id


# Переводит номера файлов в формат телефона и убирает запомненные сведения о них. Отдельные файлы
# стирает сразу, а изменения одного запроса собирает вместе, чтобы правильно обновить ответ и его
# вложения.
async def apply_items(adapter, owner, items):
    changes = []
    for item in items:
        asset = item["asset_id"]
        changes.append(
            {
                **item,
                "blob": uuid.UUID(asset).hex if asset else None,
                "preview": preview_blob_id(asset) if asset else None,
            }
        )
    by_root = defaultdict(list)
    for item in changes:
        asset = item["asset_id"]
        if asset:
            adapter.metadata_cache.pop((owner, asset), None)
        if item.get("asset_only"):
            await asyncio.to_thread(
                adapter.state.erase_assets,
                owner,
                [{"blobId": item["blob"], "previewBlobId": item["preview"]}],
            )
        else:
            by_root[(item["thread_id"], item["root_id"])].append(item)
    for (thread, root), removed in by_root.items():
        chat = adapter.state.identity(owner, "thread", thread)
        prompt = adapter.state.identity(owner, "prompt", root)
        answer = adapter.state.identity(owner, "answer", root)
        retire = any(item["prompt"] or item.get("retire_answer") for item in removed)
        await asyncio.to_thread(
            adapter.state.remove_content, owner, chat, root, prompt, answer, removed, retire
        )


# Забирает сведения об удалённых чатах порциями и переводит номера чатов в местные. Подтверждает
# каждую порцию после очистки, иначе сервер мог бы убрать запись из очереди раньше времени.
async def sync_thread_erasures(adapter, key):
    path = "/v1/internal/thread-erasures"
    headers = {"X-Kandinsky-Erasure-Key": key}
    while True:
        items = (await adapter.studio.request("GET", path, headers=headers))["items"]
        for item in items:
            owner, thread = str(uuid.UUID(item["owner_id"])), str(uuid.UUID(item["id"]))
            chat = adapter.state.identity(owner, "thread", thread)
            await asyncio.to_thread(adapter.state.erase_chat, owner, chat)
        if items:
            await adapter.studio.request(
                "POST", path + "/ack", headers=headers, json=[item["id"] for item in items]
            )
        if len(items) < 128:
            return


# Группирует удалённое содержимое по владельцам, затем применяет изменения и подтверждает всю
# порцию. При прерванной очистке сервер сможет снова прислать те же записи.
async def sync_content_erasures(adapter, key):
    path = "/v1/internal/content-erasures"
    headers = {"X-Kandinsky-Erasure-Key": key}
    while True:
        items = (await adapter.studio.request("GET", path, headers=headers))["items"]
        owners = defaultdict(list)
        for item in items:
            owners[str(uuid.UUID(item["owner_id"]))].append(item)
        for owner, changes in owners.items():
            await apply_items(adapter, owner, changes)
        if items:
            await adapter.studio.request(
                "POST", path + "/ack", headers=headers, json=[item["id"] for item in items]
            )
        if len(items) < 128:
            return

#!/usr/bin/env bash
# Собирает отдельный стенд для проверки и выдаёт готовый вход.

set -euo pipefail
cd "$(dirname "$0")/.."
if docker compose version >/dev/null 2>&1; then
    compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    compose=(docker-compose)
else
    printf 'Установи Docker с Compose и запусти Docker.\n' >&2
    exit 1
fi

unset POSTGRES_PASSWORD STUDIO_MIGRATION_PASSWORD STUDIO_API_PASSWORD STUDIO_WORKER_PASSWORD
unset STUDIO_BRIDGE_ERASURE_KEY KANDINSKY_REVIEW_PASSWORD KANDINSKY_SERVER_IMAGE
compose+=(--project-name kandinsky-review --env-file .env.review -f compose.yaml -f compose.review.yaml)
case "${1:-start}" in
    stop)
        "${compose[@]}" down
        exit
        ;;
    start)
        ;;
    *)
        printf 'Используй ./tools/dev.sh для запуска или ./tools/dev.sh stop для остановки.\n' >&2
        exit 1
        ;;
esac

if [[ -t 0 ]]; then
    if [[ -z "${KANDINSKY_REVIEW_GPU_URL:-}" ]]; then
        read -r -p 'Адрес службы генерации: ' KANDINSKY_REVIEW_GPU_URL
    fi
    if [[ -z "${KANDINSKY_REVIEW_GPU_TOKEN:-}" ]]; then
        read -rs -p 'Ключ службы генерации: ' KANDINSKY_REVIEW_GPU_TOKEN
        printf '\n'
    fi
fi

if [[ -z "${KANDINSKY_REVIEW_GPU_URL:-}" || -z "${KANDINSKY_REVIEW_GPU_TOKEN:-}" ]]; then
    printf 'Для запуска нужны адрес и ключ службы генерации.\n' >&2
    printf 'Введи их при запуске или передай KANDINSKY_REVIEW_GPU_URL и KANDINSKY_REVIEW_GPU_TOKEN.\n' >&2
    exit 1
fi
export KANDINSKY_REVIEW_GPU_URL KANDINSKY_REVIEW_GPU_TOKEN

if [[ ! -f .env.review ]]; then
    umask 077
    settings=$(mktemp .env.review.XXXXXX)
    trap 'rm -f "$settings"' EXIT
    docker run --rm -i python:3.12-slim python - --review < tools/configure.py > "$settings"
    ln "$settings" .env.review 2>/dev/null || [[ -f .env.review ]]
fi

# Все серверные службы используют один образ, собранный для api.
"${compose[@]}" build api
"${compose[@]}" run --rm --no-deps -T --entrypoint python worker - < tools/check_review_gpu.py
"${compose[@]}" up --no-build --wait --wait-timeout 900
"${compose[@]}" exec -T api python - < tools/seed_review.py
printf '\nОписание запросов http://127.0.0.1:8098/docs\n'
printf 'Служба связи http://127.0.0.1:8449/health\n'

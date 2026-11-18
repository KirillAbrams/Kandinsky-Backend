#!/usr/bin/env bash
# Готовит отдельное окружение и загружает веса до начала вычислений.
# Время загрузки не расходует часы аренды видеокарты.
set -euo pipefail
# Скачиваем веса до аренды, чтобы не платить за видеокарту во время загрузки.
runner_dir="$(cd "$(dirname "$0")" && pwd)"
backend_dir="$(cd "$runner_dir/../../../.." && pwd)"
cd "$backend_dir/.."
python3 -m venv .gpu-venv
.gpu-venv/bin/pip install -r "$runner_dir/requirements.txt"
.gpu-venv/bin/pip freeze > .gpu-venv/runtime-lock.txt
KANDINSKY_MODELS="${KANDINSKY_MODELS:-$PWD/vendor/models}"
export KANDINSKY_MODELS
PYTHONPATH="$backend_dir" .gpu-venv/bin/python - <<'PYTHON'
import os

from app.Services.Gpu.Runner.models import prepare_models

prepare_models(os.environ["KANDINSKY_MODELS"])
PYTHON
printf 'Models prepared. Run authenticated app.Services.Gpu.Runner.bridge on your private server.\n'

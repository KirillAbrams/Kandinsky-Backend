# Сверяет чувствительность проверок к намеренной поломке в отдельной временной копии. Успех здесь
# засчитываем только при отказе по той причине, которую должна заметить проверка.

"""Проверяет, замечают ли проверки намеренно внесённую ошибку в копии программы"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[4]
python = root / "backend/.venv/bin/python"
mutations = [
    (
        "tenant",
        "app/Policies/ownership.py",
        "if item is None or item.user_id != user.id:",
        "if item is None:",
        "tests/feature/studio/test_security.py::test_private_jobs_and_assets_and_revocation",
    ),
    (
        "fingerprint",
        "app/Services/Generation/submit_jobs.py",
        "or [member.fingerprint for member in existing_jobs] != fingerprints",
        "or False",
        "tests/feature/studio/test_generation_idempotency.py",
    ),
]
results = []
for name, module, before, after, test in mutations:
    with tempfile.TemporaryDirectory(prefix="studio-mutation-") as temp:
        target = Path(temp)
        for folder in ("app", "bootstrap", "config", "routes", "database"):
            shutil.copytree(root / "backend" / folder, target / folder)
        shutil.copytree(root / "backend/tests", target / "tests")
        shutil.copy(root / "backend/pyproject.toml", target / "pyproject.toml")
        path = target / module
        source = path.read_text()
        assert before in source, f"Stale mutation locator: {name}"
        path.write_text(source.replace(before, after, 1))
        env = {**os.environ, "PYTHONPATH": str(target)}
        run = subprocess.run(
            [str(python), "-m", "pytest", test, "-q"],
            cwd=target,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        killed = run.returncode == 1 and "AssertionError" in run.stdout
        results.append({"mutation": name, "killed": killed, "exit_code": run.returncode})
        assert killed, run.stdout[-1500:] + run.stderr[-1000:]
print(json.dumps({"ok": True, "mutations": results}))

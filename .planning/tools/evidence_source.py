"""Content identity of the sources and configuration exercised by a run.

Reports and generated evidence are deliberately excluded. Working-tree files,
including untracked source files, are hashed rather than Git status labels.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

DIRECTORIES = (
    "src",
    "tests",
    "tools",
    "scripts",
    "deploy",
    "docker",
    "ops",
    "infrastructure",
    "config",
    ".github",
    ".planning/tools",
)
ROOT_PATTERNS = (
    "*.toml",
    "*.ini",
    "*.cfg",
    "*.yaml",
    "*.yml",
    "*.lock",
    "*.json",
    "*.py",
    "Dockerfile*",
    "Makefile*",
    "requirements*.txt",
)
ROOT_FILES = (
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".dockerignore",
    ".env.example",
    "pytest.ini",
    "mypy.ini",
    "ruff.toml",
    ".ruff.toml",
    "setup.cfg",
    "setup.py",
    "requirements.txt",
    "requirements-dev.txt",
    "conftest.py",
    "tox.ini",
)
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis"}


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_source(root: Path) -> dict[str, Any]:
    paths = {root / name for name in ROOT_FILES if (root / name).is_file()}
    for pattern in ROOT_PATTERNS:
        paths.update(path for path in root.glob(pattern) if path.is_file())
    for name in DIRECTORIES:
        directory = root / name
        if directory.exists():
            paths.update(
                path
                for path in directory.rglob("*")
                if path.is_file()
                and not (set(path.relative_to(root).parts) & IGNORED_PARTS)
                and path.suffix not in {".pyc", ".pyo"}
            )
    files = {path.relative_to(root).as_posix(): file_digest(path) for path in sorted(paths)}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"version": 1, "sha256": digest, "files": files}


def validate_source(report: dict[str, Any], root: Path) -> list[str]:
    before, after = report.get("source_before"), report.get("source_after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["Нет content manifest исходников до/после прогона: соберите новые доказательства"]
    current = capture_source(root)
    if before != after:
        return ["Исходники менялись во время прогона; результаты недействительны"]
    if before != current:
        return ["Содержимое src/tests/config/build/tools отличается от проверенного прогона"]
    return []

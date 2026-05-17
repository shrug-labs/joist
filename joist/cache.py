from __future__ import annotations

import glob
import hashlib
import json
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from .models import Project, Target
from .render import render_template


class TaskCache:
    def __init__(self, workspace_root: Path, cache_dir: Path):
        self.workspace_root = workspace_root
        self.cache_dir = cache_dir

    def key(
        self,
        project: Project,
        target: Target,
        cwd: Path,
        env: dict[str, str],
        commands: tuple[str, ...],
    ) -> str:
        digest = hashlib.sha256()
        payload = {
            "project": project.name,
            "target": target.name,
            "cwd": _path_label(self.workspace_root, cwd),
            "env": env,
            "commands": list(commands),
            "inputs": list(target.inputs),
        }
        digest.update(json.dumps(payload, sort_keys=True).encode())
        for path, value in self._input_hashes(project, target, cwd):
            digest.update(path.encode())
            digest.update(value.encode())
        return digest.hexdigest()

    def read(self, key: str) -> dict[str, Any] | None:
        cache_file = self.cache_dir / f"{key}.json"
        if not cache_file.exists():
            return None
        try:
            with cache_file.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, JSONDecodeError):
            return None

    def write(self, key: str, result: dict[str, Any]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self.cache_dir / f"{key}.json"
        with cache_file.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)

    def outputs_present(self, project: Project, target: Target, cwd: Path) -> bool:
        if not target.outputs:
            return True
        for pattern in target.outputs:
            if not self._expand(project, pattern, cwd):
                return False
        return True

    def _input_hashes(self, project: Project, target: Target, cwd: Path) -> list[tuple[str, str]]:
        inputs = target.inputs or (
            "{project_root}/**/*.py",
            "{project_root}/pyproject.toml",
            "{workspace_root}/pyproject.toml",
            "{workspace_root}/uv.lock",
            "{workspace_root}/joist.toml",
        )
        values: list[tuple[str, str]] = []
        for pattern in inputs:
            matches = self._expand(project, pattern, cwd)
            if not matches:
                values.append((pattern, "missing"))
                continue
            for path in matches:
                if path.is_dir():
                    continue
                try:
                    label = str(path.relative_to(self.workspace_root))
                except ValueError:
                    label = str(path)
                values.append((label, _hash_file(path)))
        return sorted(values)

    def _expand(self, project: Project, pattern: str, cwd: Path) -> list[Path]:
        rendered = render_template(pattern, self.workspace_root, project)
        path = Path(rendered)
        if not path.is_absolute():
            path = cwd / path
        matches = [Path(match) for match in glob.glob(str(path), recursive=True)]
        if path.exists() and path not in matches:
            matches.append(path)
        return sorted({match.resolve() for match in matches})


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_label(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)

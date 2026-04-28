from __future__ import annotations

import glob
import hashlib
import json
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from .models import Project, Target


class TaskCache:
    def __init__(self, workspace_root: Path, cache_dir: Path):
        self.workspace_root = workspace_root
        self.cache_dir = cache_dir

    def key(self, project: Project, target: Target, command: str, extra_args: list[str]) -> str:
        digest = hashlib.sha256()
        payload = {
            "project": project.name,
            "target": target.name,
            "command": command,
            "args": extra_args,
            "inputs": list(target.inputs),
        }
        digest.update(json.dumps(payload, sort_keys=True).encode())
        for path, value in self._input_hashes(project, target):
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

    def outputs_present(self, project: Project, target: Target) -> bool:
        if not target.outputs:
            return True
        for pattern in target.outputs:
            if not self._expand(project, pattern):
                return False
        return True

    def _input_hashes(self, project: Project, target: Target) -> list[tuple[str, str]]:
        inputs = target.inputs or (
            "{project_root}/**/*.py",
            "{project_root}/pyproject.toml",
            "pyproject.toml",
            "uv.lock",
            "joist.toml",
        )
        values: list[tuple[str, str]] = []
        for pattern in inputs:
            matches = self._expand(project, pattern)
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

    def _expand(self, project: Project, pattern: str) -> list[Path]:
        rendered = pattern.format(
            project=project.name,
            project_name=project.name,
            package_name=project.package_name,
            project_root=str(project.root),
            workspace_root=str(self.workspace_root),
        )
        path = Path(rendered)
        if not path.is_absolute():
            path = self.workspace_root / path
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

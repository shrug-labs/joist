from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Target:
    name: str
    commands: tuple[str, ...]
    cwd: str = "{workspace_root}"
    env: dict[str, str] = field(default_factory=dict)
    if_exists: tuple[str, ...] = ()
    cache: bool = True
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class Project:
    name: str
    root: Path
    package_name: str
    version: str
    type: str = "lib"
    private: bool = False
    depends_on: tuple[str, ...] = ()
    targets: dict[str, Target] = field(default_factory=dict)

    def target(self, name: str) -> Target | None:
        return self.targets.get(name)


@dataclass(frozen=True)
class WorkspaceConfig:
    root: Path
    project_globs: tuple[str, ...]
    cache_dir: Path
    project_excludes: tuple[str, ...] = ()
    affects_all: tuple[str, ...] = ()
    default_base: str = "main"
    target_defaults: dict[str, Target] = field(default_factory=dict)

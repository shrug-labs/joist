from __future__ import annotations

import json
import re
import subprocess
import tomllib
from collections import defaultdict, deque
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from .models import Project, Target, WorkspaceConfig

DEFAULT_PROJECT_GLOBS = ("packages/*", "apps/*")
DEFAULT_AFFECTS_ALL = ("joist.toml", "pyproject.toml", "uv.lock")
DEPENDENCY_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")


class WorkspaceError(RuntimeError):
    pass


class Workspace:
    def __init__(self, config: WorkspaceConfig, projects: dict[str, Project]):
        self.config = config
        self.root = config.root
        self.projects = projects
        self.reverse_dependencies = _reverse_dependencies(projects)

    @classmethod
    def load(cls, start: Path | None = None) -> "Workspace":
        root = find_workspace_root(start or Path.cwd())
        config = load_config(root)
        projects = discover_projects(config)
        if not projects:
            raise WorkspaceError(
                "No projects found. Run `uv run joist init` or check joist.toml."
            )
        return cls(config, projects)

    def project(self, name: str) -> Project:
        try:
            return self.projects[name]
        except KeyError as exc:
            names = ", ".join(sorted(self.projects))
            raise WorkspaceError(f"Unknown project '{name}'. Known projects: {names}") from exc

    def sorted_projects(self, names: set[str] | None = None) -> list[Project]:
        selected = set(self.projects) if names is None else set(names)
        indegree = {name: 0 for name in selected}
        outgoing: dict[str, list[str]] = {name: [] for name in selected}

        for name in selected:
            project = self.projects[name]
            for dep in project.depends_on:
                if dep in selected:
                    indegree[name] += 1
                    outgoing[dep].append(name)

        queue = deque(sorted(name for name, count in indegree.items() if count == 0))
        ordered: list[str] = []
        while queue:
            name = queue.popleft()
            ordered.append(name)
            for dependent in sorted(outgoing[name]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    queue.append(dependent)

        if len(ordered) != len(selected):
            cycle = ", ".join(sorted(selected - set(ordered)))
            raise WorkspaceError(f"Dependency cycle detected among: {cycle}")

        return [self.projects[name] for name in ordered]

    def transitive_dependencies(self, name: str) -> set[str]:
        seen: set[str] = set()

        def visit(project_name: str) -> None:
            for dep in self.projects[project_name].depends_on:
                if dep not in self.projects or dep in seen:
                    continue
                seen.add(dep)
                visit(dep)

        visit(name)
        return seen

    def with_dependencies(self, names: set[str]) -> set[str]:
        selected = set(names)
        for name in list(names):
            selected.update(self.transitive_dependencies(name))
        return selected

    def affected_by_files(self, files: list[str]) -> set[str]:
        normalized = [_changed_path(self.root, path) for path in files if path.strip()]
        if any(_matches_any(str(path).replace("\\", "/"), self.config.affects_all) for path in normalized):
            return set(self.projects)

        changed: set[str] = set()
        for project in self.projects.values():
            project_root = project.root.relative_to(self.root)
            root_text = "." if str(project_root) == "." else str(project_root).replace("\\", "/")
            for path in normalized:
                text = str(path).replace("\\", "/")
                if root_text == "." or text == root_text or text.startswith(f"{root_text}/"):
                    changed.add(project.name)
                    break

        affected = set(changed)
        queue = deque(changed)
        while queue:
            name = queue.popleft()
            for dependent in self.reverse_dependencies.get(name, ()):
                if dependent not in affected:
                    affected.add(dependent)
                    queue.append(dependent)
        return affected

    def changed_files(self, base: str, head: str | None = None) -> list[str]:
        if head:
            args = ["git", "-C", str(self.root), "diff", "--name-only", f"{base}...{head}"]
            return _git_lines(args)

        files = set(_git_lines(["git", "-C", str(self.root), "diff", "--name-only", f"{base}...HEAD"]))
        files.update(_git_lines(["git", "-C", str(self.root), "diff", "--name-only"]))
        files.update(_git_lines(["git", "-C", str(self.root), "diff", "--name-only", "--cached"]))
        files.update(_git_lines(["git", "-C", str(self.root), "ls-files", "--others", "--exclude-standard"]))
        return sorted(files)

    def affected(self, base: str | None = None, head: str | None = None) -> set[str]:
        changed = self.changed_files(base or self.config.default_base, head)
        return self.affected_by_files(changed)

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)

    def as_dict(self, names: set[str] | None = None) -> dict[str, Any]:
        selected = set(self.projects) if names is None else set(names)
        payload = {
            "root": str(self.root),
            "projects": {
                name: {
                    "root": str(project.root.relative_to(self.root)),
                    "package": project.package_name,
                    "version": project.version,
                    "type": project.type,
                    "private": project.private,
                    "dependsOn": list(project.depends_on),
                    "targets": sorted(project.targets),
                }
                for name, project in sorted(self.projects.items())
                if name in selected
            },
        }
        return payload


def find_workspace_root(start: Path) -> Path:
    start = start.resolve()
    candidates = [start, *start.parents]
    for candidate in candidates:
        if (candidate / "joist.toml").exists():
            return candidate
    for candidate in candidates:
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise WorkspaceError("Could not find joist.toml or pyproject.toml.")


def load_config(root: Path) -> WorkspaceConfig:
    root = root.resolve()
    joist_path = root / "joist.toml"
    data = _read_toml(joist_path) if joist_path.exists() else {}
    workspace_data = _table(data.get("workspace", {}), "workspace")
    uv_members, uv_excludes = _uv_workspace_globs(root)

    project_globs = _string_sequence(workspace_data.get("projects"), "workspace.projects") or uv_members or DEFAULT_PROJECT_GLOBS
    project_excludes = _string_sequence(workspace_data.get("exclude"), "workspace.exclude") or uv_excludes
    configured_affects_all = _string_sequence(workspace_data.get("affects_all"), "workspace.affects_all")
    affects_all = tuple(dict.fromkeys(DEFAULT_AFFECTS_ALL + configured_affects_all))
    cache_dir = _workspace_path(root, _string_value(workspace_data.get("cache_dir", ".joist/cache"), "workspace.cache_dir"))
    default_base = _string_value(workspace_data.get("default_base", "main"), "workspace.default_base")
    target_defaults = _parse_targets(_table(data.get("target_defaults", {}), "target_defaults"), {})

    return WorkspaceConfig(
        root=root,
        project_globs=project_globs,
        project_excludes=project_excludes,
        cache_dir=cache_dir,
        default_base=default_base,
        affects_all=affects_all,
        target_defaults=target_defaults,
    )


def discover_projects(config: WorkspaceConfig) -> dict[str, Project]:
    candidates: list[Path] = []
    for pattern in config.project_globs:
        matches = [config.root] if pattern == "." else sorted(config.root.glob(pattern))
        for match in matches:
            project_root = match if match.is_dir() else match.parent
            if _is_excluded(config.root, project_root, config.project_excludes):
                continue
            if (project_root / "pyproject.toml").exists() and project_root not in candidates:
                candidates.append(project_root)

    if not candidates and (config.root / "pyproject.toml").exists():
        candidates.append(config.root)

    projects = [_project_from_pyproject(config, path) for path in candidates]
    _ensure_unique_projects(projects)
    package_to_project = {_normalize(project.package_name): project.name for project in projects}

    hydrated: dict[str, Project] = {}
    for project in projects:
        inferred = _internal_dependencies(project.root / "pyproject.toml", package_to_project)
        deps = tuple(sorted(set(project.depends_on).union(inferred) - {project.name}))
        hydrated[project.name] = Project(
            name=project.name,
            root=project.root,
            package_name=project.package_name,
            version=project.version,
            type=project.type,
            private=project.private,
            depends_on=deps,
            targets=project.targets,
        )

    return hydrated


def _project_from_pyproject(config: WorkspaceConfig, project_root: Path) -> Project:
    pyproject = _read_toml(project_root / "pyproject.toml")
    project_data = _table(pyproject.get("project", {}), "project")
    tool_data = _table(pyproject.get("tool", {}), "tool")
    joist_data = _table(tool_data.get("joist", {}), "tool.joist")
    package_name = _string_value(project_data.get("name") or joist_data.get("name") or project_root.name, "project.name")
    name = _string_value(joist_data.get("name") or package_name, "tool.joist.name")
    targets = _parse_targets(_table(joist_data.get("targets", {}), "tool.joist.targets"), config.target_defaults)
    return Project(
        name=name,
        root=project_root,
        package_name=package_name,
        version=_string_value(project_data.get("version", "0.0.0"), "project.version"),
        type=_string_value(joist_data.get("type", "lib"), "tool.joist.type"),
        private=_bool_value(joist_data.get("private", False), "tool.joist.private"),
        depends_on=_string_sequence(joist_data.get("depends_on"), "tool.joist.depends_on"),
        targets=targets,
    )


def _parse_targets(raw_targets: dict[str, Any], defaults: dict[str, Target]) -> dict[str, Target]:
    targets = dict(defaults)
    for name, raw in raw_targets.items():
        base = targets.get(name)
        if isinstance(raw, str):
            raw = {"command": raw}
        if not isinstance(raw, dict):
            raise WorkspaceError(f"Target '{name}' must be a string or table.")
        commands = _target_commands(name, raw, base)
        cwd = raw.get("cwd", base.cwd if base else "{workspace_root}")
        if not isinstance(cwd, str):
            raise WorkspaceError(f"Target '{name}' cwd must be a string.")
        targets[name] = Target(
            name=name,
            commands=commands,
            cwd=cwd,
            env=_target_env(name, raw, base),
            if_exists=_string_tuple(name, raw.get("if_exists", base.if_exists if base else ()), "if_exists"),
            cache=_bool_value(raw.get("cache", base.cache if base else True), f"Target '{name}' cache"),
            inputs=_string_tuple(name, raw.get("inputs", base.inputs if base else ()), "inputs"),
            outputs=_string_tuple(name, raw.get("outputs", base.outputs if base else ()), "outputs"),
            depends_on=_string_tuple(name, raw.get("depends_on", base.depends_on if base else ()), "depends_on"),
        )
    return targets


def _target_commands(name: str, raw: dict[str, Any], base: Target | None) -> tuple[str, ...]:
    has_command = "command" in raw
    has_commands = "commands" in raw
    if has_command and has_commands:
        raise WorkspaceError(f"Target '{name}' must use either command or commands, not both.")
    if has_commands:
        commands = _string_sequence(raw["commands"], f"Target '{name}' commands")
        if not commands:
            raise WorkspaceError(f"Target '{name}' commands must be a non-empty list of strings.")
        return commands
    if has_command:
        command = _string_value(raw["command"], f"Target '{name}' command")
        if not command:
            raise WorkspaceError(f"Target '{name}' command must be a non-empty string.")
        return (command,)
    if base:
        return base.commands
    raise WorkspaceError(f"Target '{name}' is missing command or commands.")


def _target_env(name: str, raw: dict[str, Any], base: Target | None) -> dict[str, str]:
    env = dict(base.env) if base else {}
    if "env" not in raw:
        return env
    raw_env = _table(raw["env"], f"Target '{name}' env")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in raw_env.items()):
        raise WorkspaceError(f"Target '{name}' env must be a table of string keys and values.")
    env.update(raw_env)
    return env


def _string_tuple(name: str, value: Any, field: str) -> tuple[str, ...]:
    return _string_sequence(value, f"Target '{name}' {field}", allow_single=True)


def _internal_dependencies(pyproject_path: Path, package_to_project: dict[str, str]) -> set[str]:
    data = _read_toml(pyproject_path)
    project_data = data.get("project", {})
    dependency_specs: list[str] = list(project_data.get("dependencies", ()))
    for values in project_data.get("optional-dependencies", {}).values():
        dependency_specs.extend(values)

    internal: set[str] = set()
    for spec in dependency_specs:
        match = DEPENDENCY_RE.match(spec)
        if not match:
            continue
        dependency_name = _normalize(match.group(1))
        if dependency_name in package_to_project:
            internal.add(package_to_project[dependency_name])
    return internal


def _reverse_dependencies(projects: dict[str, Project]) -> dict[str, tuple[str, ...]]:
    reverse: dict[str, list[str]] = defaultdict(list)
    for name, project in projects.items():
        for dep in project.depends_on:
            if dep in projects:
                reverse[dep].append(name)
    return {name: tuple(sorted(dependents)) for name, dependents in reverse.items()}


def _ensure_unique_projects(projects: list[Project]) -> None:
    seen_names: dict[str, Path] = {}
    seen_packages: dict[str, Path] = {}
    for project in projects:
        if project.name in seen_names:
            first = seen_names[project.name]
            raise WorkspaceError(
                f"Duplicate project name '{project.name}' in {first} and {project.root}."
            )
        seen_names[project.name] = project.root

        normalized_package = _normalize(project.package_name)
        if normalized_package in seen_packages:
            first = seen_packages[normalized_package]
            raise WorkspaceError(
                f"Duplicate package name '{project.package_name}' in {first} and {project.root}."
            )
        seen_packages[normalized_package] = project.root


def _is_excluded(root: Path, project_root: Path, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    relative = project_root.relative_to(root).as_posix()
    for pattern in patterns:
        normalized = pattern.rstrip("/")
        if fnmatch(relative, normalized) or fnmatch(f"{relative}/", f"{normalized}/"):
            return True
    return False


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(value, pattern.rstrip("/")) for pattern in patterns)


def _table(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkspaceError(f"{name} must be a table.")
    return value


def _string_value(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise WorkspaceError(f"{name} must be a string.")
    return value


def _bool_value(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise WorkspaceError(f"{name} must be true or false.")
    return value


def _string_sequence(value: Any, name: str, *, allow_single: bool = False) -> tuple[str, ...]:
    if value is None:
        return ()
    if allow_single and isinstance(value, str):
        return (value,)
    if not isinstance(value, list | tuple):
        kind = "a string or list of strings" if allow_single else "a list of strings"
        raise WorkspaceError(f"{name} must be {kind}.")
    if not all(isinstance(item, str) for item in value):
        raise WorkspaceError(f"{name} must contain only strings.")
    return tuple(value)


def _workspace_path(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError(f"Workspace paths must stay inside {root}: {value}") from exc
    return resolved


def _changed_path(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        return path
    try:
        return path.resolve().relative_to(root)
    except ValueError:
        return path


def _uv_workspace_globs(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.exists():
        return (), ()
    data = _read_toml(pyproject_path)
    tool = _table(data.get("tool", {}), "tool")
    uv = _table(tool.get("uv", {}), "tool.uv")
    workspace = _table(uv.get("workspace", {}), "tool.uv.workspace")
    return (
        _string_sequence(workspace.get("members"), "tool.uv.workspace.members"),
        _string_sequence(workspace.get("exclude"), "tool.uv.workspace.exclude"),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _git_lines(args: list[str]) -> list[str]:
    try:
        completed = subprocess.run(args, check=True, capture_output=True, text=True)
    except OSError as exc:
        raise WorkspaceError(f"Could not run Git: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() or exc.stdout.strip()
        detail = f": {stderr}" if stderr else ""
        raise WorkspaceError(f"Git command failed ({' '.join(args[3:])}){detail}") from exc
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]

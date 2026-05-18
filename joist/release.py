from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .workspace import Workspace, WorkspaceError


@dataclass(frozen=True)
class VersionChange:
    path: Path
    old: str
    new: str


def bump_workspace_version(workspace: Workspace, bump: str, dry_run: bool = False) -> list[VersionChange]:
    projects = sorted(workspace.projects.values(), key=lambda item: item.name)
    public_projects = [project for project in projects if not project.private]
    if not public_projects:
        raise WorkspaceError("No public projects to version.")

    current_versions = {project.version for project in public_projects}
    if bump in {"major", "minor", "patch"} and len(current_versions) > 1:
        versions = ", ".join(sorted(current_versions))
        raise WorkspaceError(f"Fixed-version release needs one current version; found: {versions}.")

    current = sorted(current_versions)[0]
    next_version = bump_version(current, bump)
    public_package_versions = {
        project.package_name: next_version
        for project in public_projects
    }
    changes: list[VersionChange] = []

    for project in public_projects:
        pyproject = project.root / "pyproject.toml"
        changes.append(VersionChange(pyproject, project.version, next_version))
        if not dry_run:
            _replace_project_version(pyproject, next_version)

        init_file = _module_init_file(project.root, project.package_name)
        old = _read_module_version(init_file) if init_file else None
        if init_file and old is not None:
            changes.append(VersionChange(init_file, old, next_version))
            if not dry_run:
                _replace_module_version(init_file, next_version)

    if not dry_run:
        for project in projects:
            _replace_internal_dependency_versions(project.root / "pyproject.toml", public_package_versions)

    version_file = workspace.root / "VERSION"
    if version_file.exists() or not dry_run:
        old = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else current
        changes.append(VersionChange(version_file, old, next_version))
        if not dry_run:
            version_file.write_text(f"{next_version}\n", encoding="utf-8")

    return changes


def bump_version(version: str, bump: str) -> str:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", version)
    if not match:
        raise WorkspaceError(f"Unsupported version '{version}'. Expected MAJOR.MINOR.PATCH.")
    major, minor, patch = (int(part) for part in match.groups())
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if re.match(r"^\d+\.\d+\.\d+$", bump):
        return bump
    raise WorkspaceError("Version must be 'major', 'minor', 'patch', or an explicit MAJOR.MINOR.PATCH.")


def _replace_project_version(pyproject: Path, version: str) -> None:
    lines = pyproject.read_text(encoding="utf-8").splitlines()
    in_project = False
    replaced = False
    output: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
        if in_project and re.match(r"version\s*=", stripped):
            output.append(f'version = "{version}"')
            replaced = True
        else:
            output.append(line)

    if not replaced:
        raise WorkspaceError(f"No [project] version found in {pyproject}.")
    pyproject.write_text("\n".join(output) + "\n", encoding="utf-8")


def _replace_internal_dependency_versions(pyproject: Path, versions: dict[str, str]) -> None:
    normalized_versions = {_normalize_package_name(name): version for name, version in versions.items()}
    lines = pyproject.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    table = ""
    in_dependency_list = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            table = stripped
            in_dependency_list = False

        should_rewrite = False
        if in_dependency_list:
            should_rewrite = True
            if _line_closes_list(line):
                in_dependency_list = False
        elif table == "[project]" and re.match(r"dependencies\s*=", stripped):
            should_rewrite = True
            in_dependency_list = "[" in line and not _line_closes_list(line)
        elif table == "[project.optional-dependencies]" and "=" in line:
            should_rewrite = True
            in_dependency_list = "[" in line and not _line_closes_list(line)

        output.append(_rewrite_dependency_line(line, normalized_versions) if should_rewrite else line)

    pyproject.write_text("\n".join(output) + "\n", encoding="utf-8")


def _line_closes_list(line: str) -> bool:
    quote = ""
    escaped = False
    for char in line:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "]":
            return True
    return False


def _rewrite_dependency_line(line: str, versions: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        quote = match.group("quote")
        spec = match.group("spec")
        rewritten = _rewrite_dependency_spec(spec, versions)
        return f"{quote}{rewritten}{quote}"

    return re.sub(r"(?P<quote>[\"'])(?P<spec>[^\"']+)(?P=quote)", replace, line)


def _rewrite_dependency_spec(spec: str, versions: dict[str, str]) -> str:
    marker = ""
    requirement = spec
    if ";" in spec:
        requirement, marker = spec.split(";", 1)
        marker = f";{marker}"

    match = re.match(r"\s*(?P<name>[A-Za-z0-9_.-]+)(?P<extras>\[[^\]]+\])?", requirement)
    if not match:
        return spec

    normalized = _normalize_package_name(match.group("name"))
    if normalized not in versions:
        return spec

    return f"{match.group('name')}{match.group('extras') or ''}=={versions[normalized]}{marker}"


def _module_init_file(project_root: Path, package_name: str) -> Path | None:
    module = _normalize_module_name(package_name)
    candidates = (
        project_root / "src" / module / "__init__.py",
        project_root / module / "__init__.py",
    )
    return next((path for path in candidates if path.exists()), None)


def _read_module_version(path: Path) -> str | None:
    match = re.search(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", path.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def _replace_module_version(path: Path, version: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"^__version__\s*=\s*[\"'][^\"']+[\"']",
        f'__version__ = "{version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count:
        path.write_text(updated, encoding="utf-8")


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalize_module_name(name: str) -> str:
    return re.sub(r"[-.]+", "_", name).lower()

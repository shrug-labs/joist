from __future__ import annotations

import shlex
from pathlib import Path

from .models import Project
from .workspace import WorkspaceError


def render_template(template: str, workspace_root: Path, project: Project, *, quoted: bool = False) -> str:
    values = {
        "project": project.name,
        "project_name": project.name,
        "package_name": project.package_name,
        "project_root": str(project.root),
        "workspace_root": str(workspace_root),
        "version": project.version,
    }
    if quoted:
        values = {key: shlex.quote(value) for key, value in values.items()}
    return template.format(**values)


def resolve_cwd(workspace_root: Path, project: Project, cwd: str) -> Path:
    rendered = render_template(cwd, workspace_root, project)
    path = Path(rendered)
    if not path.is_absolute():
        path = workspace_root / path
    resolved = path.resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise WorkspaceError(f"Target cwd must stay inside {workspace_root}: {rendered}") from exc
    return resolved


def resolve_target_path(workspace_root: Path, project: Project, cwd: Path, value: str) -> Path:
    rendered = render_template(value, workspace_root, project)
    path = Path(rendered)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()

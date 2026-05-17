from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .cache import TaskCache
from .models import Project, Target
from .render import render_template, resolve_cwd, resolve_target_path
from .workspace import Workspace, WorkspaceError


@dataclass(frozen=True)
class Task:
    project: Project
    target: Target
    skip_reason: str | None = None

    @property
    def label(self) -> str:
        return f"{self.project.name}:{self.target.name}"


@dataclass(frozen=True)
class RunOptions:
    target: str
    projects: tuple[str, ...] = ()
    project_filters: tuple[str, ...] = ()
    include_deps: bool = False
    affected: bool = False
    base: str | None = None
    head: str | None = None
    no_cache: bool = False
    dry_run: bool = False
    extra_args: tuple[str, ...] = ()


class Runner:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.cache = TaskCache(workspace.root, workspace.config.cache_dir)

    def run(self, options: RunOptions) -> int:
        tasks = self.plan(options)
        if not tasks:
            print("joist: no tasks selected")
            return 0

        for task in tasks:
            if task.skip_reason:
                print(f"joist: skipped {task.label} ({task.skip_reason})")
                continue
            execution = render_execution(self.workspace.root, task.project, task.target, options.extra_args)
            if options.dry_run:
                for command in execution.commands:
                    print(f"{task.label} -> {command}")
                continue

            code = self._run_task(task, execution, options.no_cache)
            if code != 0:
                return code
        return 0

    def plan(self, options: RunOptions) -> list[Task]:
        selected = self._select_projects(options)
        if options.include_deps:
            selected = self.workspace.with_dependencies(selected)
        selected_count = len(selected)

        planned: list[Task] = []
        seen: set[tuple[str, str]] = set()
        visiting: set[tuple[str, str]] = set()

        def add_task(project_name: str, target_name: str, strict: bool) -> None:
            key = (project_name, target_name)
            if key in seen:
                return
            if key in visiting:
                raise WorkspaceError(f"Task dependency cycle at {project_name}:{target_name}")
            project = self.workspace.project(project_name)
            target = project.target(target_name)
            if target is None:
                if strict:
                    raise WorkspaceError(f"Project '{project_name}' has no target '{target_name}'.")
                return
            skip_reason = skip_reason_for_target(self.workspace.root, project, target)
            if skip_reason:
                seen.add(key)
                planned.append(Task(project=project, target=target, skip_reason=skip_reason))
                return

            visiting.add(key)
            for dependency in target.depends_on:
                if dependency.startswith("^"):
                    dependency_target = dependency[1:]
                    for dep_name in self.workspace.sorted_projects(self.workspace.transitive_dependencies(project_name)):
                        add_task(dep_name.name, dependency_target, strict=False)
                else:
                    add_task(project_name, dependency, strict=True)
            visiting.remove(key)
            seen.add(key)
            planned.append(Task(project=project, target=target))

        strict_target = bool(options.projects or options.project_filters)
        for project in self.workspace.sorted_projects(selected):
            add_task(project.name, options.target, strict=strict_target)
        if selected_count and not planned:
            raise WorkspaceError(f"No selected projects define target '{options.target}'.")

        return planned

    def _select_projects(self, options: RunOptions) -> set[str]:
        requested = (*options.projects, *options.project_filters)
        if options.affected:
            affected = self.workspace.affected(options.base, options.head)
            if requested:
                requested_names = {self.workspace.project(name).name for name in requested}
                return affected.intersection(requested_names)
            return affected

        if not requested:
            return set(self.workspace.projects)

        return {self.workspace.project(name).name for name in requested}

    def _run_task(self, task: Task, execution: "RenderedExecution", no_cache: bool) -> int:
        use_cache = task.target.cache and not no_cache
        cache_key = (
            self.cache.key(task.project, task.target, execution.cwd, execution.env, execution.commands)
            if use_cache
            else None
        )
        if cache_key and self.cache.outputs_present(task.project, task.target, execution.cwd):
            cached = self.cache.read(cache_key)
            if cached and cached.get("returncode") == 0:
                print(f"joist: cache hit {task.label}")
                output = cached.get("output", "")
                if output:
                    print(output, end="" if output.endswith("\n") else "\n")
                return 0

        print(f"joist: running {task.label}")
        output = ""
        process_env = os.environ.copy()
        process_env.update(execution.env)
        for command in execution.commands:
            command_args = shlex.split(command)
            if not command_args:
                raise WorkspaceError(f"Target '{task.label}' rendered an empty command.")
            try:
                completed = subprocess.run(
                    command_args,
                    cwd=execution.cwd,
                    env=process_env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            except OSError as exc:
                raise WorkspaceError(f"Could not run {task.label}: {exc}") from exc
            if completed.stdout:
                output += completed.stdout
                print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
            if completed.returncode != 0:
                return completed.returncode
        if cache_key:
            self.cache.write(
                cache_key,
                {
                    "label": task.label,
                    "commands": list(execution.commands),
                    "cwd": str(execution.cwd),
                    "returncode": 0,
                    "output": output,
                },
            )
        return 0


@dataclass(frozen=True)
class RenderedExecution:
    cwd: Path
    env: dict[str, str]
    commands: tuple[str, ...]


def render_execution(
    workspace_root: Path,
    project: Project,
    target: Target,
    extra_args: tuple[str, ...] = (),
) -> RenderedExecution:
    cwd = resolve_cwd(workspace_root, project, target.cwd)
    env = {
        key: render_template(value, workspace_root, project)
        for key, value in target.env.items()
    }
    commands = tuple(render_template(command, workspace_root, project, quoted=True) for command in target.commands)
    if extra_args:
        if len(commands) != 1:
            raise WorkspaceError("Extra CLI args can only be used with single-command targets.")
        args = " ".join(shlex.quote(arg) for arg in extra_args)
        commands = (f"{commands[0]} {args}".strip(),)
    return RenderedExecution(cwd=cwd, env=env, commands=commands)


def skip_reason_for_target(workspace_root: Path, project: Project, target: Target) -> str | None:
    if not target.if_exists:
        return None
    cwd = resolve_cwd(workspace_root, project, target.cwd)
    missing = [
        render_template(path, workspace_root, project)
        for path in target.if_exists
        if not resolve_target_path(workspace_root, project, cwd, path).exists()
    ]
    if not missing:
        return None
    joined = ", ".join(missing)
    return f"missing {joined}"

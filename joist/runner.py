from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Iterable

from .cache import TaskCache
from .models import Project, Target
from .workspace import Workspace, WorkspaceError


@dataclass(frozen=True)
class Task:
    project: Project
    target: Target

    @property
    def label(self) -> str:
        return f"{self.project.name}:{self.target.name}"


@dataclass(frozen=True)
class RunOptions:
    target: str
    projects: tuple[str, ...] = ()
    all_projects: bool = False
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
            command = format_command(self.workspace.root, task.project, task.target.command, options.extra_args)
            if options.dry_run:
                print(f"{task.label} -> {command}")
                continue

            code = self._run_task(task, command, list(options.extra_args), options.no_cache)
            if code != 0:
                return code
        return 0

    def plan(self, options: RunOptions) -> list[Task]:
        selected = self._select_projects(options)
        if options.include_deps:
            selected = self.workspace.with_dependencies(selected)

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

        for project in self.workspace.sorted_projects(selected):
            add_task(project.name, options.target, strict=not options.all_projects)

        return planned

    def _select_projects(self, options: RunOptions) -> set[str]:
        if options.affected:
            affected = self.workspace.affected(options.base, options.head)
            if options.projects:
                requested = {self.workspace.project(name).name for name in options.projects}
                return affected.intersection(requested)
            return affected

        if options.all_projects or not options.projects:
            return set(self.workspace.projects)

        return {self.workspace.project(name).name for name in options.projects}

    def _run_task(self, task: Task, command: str, extra_args: list[str], no_cache: bool) -> int:
        use_cache = task.target.cache and not no_cache
        cache_key = self.cache.key(task.project, task.target, command, extra_args) if use_cache else None
        if cache_key and self.cache.outputs_present(task.project, task.target):
            cached = self.cache.read(cache_key)
            if cached and cached.get("returncode") == 0:
                print(f"joist: cache hit {task.label}")
                output = cached.get("output", "")
                if output:
                    print(output, end="" if output.endswith("\n") else "\n")
                return 0

        print(f"joist: running {task.label}")
        command_args = shlex.split(command)
        if not command_args:
            raise WorkspaceError(f"Target '{task.label}' rendered an empty command.")
        try:
            completed = subprocess.run(
                command_args,
                cwd=self.workspace.root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise WorkspaceError(f"Could not run {task.label}: {exc}") from exc
        if completed.stdout:
            print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
        if cache_key and completed.returncode == 0:
            self.cache.write(
                cache_key,
                {
                    "label": task.label,
                    "command": command,
                    "returncode": completed.returncode,
                    "output": completed.stdout,
                },
            )
        return completed.returncode


def format_command(workspace_root, project: Project, template: str, extra_args: Iterable[str]) -> str:
    values = {
        "project": shlex.quote(project.name),
        "project_name": shlex.quote(project.name),
        "package_name": shlex.quote(project.package_name),
        "project_root": shlex.quote(str(project.root)),
        "workspace_root": shlex.quote(str(workspace_root)),
    }
    command = template.format(**values)
    args = " ".join(shlex.quote(arg) for arg in extra_args)
    return f"{command} {args}".strip()

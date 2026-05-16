from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .release import bump_workspace_version
from .runner import RunOptions, Runner
from .scaffold import init_workspace, new_project
from .workspace import Workspace, WorkspaceError


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra_args: list[str] = []
    if "--" in argv:
        index = argv.index("--")
        extra_args = argv[index + 1 :]
        argv = argv[:index]

    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    try:
        return dispatch(args, extra_args)
    except WorkspaceError as exc:
        print(f"joist: {exc}", file=sys.stderr)
        return 2
    except (FileExistsError, ValueError) as exc:
        print(f"joist: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="joist", description="A tiny uv-native Python monorepo framework.")
    parser.add_argument("--version", action="version", version=f"joist {__version__}")
    subcommands = parser.add_subparsers(dest="command")

    init = subcommands.add_parser("init", help="Create an opinionated uv monorepo skeleton.")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--force", action="store_true")

    new = subcommands.add_parser("new", help="Create an app or lib project.")
    new.add_argument("kind", choices=("app", "lib"))
    new.add_argument("name")
    new.add_argument("--depends-on", action="append", default=[])
    new.add_argument("--force", action="store_true")

    subcommands.add_parser("list", help="List discovered projects.").add_argument("--json", action="store_true")

    graph = subcommands.add_parser("graph", help="Print the project dependency graph.")
    graph.add_argument("--format", choices=("text", "json", "dot"), default="text")

    run = subcommands.add_parser("run", help="Run a target across projects.")
    _add_run_arguments(run)

    affected = subcommands.add_parser(
        "affected",
        help="Run a target only for changed projects and their dependents, or list affected projects.",
    )
    _add_run_arguments(affected, target_required=False)
    affected.add_argument("--list", action="store_true", dest="list_projects", help="List affected projects.")
    affected.add_argument("--json", action="store_true", help="Use JSON output with --list.")
    affected.set_defaults(affected=True)

    cache = subcommands.add_parser("cache", help="Manage the local task cache.")
    cache_subcommands = cache.add_subparsers(dest="cache_command")
    cache_subcommands.add_parser("clear", help="Delete .joist/cache.")

    version = subcommands.add_parser("version", help="Bump all public project versions together.")
    version.add_argument("bump", help="major, minor, patch, or an explicit MAJOR.MINOR.PATCH")
    version.add_argument("--dry-run", action="store_true")

    return parser


def _add_run_arguments(parser: argparse.ArgumentParser, *, target_required: bool = True) -> None:
    if target_required:
        parser.add_argument("target", help="Target to run.")
    else:
        parser.add_argument("target", nargs="?", help="Target to run. Omit when using --list.")
    parser.add_argument("projects", nargs="*", help="Project names to filter.")
    parser.add_argument("--all", action="store_true", dest="all_projects")
    parser.add_argument("--include-deps", action="store_true")
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--dry-run", action="store_true")


def dispatch(args: argparse.Namespace, extra_args: list[str]) -> int:
    if args.command == "init":
        written = init_workspace(Path(args.path).resolve(), force=args.force)
        for path in written:
            print(f"created {path}")
        if not written:
            print("joist: workspace files already exist")
        return 0

    if args.command == "new":
        workspace = Workspace.load()
        written = new_project(
            workspace.root,
            args.kind,
            args.name,
            depends_on=args.depends_on,
            force=args.force,
        )
        for path in written:
            print(f"created {path.relative_to(workspace.root)}")
        return 0

    workspace = Workspace.load()

    if args.command == "list":
        if args.json:
            print(workspace.as_json())
        else:
            for project in workspace.sorted_projects():
                deps = f" -> {', '.join(project.depends_on)}" if project.depends_on else ""
                print(f"{project.name} ({project.type}) {project.root.relative_to(workspace.root)}{deps}")
        return 0

    if args.command == "graph":
        print_graph(workspace, args.format)
        return 0

    if args.command in {"run", "affected"}:
        if args.command == "affected" and args.list_projects:
            names = select_affected_projects(workspace, args)
            if args.json:
                print(json.dumps(workspace.as_dict(names), indent=2, sort_keys=True))
            else:
                for project in workspace.sorted_projects(names):
                    print(project.name)
            return 0
        if args.command == "affected" and args.json:
            raise WorkspaceError("--json can only be used with `joist affected --list`.")
        if args.command == "affected" and not args.target:
            raise WorkspaceError("`joist affected` requires a target, or use `joist affected --list`.")

        options = RunOptions(
            target=args.target,
            projects=tuple(args.projects),
            all_projects=args.all_projects,
            include_deps=args.include_deps,
            affected=bool(getattr(args, "affected", False)),
            base=args.base,
            head=args.head,
            no_cache=args.no_cache,
            dry_run=args.dry_run,
            extra_args=tuple(extra_args),
        )
        return Runner(workspace).run(options)

    if args.command == "cache":
        if args.cache_command == "clear":
            shutil.rmtree(workspace.config.cache_dir, ignore_errors=True)
            print(f"cleared {workspace.config.cache_dir}")
            return 0
        print("joist: choose a cache command", file=sys.stderr)
        return 2

    if args.command == "version":
        changes = bump_workspace_version(workspace, args.bump, dry_run=args.dry_run)
        for change in changes:
            action = "would update" if args.dry_run else "updated"
            print(f"{action} {change.path.relative_to(workspace.root)}: {change.old} -> {change.new}")
        return 0

    return 0


def select_affected_projects(workspace: Workspace, args: argparse.Namespace) -> set[str]:
    if args.list_projects:
        requested_names = tuple(name for name in (args.target, *args.projects) if name)
    else:
        requested_names = tuple(args.projects)

    if args.all_projects:
        selected = set(workspace.projects)
    else:
        selected = workspace.affected(args.base, args.head)
        if requested_names:
            requested = {workspace.project(name).name for name in requested_names}
            selected = selected.intersection(requested)
    if args.include_deps:
        selected = workspace.with_dependencies(selected)
    return selected


def print_graph(workspace: Workspace, format_name: str) -> None:
    if format_name == "json":
        print(workspace.as_json())
        return

    if format_name == "dot":
        print("digraph joist {")
        for project in workspace.sorted_projects():
            if not project.depends_on:
                print(f'  "{project.name}";')
            for dep in project.depends_on:
                print(f'  "{dep}" -> "{project.name}";')
        print("}")
        return

    for project in workspace.sorted_projects():
        if project.depends_on:
            print(f"{project.name}: {', '.join(project.depends_on)}")
        else:
            print(f"{project.name}:")


if __name__ == "__main__":
    raise SystemExit(main())

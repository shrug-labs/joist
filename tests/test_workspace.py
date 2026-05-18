from __future__ import annotations

import json
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from joist.cache import TaskCache
from joist.cli import main
from joist.release import bump_version, bump_workspace_version
from joist.render import render_template, resolve_cwd
from joist.runner import RunOptions, Runner, render_execution
from joist.scaffold import init_workspace, new_project
from joist.workspace import Workspace, WorkspaceError, load_config, discover_projects


class WorkspaceTests(unittest.TestCase):
    def test_discovers_projects_and_internal_dependencies(self) -> None:
        root = self.make_workspace()
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        self.assertEqual(sorted(workspace.projects), ["api", "core"])
        self.assertEqual(workspace.projects["api"].depends_on, ("core",))

    def test_affected_includes_reverse_dependencies(self) -> None:
        root = self.make_workspace()
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        affected = workspace.affected_by_files(["packages/core/src/core/__init__.py"])

        self.assertEqual(affected, {"core", "api"})

    def test_configured_affects_all_patterns_select_every_project(self) -> None:
        root = self.make_workspace()
        write(
            root / "joist.toml",
            """
[workspace]
projects = ["packages/*", "apps/*"]
affects_all = ["requirements*.txt", ".github/workflows/**"]

[target_defaults.build]
command = "python -m build {project_root}"
depends_on = ["^build"]
cache = false
""",
        )
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        self.assertEqual(workspace.affected_by_files(["requirements-dev.txt"]), {"core", "api"})
        self.assertEqual(workspace.affected_by_files([".github/workflows/ci.yml"]), {"core", "api"})
        self.assertEqual(workspace.affected_by_files(["joist.toml"]), {"core", "api"})

    def test_affected_list_flag_json_outputs_selected_projects(self) -> None:
        root = self.make_workspace()
        self.init_git_repo(root)
        write(root / "packages/core/src/core/__init__.py", '__version__ = "0.1.1"\n')

        output = StringIO()
        with chdir(root), redirect_stdout(output):
            code = main(["affected", "--list", "--json", "--base", "HEAD"])

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(sorted(payload["projects"]), ["api", "core"])

    def test_list_since_json_outputs_selected_projects(self) -> None:
        root = self.make_workspace()
        self.init_git_repo(root)
        write(root / "packages/core/src/core/__init__.py", '__version__ = "0.1.1"\n')

        output = StringIO()
        with chdir(root), redirect_stdout(output):
            code = main(["list", "--since", "HEAD", "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(sorted(payload["projects"]), ["api", "core"])

    def test_project_filter_intersects_since_selection(self) -> None:
        root = self.make_workspace()
        self.init_git_repo(root)
        write(root / "packages/core/src/core/__init__.py", '__version__ = "0.1.1"\n')

        output = StringIO()
        with chdir(root), redirect_stdout(output):
            code = main(["list", "--since", "HEAD", "--project", "api", "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(sorted(payload["projects"]), ["api"])

    def test_run_since_selects_affected_projects(self) -> None:
        root = self.make_workspace()
        self.init_git_repo(root)
        write(root / "packages/core/src/core/__init__.py", '__version__ = "0.1.1"\n')

        output = StringIO()
        with chdir(root), redirect_stdout(output):
            code = main(["run", "build", "--since", "HEAD", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertIn("core:build ->", output.getvalue())
        self.assertIn("api:build ->", output.getvalue())

    def test_run_project_filter_selects_exact_project(self) -> None:
        root = self.make_workspace()

        output = StringIO()
        with chdir(root), redirect_stdout(output):
            code = main(["run", "no_shell", "--project", "core", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertIn("core:no_shell ->", output.getvalue())

    def test_default_run_skips_projects_missing_target(self) -> None:
        root = self.make_workspace()

        output = StringIO()
        with chdir(root), redirect_stdout(output):
            code = main(["run", "no_shell", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertIn("core:no_shell ->", output.getvalue())

    def test_unknown_target_errors_when_no_selected_projects_define_it(self) -> None:
        root = self.make_workspace()

        error = StringIO()
        with chdir(root), redirect_stderr(error):
            code = main(["run", "not_a_target", "--dry-run"])

        self.assertEqual(code, 2)
        self.assertIn("No selected projects define target 'not_a_target'", error.getvalue())

    def test_explicit_project_errors_when_target_is_missing(self) -> None:
        root = self.make_workspace()

        error = StringIO()
        with chdir(root), redirect_stderr(error):
            code = main(["run", "no_shell", "--project", "api", "--dry-run"])

        self.assertEqual(code, 2)
        self.assertIn("Project 'api' has no target 'no_shell'", error.getvalue())

    def test_since_and_base_are_mutually_exclusive(self) -> None:
        root = self.make_workspace()

        error = StringIO()
        with chdir(root), redirect_stderr(error):
            code = main(["run", "build", "--since", "HEAD", "--base", "main"])

        self.assertEqual(code, 2)
        self.assertIn("either --since or --base", error.getvalue())

    def test_all_flag_is_not_supported(self) -> None:
        root = self.make_workspace()

        error = StringIO()
        with chdir(root), redirect_stderr(error):
            with self.assertRaises(SystemExit) as raised:
                main(["run", "build", "--all"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments: --all", error.getvalue())

    def test_affected_does_not_accept_since(self) -> None:
        root = self.make_workspace()

        error = StringIO()
        with chdir(root), redirect_stderr(error):
            with self.assertRaises(SystemExit) as raised:
                main(["affected", "test", "--since", "HEAD"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments: --since", error.getvalue())

    def test_affected_list_target_is_not_shadowed(self) -> None:
        root = self.make_workspace()
        write(
            root / "joist.toml",
            f"""
[workspace]
projects = ["packages/*", "apps/*"]

[target_defaults.list]
command = "{shlex.quote(sys.executable)} -c \\"print('ran-list-target')\\""
cache = false
""",
        )
        self.init_git_repo(root)
        write(root / "packages/core/src/core/__init__.py", '__version__ = "0.1.1"\n')

        output = StringIO()
        with chdir(root), redirect_stdout(output):
            code = main(["affected", "list", "--base", "HEAD", "--no-cache"])

        self.assertEqual(code, 0)
        self.assertIn("joist: running core:list", output.getvalue())
        self.assertIn("joist: running api:list", output.getvalue())
        self.assertIn("ran-list-target", output.getvalue())

    def test_affected_json_requires_list_flag(self) -> None:
        root = self.make_workspace()

        error = StringIO()
        with chdir(root), redirect_stderr(error):
            code = main(["affected", "test", "--json"])

        self.assertEqual(code, 2)
        self.assertIn("--json can only be used", error.getvalue())

    def test_affects_all_must_be_a_list(self) -> None:
        root = self.make_workspace()
        write(
            root / "joist.toml",
            """
[workspace]
projects = ["packages/*", "apps/*"]
affects_all = "requirements*.txt"
""",
        )

        with self.assertRaisesRegex(WorkspaceError, "workspace.affects_all"):
            load_config(root)

    def test_workspace_project_globs_must_be_a_list(self) -> None:
        root = self.make_workspace()
        write(
            root / "joist.toml",
            """
[workspace]
projects = "packages/*"
""",
        )

        with self.assertRaisesRegex(WorkspaceError, "workspace.projects"):
            load_config(root)

    def test_project_private_must_be_boolean(self) -> None:
        root = self.make_workspace()
        write(
            root / "apps/api/pyproject.toml",
            """
[project]
name = "api"
version = "0.1.0"
dependencies = []

[tool.joist]
private = "false"
""",
        )

        with self.assertRaisesRegex(WorkspaceError, "tool.joist.private"):
            discover_projects(load_config(root))

    def test_target_cache_must_be_boolean(self) -> None:
        root = self.make_workspace()
        write(
            root / "joist.toml",
            """
[workspace]
projects = ["packages/*", "apps/*"]

[target_defaults.bad]
command = "echo bad"
cache = "false"
""",
        )

        with self.assertRaisesRegex(WorkspaceError, "Target 'bad' cache"):
            load_config(root)

    def test_build_plan_honors_dependency_targets(self) -> None:
        root = self.make_workspace()
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))
        plan = Runner(workspace).plan(RunOptions(target="build", projects=("api",)))

        self.assertEqual([task.label for task in plan], ["core:build", "api:build"])

    def test_runner_does_not_execute_targets_through_shell(self) -> None:
        root = self.make_workspace()
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        with redirect_stdout(StringIO()):
            code = Runner(workspace).run(RunOptions(target="no_shell", projects=("core",), no_cache=True))

        self.assertEqual(code, 0)

    def test_target_cwd_env_version_and_commands_are_applied(self) -> None:
        root = self.make_workspace()
        write(
            root / "joist.toml",
            f"""
[workspace]
projects = ["packages/*", "apps/*"]

[target_defaults.describe]
cwd = "{{project_root}}"
env = {{ JOIST_PROJECT = "{{project_name}}", JOIST_VERSION = "{{version}}" }}
commands = [
  "{shlex.quote(sys.executable)} -c \\"import os, pathlib; print(pathlib.Path.cwd().name); print(os.environ['JOIST_PROJECT']); print(os.environ['JOIST_VERSION'])\\"",
  "{shlex.quote(sys.executable)} -c \\"print('second-step-{{version}}')\\"",
]
cache = false
""",
        )
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        output = StringIO()
        with redirect_stdout(output):
            code = Runner(workspace).run(RunOptions(target="describe", projects=("core",), no_cache=True))

        self.assertEqual(code, 0)
        self.assertIn("joist: running core:describe", output.getvalue())
        self.assertIn("core\ncore\n0.1.0", output.getvalue())
        self.assertIn("second-step-0.1.0", output.getvalue())

    def test_if_exists_skips_projects_missing_required_files(self) -> None:
        root = self.make_workspace()
        write(root / "packages/core/Containerfile", "FROM scratch")
        write(
            root / "joist.toml",
            f"""
[workspace]
projects = ["packages/*", "apps/*"]

[target_defaults.containerize]
cwd = "{{project_root}}"
if_exists = "Containerfile"
commands = ["{shlex.quote(sys.executable)} -c \\"print('container:{{project_name}}')\\""]
cache = false
""",
        )
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        output = StringIO()
        with redirect_stdout(output):
            code = Runner(workspace).run(RunOptions(target="containerize", dry_run=True))

        self.assertEqual(code, 0)
        self.assertIn("core:containerize ->", output.getvalue())
        self.assertIn("container:core", output.getvalue())
        self.assertIn("joist: skipped api:containerize (missing Containerfile)", output.getvalue())

    def test_command_and_commands_are_mutually_exclusive(self) -> None:
        root = self.make_workspace()
        write(
            root / "joist.toml",
            """
[workspace]
projects = ["packages/*", "apps/*"]

[target_defaults.bad]
command = "echo one"
commands = ["echo two"]
""",
        )

        with self.assertRaisesRegex(WorkspaceError, "either command or commands"):
            load_config(root)

    def test_extra_args_are_rejected_for_multi_command_targets(self) -> None:
        root = self.make_workspace()
        write(
            root / "joist.toml",
            """
[workspace]
projects = ["packages/*", "apps/*"]

[target_defaults.multi]
commands = ["echo one", "echo two"]
cache = false
""",
        )
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        with self.assertRaisesRegex(WorkspaceError, "Extra CLI args"):
            Runner(workspace).run(RunOptions(target="multi", projects=("core",), extra_args=("--verbose",)))

    def test_cache_key_includes_execution_context(self) -> None:
        root = self.make_workspace()
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))
        project = workspace.project("core")
        target = project.target("no_shell")
        self.assertIsNotNone(target)
        assert target is not None
        cache = TaskCache(root, root / ".joist/cache")

        cwd = root / "packages/core"
        key = cache.key(project, target, cwd, {"A": "one"}, ("echo one",))

        self.assertNotEqual(key, cache.key(project, target, root, {"A": "one"}, ("echo one",)))
        self.assertNotEqual(key, cache.key(project, target, cwd, {"A": "two"}, ("echo one",)))
        self.assertNotEqual(key, cache.key(project, target, cwd, {"A": "one"}, ("echo two",)))

    def test_cached_results_do_not_store_configured_env_values(self) -> None:
        root = self.make_workspace()
        write(
            root / "joist.toml",
            f"""
[workspace]
projects = ["packages/*", "apps/*"]

[target_defaults.secret]
env = {{ TOKEN = "s3cr3t" }}
commands = ["{shlex.quote(sys.executable)} -c \\"print('ok')\\""]
cache = true
""",
        )
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        with redirect_stdout(StringIO()):
            code = Runner(workspace).run(RunOptions(target="secret", projects=("core",)))

        self.assertEqual(code, 0)
        cache_text = "\n".join(path.read_text(encoding="utf-8") for path in (root / ".joist/cache").glob("*.json"))
        self.assertNotIn("s3cr3t", cache_text)

    def test_cache_ignores_non_object_records(self) -> None:
        root = self.make_workspace()
        write(root / ".joist/cache/bad.json", "[]")

        self.assertIsNone(TaskCache(root, root / ".joist/cache").read("bad"))

    def test_inputs_resolve_relative_to_target_cwd(self) -> None:
        root = self.make_workspace()
        write(root / "packages/core/data.txt", "one")
        write(
            root / "joist.toml",
            """
[workspace]
projects = ["packages/*", "apps/*"]

[target_defaults.ctx]
cwd = "{project_root}"
commands = ["echo ctx"]
inputs = ["data.txt"]
""",
        )
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))
        project = workspace.project("core")
        target = project.target("ctx")
        self.assertIsNotNone(target)
        assert target is not None
        cache = TaskCache(root, root / ".joist/cache")
        execution = render_execution(root, project, target)

        first = cache.key(project, target, execution.cwd, execution.env, execution.commands)
        write(root / "packages/core/data.txt", "two")
        second = cache.key(project, target, execution.cwd, execution.env, execution.commands)

        self.assertNotEqual(first, second)

    def test_bump_version(self) -> None:
        self.assertEqual(bump_version("1.2.3", "patch"), "1.2.4")
        self.assertEqual(bump_version("1.2.3", "minor"), "1.3.0")
        self.assertEqual(bump_version("1.2.3", "major"), "2.0.0")
        self.assertEqual(bump_version("1.2.3", "2.4.6"), "2.4.6")

    def test_scaffold_uses_uv_workspace_sources_for_internal_deps(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)

        init_workspace(root)
        new_project(root, "lib", "core")
        new_project(root, "app", "api", depends_on=["core"])

        pyproject = (root / "apps/api/pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('[tool.uv.sources]', pyproject)
        self.assertIn('core = { workspace = true }', pyproject)
        self.assertIn('dependencies = ["core==0.1.0"]', pyproject)
        self.assertIn('build-backend = "uv_build"', pyproject)

    def test_scaffold_does_not_cache_builds_without_artifact_restore(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)

        init_workspace(root)

        joist_toml = (root / "joist.toml").read_text(encoding="utf-8")
        self.assertIn("[target_defaults.build]", joist_toml)
        self.assertIn("cache = false", joist_toml)
        self.assertNotIn('outputs = ["dist"]', joist_toml)

    def test_release_updates_versions_and_internal_dependency_pins(self) -> None:
        root = self.make_workspace()
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        changes = bump_workspace_version(workspace, "patch")

        self.assertIn(root / "packages/core/pyproject.toml", {change.path for change in changes})
        self.assertIn('version = "0.1.1"', (root / "packages/core/pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn('version = "0.1.1"', (root / "apps/api/pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn('"core==0.1.1"', (root / "apps/api/pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn('__version__ = "0.1.1"', (root / "packages/core/src/core/__init__.py").read_text(encoding="utf-8"))

    def test_release_only_rewrites_the_project_version_key(self) -> None:
        root = self.make_workspace()
        pyproject = root / "packages/core/pyproject.toml"
        pyproject.write_text(
            pyproject.read_text(encoding="utf-8").replace('version = "0.1.0"', 'versioning = "keep"\nversion = "0.1.0"', 1),
            encoding="utf-8",
        )
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        bump_workspace_version(workspace, "patch")

        self.assertIn('versioning = "keep"', pyproject.read_text(encoding="utf-8"))
        self.assertIn('version = "0.1.1"', pyproject.read_text(encoding="utf-8"))

    def test_release_updates_private_project_internal_dependency_pins(self) -> None:
        root = self.make_workspace(api_private=True)
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        bump_workspace_version(workspace, "patch")

        api_pyproject = (root / "apps/api/pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = "0.1.0"', api_pyproject)
        self.assertIn('"core==0.1.1"', api_pyproject)

    def test_release_handles_extras_in_multiline_dependency_lists(self) -> None:
        root = self.make_workspace(api_private=True)
        write(
            root / "apps/api/pyproject.toml",
            """
[project]
name = "api"
version = "0.1.0"
dependencies = [
  "core[cli]>=0.1.0",
  "core>=0.1.0",
]

[tool.uv.sources]
core = { workspace = true }

[tool.joist]
name = "api"
type = "app"
private = true
""",
        )
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        bump_workspace_version(workspace, "patch")

        api_pyproject = (root / "apps/api/pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"core[cli]==0.1.1"', api_pyproject)
        self.assertIn('"core==0.1.1"', api_pyproject)

    def test_release_rejects_mixed_versions_for_relative_bump(self) -> None:
        root = self.make_workspace(api_version="0.2.0")
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        with self.assertRaisesRegex(WorkspaceError, "one current version"):
            bump_workspace_version(workspace, "patch")

    def test_duplicate_project_names_are_rejected(self) -> None:
        root = self.make_workspace()
        write(
            root / "packages/other/pyproject.toml",
            """
[project]
name = "other"
version = "0.1.0"
dependencies = []

[tool.joist]
name = "core"
""",
        )

        with self.assertRaisesRegex(WorkspaceError, "Duplicate project name"):
            discover_projects(load_config(root))

    def test_cache_dir_must_stay_inside_workspace(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        write(
            root / "joist.toml",
            """
[workspace]
projects = ["."]
cache_dir = "../outside"
""",
        )

        with self.assertRaisesRegex(WorkspaceError, "inside"):
            load_config(root)

    def test_target_cwd_must_stay_inside_workspace(self) -> None:
        root = self.make_workspace()
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        with self.assertRaisesRegex(WorkspaceError, "Target cwd must stay inside"):
            resolve_cwd(root, workspace.project("core"), "..")

    def test_unknown_template_field_is_a_workspace_error(self) -> None:
        root = self.make_workspace()
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        with self.assertRaisesRegex(WorkspaceError, "Unknown target template field 'missing'"):
            render_template("{missing}", root, workspace.project("core"))
        with self.assertRaisesRegex(WorkspaceError, "Invalid target template"):
            render_template("{project_name.missing}", root, workspace.project("core"))

    def make_workspace(self, api_version: str = "0.1.0", api_private: bool = False) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return make_workspace(Path(temp.name).resolve(), api_version=api_version, api_private=api_private)

    def init_git_repo(self, root: Path) -> None:
        run(["git", "init"], root)
        run(["git", "config", "user.email", "joist@example.com"], root)
        run(["git", "config", "user.name", "Joist Tests"], root)
        run(["git", "add", "."], root)
        run(["git", "commit", "-m", "initial"], root)


def make_workspace(root: Path, api_version: str = "0.1.0", api_private: bool = False) -> Path:
    write(
        root / "joist.toml",
        """
[workspace]
projects = ["packages/*", "apps/*"]

[target_defaults.build]
command = "python -m build {project_root}"
depends_on = ["^build"]
cache = false
""",
    )
    write(
        root / "packages/core/pyproject.toml",
        f"""
[project]
name = "core"
version = "0.1.0"
dependencies = []

[tool.joist]
name = "core"
type = "lib"

[tool.joist.targets.no_shell]
command = "{shlex.quote(sys.executable)} -c \\"import sys; assert sys.argv[1] == '&&'\\" && false"
cache = false
""",
    )
    write(root / "packages/core/src/core/__init__.py", '__version__ = "0.1.0"\n')
    write(
        root / "apps/api/pyproject.toml",
        f"""
[project]
name = "api"
version = "{api_version}"
dependencies = ["core>=0.1.0"]

[tool.uv.sources]
core = {{ workspace = true }}

[tool.joist]
name = "api"
type = "app"
{"private = true" if api_private else ""}
""",
    )
    write(root / "apps/api/src/api/__init__.py", "")
    return root


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


class chdir:
    def __init__(self, path: Path):
        self.path = path
        self.previous = Path.cwd()

    def __enter__(self) -> None:
        import os

        os.chdir(self.path)

    def __exit__(self, *args) -> None:
        import os

        os.chdir(self.previous)


def run(args: list[str], cwd: Path) -> None:
    import subprocess

    subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


if __name__ == "__main__":
    unittest.main()

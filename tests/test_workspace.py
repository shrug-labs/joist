from __future__ import annotations

import json
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from joist.cli import main
from joist.release import bump_version, bump_workspace_version
from joist.runner import RunOptions, Runner
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

    def test_build_plan_honors_dependency_targets(self) -> None:
        root = self.make_workspace()
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))
        plan = Runner(workspace).plan(RunOptions(target="build", projects=("api",)))

        self.assertEqual([task.label for task in plan], ["core:build", "api:build"])

    def test_runner_does_not_execute_targets_through_shell(self) -> None:
        root = self.make_workspace()
        workspace = Workspace(load_config(root), discover_projects(load_config(root)))

        code = Runner(workspace).run(RunOptions(target="no_shell", projects=("core",), no_cache=True))

        self.assertEqual(code, 0)

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

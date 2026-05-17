from __future__ import annotations

import re
from pathlib import Path


ROOT_PYPROJECT = """[project]
name = "workspace"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[tool.uv]
package = false

[tool.uv.workspace]
members = ["packages/*", "apps/*"]

[dependency-groups]
dev = [
  "pytest>=8",
  "ruff>=0.6",
]
"""

ROOT_JOIST = """[workspace]
projects = ["packages/*", "apps/*"]
default_base = "main"
cache_dir = ".joist/cache"

[target_defaults.test]
cwd = "{project_root}"
commands = ["uv run pytest tests"]
cache = true
inputs = ["src/**/*.py", "tests/**/*.py", "pyproject.toml", "{workspace_root}/pyproject.toml", "{workspace_root}/uv.lock"]

[target_defaults.lint]
cwd = "{project_root}"
commands = ["uv run ruff check src tests"]
cache = true
inputs = ["src/**/*.py", "tests/**/*.py", "pyproject.toml", "{workspace_root}/pyproject.toml", "{workspace_root}/uv.lock"]

[target_defaults.build]
cwd = "{project_root}"
commands = ["uv build"]
cache = false
inputs = ["src/**/*.py", "pyproject.toml", "{workspace_root}/pyproject.toml", "{workspace_root}/uv.lock"]
depends_on = ["^build"]
"""


def init_workspace(root: Path, force: bool = False) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    written = [
        _write_if_missing(root / "pyproject.toml", ROOT_PYPROJECT, force),
        _write_if_missing(root / "joist.toml", ROOT_JOIST, force),
    ]
    (root / "packages").mkdir(exist_ok=True)
    (root / "apps").mkdir(exist_ok=True)
    return [path for path in written if path is not None]


def new_project(root: Path, kind: str, name: str, depends_on: list[str] | None = None, force: bool = False) -> list[Path]:
    if kind not in {"app", "lib"}:
        raise ValueError("kind must be 'app' or 'lib'")

    package_name = _package_name(name)
    module_name = _module_name(name)
    base = root / ("apps" if kind == "app" else "packages") / package_name
    if base.exists() and not force:
        raise FileExistsError(f"{base} already exists")

    depends_on = depends_on or []
    src = base / "src" / module_name
    tests = base / "tests"
    src.mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)

    dependency_specs = ", ".join(f'"{_package_name(dep)}==0.1.0"' for dep in depends_on)
    internal_names = ", ".join(f'"{_package_name(dep)}"' for dep in depends_on)
    uv_sources = _uv_sources(depends_on)
    pyproject = f"""[project]
name = "{package_name}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [{dependency_specs}]

[build-system]
requires = ["uv_build>=0.11.8,<0.12"]
build-backend = "uv_build"

{uv_sources}[tool.joist]
name = "{package_name}"
type = "{kind}"
depends_on = [{internal_names}]
"""
    init_body = f'"""Generated {kind} package for {package_name}."""\n\n__version__ = "0.1.0"\n'
    test_body = f"from {module_name} import __version__\n\n\ndef test_version():\n    assert __version__ == \"0.1.0\"\n"

    written = [
        _write_if_missing(base / "pyproject.toml", pyproject, force),
        _write_if_missing(src / "__init__.py", init_body, force),
        _write_if_missing(tests / f"test_{module_name}.py", test_body, force),
    ]
    if kind == "app":
        main_body = (
            "def main() -> None:\n"
            f"    print(\"hello from {package_name}\")\n\n\n"
            "if __name__ == \"__main__\":\n"
            "    main()\n"
        )
        written.append(_write_if_missing(src / "__main__.py", main_body, force))

    return [path for path in written if path is not None]


def _write_if_missing(path: Path, content: str, force: bool) -> Path | None:
    if path.exists() and not force:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _package_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-_.").lower()
    if not value:
        raise ValueError("project name cannot be empty")
    return value


def _module_name(name: str) -> str:
    package = _package_name(name)
    module = re.sub(r"[-.]+", "_", package)
    if module[0].isdigit():
        module = f"_{module}"
    return module


def _uv_sources(depends_on: list[str]) -> str:
    if not depends_on:
        return ""
    lines = ["[tool.uv.sources]"]
    for dep in depends_on:
        package = _package_name(dep)
        key = package if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", package) else f'"{package}"'
        lines.append(f"{key} = {{ workspace = true }}")
    return "\n".join(lines) + "\n\n"

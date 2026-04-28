# Joist

A tiny, opinionated Python monorepo framework built around `uv`.

Joist borrows a few proven ideas from Nx and Lerna without trying to become
their Python clone:

- a project graph built from workspace packages
- named targets such as `test`, `lint`, and `build`
- dependency-aware task ordering
- affected-project runs based on `git diff`
- a small local task cache
- fixed-version releases for public packages

## Quickstart

```sh
uvx joist init demo
cd demo
uvx joist new lib core
uvx joist new app api --depends-on core
uvx joist graph
uvx joist run test --all
uvx joist affected test --base main
uvx joist version patch --dry-run
```

This creates a uv workspace with one library, one app, an editable internal
workspace dependency from `api` to `core`, and graph-aware Joist targets.

If Joist is installed in the current environment, the console command is
available directly:

```sh
joist list
joist affected test --base origin/main --head HEAD
```

## Python support

Joist supports Python 3.11 through 3.14. The minimum is Python 3.11 because
Joist uses the standard-library `tomllib` module for TOML parsing.

## Opinionated layout

`joist init` creates this shape:

```text
.
|-- apps/
|-- packages/
|-- joist.toml
`-- pyproject.toml
```

Projects live in `apps/*` or `packages/*`, each with its own `pyproject.toml`.
The root `pyproject.toml` owns the uv workspace; Joist treats that as the
package source of truth whenever possible. Joist uses uv workspace membership to
discover packages, uv workspace sources to keep internal dependencies editable,
and uv commands to sync, test, build, and publish. Joist only adds graph-aware
task orchestration on top:

```toml
[tool.uv]
package = false

[tool.uv.workspace]
members = ["packages/*", "apps/*"]
```

## Configuration

`joist.toml` defines project globs and target defaults:

```toml
[workspace]
projects = ["packages/*", "apps/*"]
default_base = "main"
cache_dir = ".joist/cache"

[target_defaults.test]
command = "uv run --package {package_name} pytest {project_root}/tests"
cache = true
inputs = ["{project_root}/src/**/*.py", "{project_root}/tests/**/*.py", "{project_root}/pyproject.toml", "pyproject.toml", "uv.lock"]

[target_defaults.build]
command = "uv build --package {package_name}"
cache = false
depends_on = ["^build"]
```

Target command templates support:

- `{workspace_root}`
- `{project_root}`
- `{project_name}`
- `{package_name}`

Commands are split with Python's `shlex` and executed without a shell. Use an
explicit shell command such as `sh -c "..."` only when shell syntax is required.

Each project can override or add targets in its own `pyproject.toml`:

```toml
[tool.joist]
name = "api"
type = "app"
depends_on = ["core"]

[tool.joist.targets.serve]
command = "uv run --package api python -m api"
cache = false
```

Internal dependencies are inferred from `[project].dependencies` when a
dependency name matches another workspace package. In uv, workspace-member
dependencies should also be declared with `tool.uv.sources`:

```toml
[project]
dependencies = ["core==0.1.0"]

[tool.uv.sources]
core = { workspace = true }
```

Explicit `depends_on` is available for task-ordering edges that are not package
dependencies.

## Affected run flow

`joist affected` follows the same broad shape as Nx affected runs: Git decides
which files changed, Joist maps those files onto workspace projects, then the
project graph pulls in dependents that also need validation.

```mermaid
flowchart TD
    A["git diff chooses changed files"] --> B["Map files to project roots"]
    B --> C{"Root coordination file changed?"}
    C -- "yes" --> D["Select every project"]
    C -- "no" --> E["Select directly changed projects"]
    E --> F["Add reverse dependents"]
    D --> G["Topologically sort selected projects"]
    F --> G
    G --> H["Expand target pipelines, such as ^build"]
    H --> I{"Cache hit?"}
    I -- "yes" --> J["Replay cached output"]
    I -- "no" --> K["Run rendered uv command"]
    K --> L["Record successful output in local cache"]
```

Root coordination files currently include:

```text
joist.toml
pyproject.toml
uv.lock
```

## Commands

```sh
uv run python -m joist list
uv run python -m joist graph --format dot
uv run python -m joist run build api
uv run python -m joist run lint --all --dry-run
uv run python -m joist affected test --base origin/main --head HEAD
uv run python -m joist cache clear
uv run python -m joist version minor
```

`depends_on = ["^build"]` means "run the `build` target for dependency projects
before this project." This is the one Nx-style target pipeline rule Joist
supports.

The cache is deliberately simple. It hashes configured input files, the rendered
command, and extra CLI args, then replays cached terminal output for successful
runs. It does not implement remote cache or artifact restoration, so generated
build targets are intentionally uncached by default.

## Build system integration

Treat `joist.toml` targets as the contract between the monorepo and your build
system. Keep the target commands uv-native, then have CI or local automation call
Joist to decide which projects need each target.

For pull requests, fetch enough Git history for the base comparison and run only
affected targets:

```yaml
name: ci

on:
  pull_request:

jobs:
  affected:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --locked
      - run: uv run python -m joist affected lint --base origin/${{ github.base_ref }} --head HEAD
      - run: uv run python -m joist affected test --base origin/${{ github.base_ref }} --head HEAD
      - run: uv run python -m joist affected build --base origin/${{ github.base_ref }} --head HEAD
```

For main-branch validation or nightly builds, run the complete target set:

```yaml
name: full-build

on:
  push:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --locked
      - run: uv run python -m joist run lint --all
      - run: uv run python -m joist run test --all
      - run: uv run python -m joist run build --all
```

For Make-based workflows, delegate the selection logic to Joist instead of
duplicating package lists:

```makefile
.PHONY: lint test build affected-test clean-cache

lint:
	uv run python -m joist run lint --all

test:
	uv run python -m joist run test --all

build:
	uv run python -m joist run build --all

affected-test:
	uv run python -m joist affected test --base origin/main

clean-cache:
	uv run python -m joist cache clear
```

For release pipelines, keep versioning explicit and build every public package
after the bump:

```sh
uv sync --locked
uv run python -m joist version patch
uv lock
uv run python -m joist run build --all --no-cache
```

Before uploading, run the local PyPI readiness checks:

```sh
uv run pytest
uv run ruff check .
uv build
uv run twine check dist/*
uv publish --dry-run --trusted-publishing never
```

Joist does not publish packages for you. Keep publishing as a separate,
auditable step using the uv command and credentials your release system already
controls.

For public PyPI publishing, verify the distribution name immediately before the
first upload.

## Release model

Joist uses one Lerna-inspired release mode: fixed versions. `joist version
patch` bumps every non-private workspace project to the same version and writes a
root `VERSION` file. Internal dependency pins that point at public workspace
packages are updated too, including pins in private projects.

Mark private projects with:

```toml
[tool.joist]
private = true
```

## Design boundaries

Joist is intentionally not a plugin ecosystem, not a remote cache service, and
not a Python package publisher. `uv` owns workspace membership, locking, syncing,
editable internal dependencies, building, and publishing. Joist is a small task
orchestration layer over `uv`, `git`, and project-local commands.

## License

Joist is licensed under the Universal Permissive License 1.0 (`UPL-1.0`).

# ReForge

ReForge is a multi-agent modernization and repair system for legacy Python and
JavaScript/TypeScript repositories. It preserves observed behaviour by
baselining tests before code changes, executes repository code only in Docker,
and uses a deterministic orchestrator to manage agents, budgets, guards,
checkpoints, and repairs. Agents run on CrewAI, Google ADK, or both.

See [docs/IMPLEMENTATION_NOTES.md](docs/IMPLEMENTATION_NOTES.md) for the
security fixes, multi-language support, and CrewAI + ADK architecture added in
October 2026.

## Development setup

ReForge targets Python 3.11 or newer. Install its development dependencies and
run the M0 contract tests:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

The implementation is deliberately built milestone by milestone according to
`ReForge_Workflow_Spec.md`. M0 establishes the shared contracts; M1 adds Docker
sandboxing and the deterministic test runner.

## Sandbox image (M1)

Start Docker Desktop, then build the isolated test image:

```powershell
docker build --tag reforge-sandbox:latest sandbox
```

Target repository commands are never executed on the host. `DockerSandbox`
creates one restricted container per command; dependency installation has
network access, while tests, ruff, and mypy run with networking disabled.

## Deterministic tool layer (M2)

The tool layer inspects source repositories without importing their code. It
parses dependency declarations from `requirements*.txt`, `pyproject.toml`,
`setup.py`, `setup.cfg`, and `Pipfile`; scans Python syntax against the YAML
deprecation catalog; provides workspace-jailed repository and Git operations;
and verifies candidate dependencies using `pip --dry-run` in the sandbox.

## Supported repositories

| Stack | Detection | Baseline tests | Deprecation rules |
|---|---|---|---|
| Python | `requirements*.txt`, `pyproject.toml`, `setup.py/cfg`, `Pipfile`, `test_*.py` | pytest (per-test ids via pytest-json-report), ruff, optional mypy | AST rules in `rules/python_deprecations.yaml` |
| JavaScript / TypeScript | `package.json` (npm, pnpm, or yarn lockfile) | Jest, Vitest, or Mocha with JSON reporters (per-test ids); other `npm test` scripts as one coarse check; `npm run build` fallback; `tsc --noEmit` error count for TypeScript | Line rules in `rules/javascript_deprecations.yaml` (Node, React, jQuery, TypeScript) plus known-deprecated npm packages |
| Polyglot (e.g. Python API + JS frontend) | both of the above | both suites run and are merged into one baseline | both catalogs |
| Android (Java/Kotlin) | Gradle files | — | a narrow text rule set |

Characterization tests are generated for Python (pytest) and for Jest/Vitest
projects when the existing suite is missing or too small.

## Agent runtimes: CrewAI and Google ADK

`AGENT_FRAMEWORK` selects which runtime executes each reasoning agent. The
orchestrator itself stays deterministic Python in every mode.

| Mode | Analysis, planning | Transformation, repair, test generation |
|---|---|---|
| `crewai` | CrewAI | CrewAI |
| `adk` | Google ADK | Google ADK |
| `hybrid` (template default) | Google ADK | CrewAI |

ADK agents run inside one ADK session per run. The session is created with the
run's initial state, every orchestrator stage change and agent result is
appended as an event with a `state_delta`, and each ADK agent reads that run
memory through instruction templating. `GET /runs/{id}/session` returns the
agent runtime per role and the session state; set `ADK_SESSION_DB_URL` to
persist sessions in SQLite or another SQLAlchemy database.

Supported providers: `openai`, `anthropic`, `gemini`, `ollama` (ADK uses Gemini
natively and LiteLLM for the others).

## Human-in-the-loop operation

To start a run, provide a repository URL or ZIP archive. The modernization
objective is optional: when it is blank, ReForge directs its analysis agent to
discover safe modernization opportunities and propose a prioritized plan. A
specific objective is only additional human guidance, for example when a team
already knows it needs to upgrade a particular framework.

## Run the complete stack with Docker Compose

Prerequisites: Docker Desktop is running and an LLM credential has been placed
in the ignored `.env` file. Start from the supplied safe template:

```powershell
Copy-Item .env.example .env
# Edit .env locally and add the credential for the configured provider.
# Docker Desktop on Windows: make the host runs folder visible to sibling sandbox containers.
$env:REFORGE_DOCKER_HOST_RUNS_ROOT = (Join-Path $PWD "runs")
docker compose up --build
```

The dashboard is then available at `http://127.0.0.1:5173`; the FastAPI
service is at `http://127.0.0.1:8000`. Compose builds the disposable
`reforge-sandbox:latest` image before starting the API. The API itself never
executes target repository code: it asks Docker to run that code in the sandbox.
For Docker Desktop bind mounts on Windows, Compose uses root *inside the
otherwise read-only, CPU/memory/PID-limited sandbox* so that the sandbox can
write its per-run virtual environment. Direct local Docker runs keep the
specification's default unprivileged `reforge` user.

## Sandbox security boundary

Each target command receives a disposable container with a read-only root
filesystem, temporary writable storage, CPU, memory, and process limits, all
Linux capabilities dropped, and `no-new-privileges` enabled. The workspace's
`.git` directory is mounted read-only inside the sandbox, and every host-side
Git command runs with hooks, `core.fsmonitor`, and external transports
disabled, so repository content can never execute on the host through Git.
Uploaded ZIPs are extracted without their `.git` directory, symlinks, or unsafe
paths, within size and entry-count limits; `repo_url` accepts network Git URLs
only. Test and lint
commands have no network access; only the dependency-install phase may access
the package network. The `REFORGE_SANDBOX_ROOT_FALLBACK=true` Compose setting
is a logged Windows bind-mount compatibility exception, not the default.

The backend mounts the Docker socket so it can create these disposable
containers. Docker-socket access is effectively high privilege on the host;
run the Compose stack only on a trusted local machine and only process target
repositories you are prepared to inspect in this boundary. Do not expose the
backend or its Docker socket to an untrusted network.

## Reproducible demo

With the Compose stack running, launch one controlled benchmark run:

```powershell
.\scripts\run-demo.ps1 -Benchmark flask-legacy-app
```

The script uploads a benchmark ZIP without an objective, so the analysis agent
performs autonomous discovery. It prints the run ID and report links; the same
run can be observed in the dashboard. This invokes the configured real LLM and
Docker sandbox, so it may consume provider usage and take several minutes.

Every terminal run writes `runs/<run-id>/report.md` and `report.json`. Reports
contain the objective and outcome, dependency and deprecation findings, plan,
Git change statistics, baseline/final tests, repair and replan history,
metrics, unresolved items, and exact reproduction details. The dashboard can
download either format from the Final report view.

For a presentation-ready walkthrough, use [the demo checklist](docs/demo-checklist.md).

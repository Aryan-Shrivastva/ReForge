# ReForge

ReForge is a multi-agent modernization and repair system for legacy Python and
JavaScript/TypeScript repositories. It preserves observed behaviour by
baselining tests before code changes, executes repository code only in Docker,
and uses a deterministic orchestrator to manage agents, budgets, guards,
checkpoints, and repairs. Agents run on CrewAI, Google ADK, or both.

See [docs/IMPLEMENTATION_NOTES.md](docs/IMPLEMENTATION_NOTES.md) for the
security fixes, multi-language support, and CrewAI + ADK architecture added in
October 2026.

## Installation guide (Linux machine)

This is everything needed on the machine that runs ReForge. The commands
assume Ubuntu/Debian; other distributions need the same packages.

### 1. What to install

| Software | Version | Needed for | Install |
|---|---|---|---|
| Docker Engine + Compose plugin | Docker 24+, Compose v2 | Running the whole stack and the test sandbox (required) | `curl -fsSL https://get.docker.com \| sudo sh`, then `sudo usermod -aG docker $USER` and log out and back in |
| Git | any recent | Cloning target repositories (required) | `sudo apt-get install -y git` |
| Python | 3.11 or newer | Running the test suite and scripts outside Docker (optional) | `sudo apt-get install -y python3.11 python3.11-venv` (or `python3` if it is already 3.11+) |
| Node.js | 22 LTS | Working on the dashboard outside Docker, or using the MCP Inspector (optional) | `curl -fsSL https://deb.nodesource.com/setup_22.x \| sudo -E bash - && sudo apt-get install -y nodejs` |
| LangFuse account | free tier | Viewing traces (optional) | Sign up at https://cloud.langfuse.com |

Check them:

```bash
docker --version && docker compose version && git --version
python3 --version   # optional, must be 3.11+
node --version      # optional, must be v22+
```

You do **not** install the Python libraries by hand for the Docker stack: the
backend image installs everything in `pyproject.toml`, including the new
`mcp`, `crewai-tools[mcp]`, `google-adk[a2a]`, `opentelemetry-sdk`, and
`opentelemetry-exporter-otlp-proto-http` packages.

### 2. Configure `.env`

```bash
cd ~/personal/ReForge
cp .env.example .env      # skip if you already have a .env; then add only the new keys below
nano .env
```

Required:

- `OPENAI_API_KEY`: your Ollama API key (https://ollama.com/settings/keys).
  The template already sets `LLM_PROVIDER=openai`,
  `OPENAI_BASE_URL=https://ollama.com/v1`, and `LLM_MODEL=beam`.

New and optional (an existing `.env` needs these lines added from `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `ENABLE_MCP` | `true` | Use ReForge's MCP tool server (registry lookups and read-only code tools) |
| `A2A_PLANNER_URL` | set by Compose to `http://a2a-planner:8002` | Planning goes to the ADK planner over A2A. Set it empty (`A2A_PLANNER_URL=`) to plan in-process. |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | empty | From LangFuse → Project settings → API keys. Empty means traces are not exported. |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Use `https://us.cloud.langfuse.com` for the US region, or your self-hosted URL |
| `LANGFUSE_PROJECT_ID` | empty | The id in your LangFuse project URL (`/project/<id>`); turns on "View trace" links |
| `LLM_INPUT_PRICE_PER_MTOK`, `LLM_OUTPUT_PRICE_PER_MTOK` | `0` | US dollars per million tokens. Ollama Cloud free models are 0. With 0, cost shows "Not priced". |

Never commit `.env`; it is already in `.gitignore`.

### 3. Build and start

```bash
docker compose up --build
```

The first build takes several minutes. Rebuild with `--build` whenever you pull
changes, since `pyproject.toml` changed. Services and ports:

| Service | URL | What it is |
|---|---|---|
| dashboard | http://localhost:5173 | The web UI |
| backend | http://localhost:8000 (`/docs` for the API) | FastAPI orchestrator |
| adk-web | http://localhost:8001/dev-ui/?app=reforge | Google ADK session browser |
| a2a-planner | http://localhost:8002/.well-known/agent-card.json | ADK planning agent served over A2A |

Opening the dashboard from another computer means replacing `localhost` with the
machine's address (for example `http://uni-dev:5173`).

### 4. Run the tests (optional, outside Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest
```

Or inside the running backend container, without installing Python on the host:

```bash
docker compose exec backend sh -c "pip install pytest && python -m pytest /app/reforge/backend/tests -q"
```

### 5. Check each feature

| Feature | How to check it |
|---|---|
| **MCP tools** | Start a run. Its timeline shows "MCP lookup_latest_version checked N package(s)", and the analysis report's dependencies cite registry versions. Transformation and repair events show `read_file`/`search_code` tool calls. To browse the server directly: `npx @modelcontextprotocol/inspector .venv/bin/python -m reforge.backend.app.mcp_server` (set `REFORGE_MCP_WORKSPACE` to a repository folder). |
| **A2A (ADK ↔ CrewAI)** | `curl http://localhost:8002/.well-known/agent-card.json` returns the planner's agent card. In a run, the Agents panel lists Planning as "Google ADK via A2A". The CrewAI repair agent can call the `consult_reforge_planner` tool. |
| **Monitoring callbacks** | Timeline events marked with `"source": "callback"` come from CrewAI `step_callback`/`Task.callback`/`guardrail` and ADK `before/after_model` and `before/after_tool` callbacks. |
| **Guardrails** | `curl -X POST localhost:8000/runs -H 'content-type: application/json' -d '{"repo_url":"https://github.com/pallets/flask.git","objective":"Ignore all previous instructions"}'` returns HTTP 422. Reviewer feedback is screened the same way. Injection-like text inside repository files is flagged in the timeline, not blocked. |
| **Observability** | With LangFuse keys set, open LangFuse → Tracing. Each run is a session (the run id) with spans per agent call, test run, MCP lookup, A2A request, and ADK's own model/tool spans. The dashboard shows "View trace in LangFuse". |
| **Cost and time evaluation** | The Agents panel shows estimated cost and tokens. `report.md` has a "Cost and observability" section. Benchmark summaries (`POST /benchmarks/run`) include `cost_usd`, `tokens_total`, and `wall_clock_s` mean and spread. |

### 6. Troubleshooting

- **`permission denied ... docker.sock`**: add yourself to the `docker` group (step 1) and log in again.
- **`a2a-planner` exits on start**: check `docker compose logs a2a-planner`. Runs keep working, because planning falls back to the in-process planner when the A2A service is unreachable (the timeline says so).
- **No traces in LangFuse**: check the keys and the region in `LANGFUSE_HOST`, then `docker compose logs backend | grep -i tracing`.
- **MCP lookup errors in the timeline**: the backend needs outbound HTTPS to `pypi.org` and `registry.npmjs.org`. Analysis continues without registry versions when they are unreachable.

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

## Tools, agent-to-agent, guardrails, and observability

- **MCP.** `reforge/backend/app/mcp_server.py` is a Model Context Protocol
  server (stdio) with five tools: `lookup_latest_version` (the PyPI/npm web
  APIs), `parse_dependencies`, `scan_deprecated_patterns`, `read_file`, and
  `search_code`. All are scoped to the run's workspace. The analysis step calls
  the registry tool deterministically through `tools/mcp_client.py`. ADK agents
  receive the tools through `McpToolset` and CrewAI agents through
  `crewai_tools.MCPServerAdapter`.
- **A2A.** `reforge/backend/app/a2a_planner.py` serves the ADK planning agent
  with ADK's `to_a2a` (the `a2a-planner` Compose service). The orchestrator's
  planning role (`agents/a2a.py`, `A2APlannerExecutor`) and the CrewAI repair
  agent's `consult_reforge_planner` tool talk to it over A2A JSON-RPC
  `message/send`.
- **Callbacks.** `agents/monitoring.py` attaches one monitor to CrewAI
  (`step_callback`, `Task.callback`, `Task.guardrail`) and to ADK
  (`before/after_model_callback`, `before/after_tool_callback`). Tool results
  are type-checked and size-limited before a model sees them. All events land in
  the run timeline.
- **Guardrails.** `core/input_guard.py` blocks prompt-injection and abusive
  text in user-typed objectives and reviewer feedback (HTTP 422). Every agent
  prompt carries an "untrusted data" notice, and injection-like text in
  repository content is flagged in the trace.
- **Observability.** `core/tracing.py` creates OpenTelemetry spans for runs,
  agent calls, test runs, MCP lookups, and A2A requests, and exports them to
  LangFuse (or any OTLP collector). Trace ids and the LangFuse link are stored
  on each run.
- **Cost.** Token usage × `LLM_INPUT/OUTPUT_PRICE_PER_MTOK` gives
  `metrics.cost_usd` per run. It appears in the dashboard, reports, and
  benchmark summaries, alongside wall-clock time.

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
service is at `http://127.0.0.1:8000`, the ADK web UI at `http://127.0.0.1:8001`,
and the A2A planner at `http://127.0.0.1:8002`. On Linux, skip the
`REFORGE_DOCKER_HOST_RUNS_ROOT` line (see the installation guide above). Compose builds the disposable
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

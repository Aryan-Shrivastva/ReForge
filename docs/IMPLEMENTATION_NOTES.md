# ReForge: security fixes, JavaScript support, and CrewAI + Google ADK

**Date:** 25 October 2026 · **Base commit:** `f7540cf` · **Status:** implemented and unit/integration-tested; **not yet run end-to-end under Docker** (see [Testing on a Docker machine](#testing-on-a-docker-machine)).

This document records what changed, why, where the code lives, and how to verify it. It covers four pieces of work:

1. [Fixes for the issues found in the project analysis](#1-fixes-for-the-analysed-issues) (security, budgets, routing, guards, success criteria, API lifecycle).
2. [Multi-language support](#2-multi-language-support-javascript--typescript): ReForge now modernizes JavaScript/TypeScript repositories as well as Python, including mixed repositories.
3. [CrewAI and Google ADK together](#3-crewai-and-google-adk-together), with a per-run **ADK session whose state is updated through events**, following the `run_agent_with_session.py` session-with-state-update pattern.
4. [Configuration, API, and test changes](#4-configuration-and-api-changes).

---

## Summary

| Area | Before | After |
|---|---|---|
| Uploaded/cloned repos | Could execute code on the host through Git hooks or `core.fsmonitor` in a bundled `.git`, or through hooks planted by sandboxed code | Host Git never runs repository-controlled programs; `.git` is stripped from uploads and read-only inside the sandbox |
| Budgets | `MAX_REPAIRS`, `MAX_REPLANS`, `MAX_NO_PROGRESS`, `MAX_WALL_CLOCK_S` ignored in real runs; `MAX_TOKENS` never enforced | Settings drive every run's budgets; tokens are counted from both runtimes and enforced |
| TEST routing | Any failed plan step ⇒ PARTIAL "baseline verification passed", even when tests had regressed; repair never ran | Regressions always go to repair first; PARTIAL only when the baseline really holds |
| Test-weakening guards | Bypassable (new `conftest.py`, `skipif`, `pytest.skip()`, config edits, no JS rules) | Python + JS skip/focus markers, test-infrastructure files, and selection-weakening config are rejected |
| Success criteria | Required zero deprecations repo-wide (incl. unrelated Android rules) and zero errored tests (incl. pre-existing) | Spec §7: only patterns *targeted by the plan* or *introduced by the run*; only *new* errors |
| Languages | Python (JS only as a single pass/fail `npm test`) | Python, JavaScript/TypeScript (Jest/Vitest/Mocha per-test results), polyglot repos |
| Agent runtime | CrewAI only | CrewAI, Google ADK, or hybrid; ADK session state carries run memory |
| Test suite | 74 tests (1 failing) | 172 tests, all passing (170 + 2 that need CrewAI/ADK installed, which pass in an environment that has them) |

---

## 1. Fixes for the analysed issues

### 1.1 Host code execution through Git (critical)

**Problem.** `POST /runs` with a ZIP extracted the archive and then ran `git init`, `git add .`, and `git commit` on the host. `git init` keeps an existing `.git` directory, so an archive containing `.git/config` with `core.fsmonitor = <command>`, or `.git/hooks/pre-commit`, executed that command in the backend container, which holds the Docker socket. Separately, the sandbox mounted the workspace (including `.git`) read-write, so code running *inside* the sandbox could plant a hook that the host's next `git commit` would run.

**Fix.**

- **`tools/safe_git.py` (new).** `run_git()` is now the only way the host runs Git. Every command gets `-c core.hooksPath=<devnull> -c core.fsmonitor=false -c protocol.ext.allow=never -c submodule.recurse=false`. Command-line config overrides repository config, so a planted `.git/config` cannot re-enable these. It also strips `GIT_EXTERNAL_DIFF`, `GIT_SSH_COMMAND`, `GIT_ASKPASS`, `GIT_DIR`, and `GIT_WORK_TREE` from the environment and disables prompts.
  - Used by `GitTools`, `RepositoryTools.apply_patch`, `ReportGenerator`, the `/diff` endpoint, cloning, and benchmark setup.
  - Commits use `--no-verify`, and diffs use `--no-ext-diff --no-textconv`.
- **`initialize_repository()`** refuses to run over an existing `.git`.
- **ZIP extraction** (`RunService._extract_archive`) is now member-by-member:
  - drops every `.git` path;
  - rejects symlinks and absolute, drive-letter, backslash, or `..` paths;
  - bounds the entry count and the expanded size, both declared and actually written (zip-bomb protection);
  - strips a single top-level folder, as GitHub "Download ZIP" archives have, so the project root becomes the workspace root.
- **Upload size** is capped while streaming (`MAX_UPLOAD_BYTES`, HTTP 413).
- **Clone sources.** `repo_url` accepts only `https://`, `http://`, `ssh://`, `git://`, or `user@host:path`. `ext::`, `file://`, bare paths, and option-like values are rejected. Clones use `core.symlinks=false` and `--no-recurse-submodules`.
- **Sandbox** (`tools/sandbox.py`): `.git` is mounted **read-only** at `/work/.git`, nested over the writable `/work`.

**Tests:** `test_security.py`. The hook/fsmonitor test plants both and asserts nothing runs. The same repository *does* execute the planted hook under plain `git`, which shows the test is meaningful.

### 1.2 Budgets from settings, and token enforcement

**Problem.** `WorkflowRunner.initialize()` used `Budgets()` defaults, so environment and API settings never reached a live run. Tokens were never counted.

**Fix.**

- **Budgets.** `budgets_from_settings()` builds `Budgets` from `Settings`. `WorkflowRunner.live()` passes it as `default_budgets`, and benchmark runs inherit it too.
- **Token counts.** Each executor keeps a `TokenUsage` counter:
  - CrewAI reads `CrewOutput.token_usage`.
  - ADK sums `event.usage_metadata`.
  - `_agent_call` adds the difference to `metrics.tokens_in` / `tokens_out` after every attempt.
- **Enforcement.** `_enforce_token_budget` aborts the run (`ABORTED`, "Token budget exceeded.") as soon as usage passes `max_tokens`.
- **Wall clock.** The wall-clock check now includes time recorded by earlier `run()` calls.

**Tests:** `test_workflow_fixes.py::test_settings_budgets_are_applied_to_new_runs` and `::test_token_budget_is_tracked_and_aborts_the_run`.

### 1.3 TEST-stage routing reported regressions as PARTIAL

**Problem.** The TEST stage checked "are there unapplied plan steps?" *before* looking at the test results. Any failed step produced PARTIAL with the message "baseline verification passed", even when the applied steps had broken tests, and the repair loop never ran.

**Fix.** The TEST stage logic now lives in `WorkflowRunner._evaluate_tests()` and checks in this order:

1. All success criteria met → `SUCCESS`.
2. Regressions (a frozen baseline test no longer passes, a *new* error, or a failed install) → repair, with the regressed test IDs recorded in the trace.
3. No regressions but unapplied steps → `PARTIAL`, with an accurate message and the list of unapplied steps.
4. Otherwise (for example, targeted deprecations remain) → repair.

**Tests:** `test_regression_with_unapplied_steps_goes_to_repair_not_partial`, `test_partial_is_reported_only_when_the_baseline_actually_holds`.

### 1.4 Guards that could be bypassed

**Fix** (`core/guards.py`, with classification in `tools/languages.py`):

- **Test infrastructure is untouchable** by both transformations and repairs, whether new or existing:
  - Python: `conftest.py`, `sitecustomize.py`, `usercustomize.py`, `*.pth`;
  - JavaScript: `jest/vitest/mocha.setup.*`, `setupTests.*`;
  - any test file, and anything under `tests/`, `test/`, `__tests__/`, or `spec/`.
  - New *source* files are still allowed.
- **Python markers:** `skipif`, `pytest.skip()/xfail()/importorskip()`, `@skip*`, `self.skipTest`, `raise SkipTest`, and `from unittest import skip`.
- **JavaScript markers:** `it/test/describe.skip|only|todo|failing`, `xit/xdescribe/fit/fdescribe`, and Mocha's `this.skip()`. A lookbehind keeps ordinary calls such as `model.fit(` from matching.
- **Selection weakening in configuration:**
  - pytest: `--deselect`, `--ignore`, `-k`, `-p no:`, `testpaths`, `collect_ignore`, …
  - Jest: `testPathIgnorePatterns`, `testMatch`, `passWithNoTests`, …
  - Vitest: `include/exclude`.
  - npm scripts: `|| true` and `exit 0`.
- **Assertion removal** is checked per language. Python `assert` statements are checked in Python files; `expect(`/`assert.` removals are checked only in JS test files, so refactoring `assert(...)` out of production code is allowed.
- **Dependency rollback** now requires the dependency **name and its exact prior constraint as whole tokens on the same line**. The old substring check rejected `2.0.1` because it contains `2.0`.
- **Diff paths** with spaces, Git C-quoting (`"caf\303\251.py"`), and tab timestamps parse correctly. Backslash paths are rejected.

**Tests:** `test_guards_extended.py` (33 cases).

### 1.5 Success criteria that could never be met

**Fix** (`WorkflowRunner.remaining_targeted_patterns`, following spec §7 criterion 5). A remaining deprecated pattern blocks SUCCESS only if:

- it is in a file declared by an **applied** plan step, or
- it is **new**, meaning it was absent from the analysis baseline (matched by rule, file, and snippet) and so was introduced by this run.

Pre-existing errored tests no longer block success; only *new* errors do (criterion 3).

**Tests:** `test_success_ignores_out_of_scope_patterns_but_not_targeted_or_new_ones`, `test_pre_existing_errors_do_not_block_success_but_new_errors_do`.

### 1.6 API lifecycle

| Issue | Fix (`api/runs.py`) |
|---|---|
| Cancel race: the API wrote `ABORTED` while the worker kept saving, so state flipped back | While a worker thread is alive, **only the worker writes `state.json`**. Cancel sets an event and a `cancel.requested` marker file; the worker persists `ABORTED` at its next checkpoint. Cancel still writes directly when no worker is alive |
| Runs stuck forever after a restart | `recover_interrupted_runs()` marks non-terminal runs FAILED (ABORTED if a cancel marker exists). It runs in the app **startup lifespan**, not on import, so importing the app from tests or tools never touches live runs |
| Unbounded concurrency | `MAX_CONCURRENT_RUNS` semaphore; queued runs log "Waiting for a free run slot" and can be cancelled while waiting |
| Unvalidated `config_overrides` | `Settings` validates types and bounds in `__post_init__`, so every construction path is checked; `Settings.with_overrides()` rejects unknown keys. Invalid overrides return **422 synchronously** instead of failing later in the background thread |

**Tests:** `test_api_lifecycle.py`, `test_config.py`.

### 1.7 CrewAI `appdirs` import failure

`appdirs` is now a declared dependency, and `crewai_adapter._load_crewai` tolerates its absence and the absence of `crewai_core`. The real-CrewAI construction test now skips when CrewAI is not installed instead of failing. In an environment with CrewAI 1.15.30 installed, it passes.

---

## 2. Multi-language support: JavaScript / TypeScript

ReForge previously treated a `package.json` repository as one pass/fail `npm test` check. JavaScript is now a first-class target.

| Capability | Implementation |
|---|---|
| Language detection | `tools/languages.py`: `detect_languages`, `is_test_file`, `is_test_infrastructure`, `detect_js_test_framework` (Jest/Vitest/Mocha), `detect_js_package_manager` (npm/pnpm/yarn), and a pruned walker that never enters `node_modules`, virtualenvs, `dist`, `build`, or `coverage` |
| Deprecation rules | `rules/javascript_deprecations.yaml` has 18 line rules: Node (`new Buffer`, `url.parse`, `fs.exists`, `util.is*`, `crypto.createCipher`, `domain`), the `request` package, `substr`, React (`ReactDOM.render`, unsafe lifecycles, `findDOMNode`, string refs, `createClass`, `React.PropTypes`), jQuery legacy events, `moment`, and the TypeScript `module` keyword. Comment lines and minified files are skipped. Rules can be limited by file extension |
| Deprecated npm packages | `request`, `node-sass`, `tslint`, `babel-eslint`, `istanbul`, `querystring`, `crypto`, `@babel/polyfill`, … are flagged deterministically, so the model cannot omit them |
| Analysis evidence | Languages, `engines.node`, package scripts, `main`/`bin` entry points, JS test files (`*.test|spec.*`, `__tests__/`), and a JS test-case count. The model is told which languages are present |
| Per-test baseline | Jest/Vitest (`--json --outputFile`) and Mocha (`--reporter json`) run through `npm run test --` when the test script calls the framework directly, preserving the project's own arguments; otherwise through `npx --no-install`. Results become `path::full test name` IDs, frozen like pytest node IDs. Suite load failures and Mocha hook failures are recorded as errors |
| Fallbacks | Other `npm test` scripts give one coarse `<npm-test>` check; with no tests, `npm run build` is used as an honestly labelled build check |
| Type checks | Projects with `typescript` and a `tsconfig.json` record `tsc --noEmit` error counts, so "type errors not worse than baseline" applies |
| Package managers | npm (`npm ci` with a lockfile), pnpm, and yarn through **corepack**. The sandbox image now ships Node 22 LTS with corepack instead of Debian's end-of-life Node 18 |
| Polyglot repos | When a `package.json` repo also has Python tests, both suites run and their reports merge into one baseline. A tooling-only `pyproject.toml` does *not* trigger a Python run |
| Characterization tests | Generated for Jest/Vitest projects as `tests/reforge_generated/*.test.{js,mjs,ts}`, using the project's module system (CommonJS or ESM). TypeScript tests are used only when the runner executes TypeScript (Vitest, or ts-jest) |
| Guards | JS markers, setup files, config weakening, and assertion removal (§1.4) |
| Prompts | Language-neutral. Lockfiles are never hand-edited (the sandboxed install regenerates them), and codemods are Python-only |
| Benchmark | `benchmarks/node-legacy-app` (Jest; `request`, `url.parse`, `new Buffer`, `util.isArray`) |

**Verified against real tools (Node 22.19, Jest 29) on this machine.** `TestRunner` ran the new benchmark end-to-end through a local stand-in for the Docker sandbox:

- The real Jest JSON parsed into four workspace-relative test IDs.
- A deliberately introduced regression was attributed to the single affected test (`encoding joins words`).

---

## 3. CrewAI and Google ADK together

### 3.1 Design

All agents call one interface, `AgentExecutor.run(role, goal, backstory, prompt, expected_output, output_model)`, defined in `agents/executor.py`. That call returns a validated Pydantic model. Each agent's constructor takes `executor=` (formerly `crew_executor=`). `build_agent_executors(settings)` assigns a runtime per role:

| `AGENT_FRAMEWORK` | analysis | planning | transformation | repair | test_generation |
|---|---|---|---|---|---|
| `crewai` (code default) | CrewAI | CrewAI | CrewAI | CrewAI | CrewAI |
| `adk` | ADK | ADK | ADK | ADK | ADK |
| `hybrid` (`.env.example` default) | **ADK** | **ADK** | CrewAI | CrewAI | CrewAI |

**Why this split in hybrid mode.**

- Analysis, planning, and replanning form a multi-step reasoning chain. Analyse, then plan, then (after failed repairs) replan with the history. ADK's session state carries that history between calls as structured memory.
- Transformation, repair, and test generation are single, role-scoped patch-authoring tasks. CrewAI's role/goal/backstory agent fits these well.
- One executor instance per framework is shared across roles, so a run has one ADK session and one combined token count.

**What does not change.** The orchestrator stays deterministic. Neither runtime ever decides the next stage. Every output is schema-validated, and every patch passes the guards.

### 3.2 The ADK session-with-state-update pattern

`agents/adk_adapter.py` applies the pattern from [`run_agent_with_session.py`](https://github.com/soharabhossain/Agentic_AI/blob/main/DEMO/ADK/session_runner_demo/session_with_state_update/run_agent_with_session.py) to every ReForge run:

| Demo step | ReForge equivalent |
|---|---|
| Create `InMemorySessionService` | `InMemorySessionService`, or `DatabaseSessionService(db_url=ADK_SESSION_DB_URL)` for persistence (Compose defaults to SQLite under `runs/`) |
| `create_session(app_name, user_id, state={...})` with initial state | `ADKExecutor.bind_run(run_id, initial_state)`, called by `WorkflowRunner.initialize()`. Session ID = run ID. Initial state: run ID, repository, objective, stage, and so on |
| `Runner(agent=root_agent, ...)` + `run_async(new_message=...)` | Each agent call builds an `LlmAgent` (name from the role, `include_contents="none"`, `output_key="<agent>_output"`) and runs it through a `Runner` on the run's session. The message is the agent's full prompt |
| Agent instruction reads state | The instruction includes run memory through ADK templating: `{objective?}`, `{stage?}`, `{languages?}`, `{analysis_summary?}`, `{plan_summary?}`, `{repair_history?}`, `{agent_history?}`. Caller text is brace-escaped so it never collides with templating |
| On final response, append `Event(actions=EventActions(state_delta={"response": ...}))` | After each validated result: an `Event` whose `state_delta` stores `<agent>_validated`, appends to `agent_history`, and increments `agent_calls`. The orchestrator also appends a `state_delta` event at **every stage transition** (stage, plan summary, repair history, token use, final status) |
| Print state before/after | State is mirrored into `ProjectState.agent_session` and exposed at **`GET /runs/{id}/session`**, in the dashboard's *Agent runtime* panel, and in the final report |

**Model support.** Gemini models run natively. OpenAI, Anthropic, and Ollama go through ADK's `LiteLlm` wrapper (`OPENAI_BASE_URL` and `OLLAMA_BASE_URL` are honoured). GPT-5 models omit `temperature`, as in the CrewAI adapter.

**Async bridging.** Each executor owns one private event loop, so ADK's async API, including a database session engine, stays bound to a single loop inside the run's worker thread.

**Verified against real google-adk 1.14.1** in an isolated virtualenv, using a deterministic `BaseLlm` instead of a network model. For both the in-memory and SQLite session services, the test confirmed:

- the session is created with initial state;
- templating resolves state into the instruction;
- `output_key` and the system `state_delta` events update state;
- token usage is captured;
- validated output is returned.

`test_agent_executors.py::test_real_adk_runner_session_and_state_delta` repeats this check wherever ADK is installed.

---

## 4. Configuration and API changes

### New settings (`.env.example`)

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `openai` | Now also accepts `gemini` (uses `GOOGLE_API_KEY` or `GEMINI_API_KEY`) |
| `AGENT_FRAMEWORK` | `crewai` (template: `hybrid`) | `crewai`, `adk`, or `hybrid` |
| `ADK_SESSION_DB_URL` | unset (in-memory); Compose: `sqlite:////app/runs/adk_sessions.db` | Persistent ADK sessions |
| `MAX_CONCURRENT_RUNS` | `2` | Concurrent run worker cap |
| `MAX_UPLOAD_BYTES` | 200 MiB | Upload size cap |
| `MAX_ARCHIVE_UNCOMPRESSED_BYTES` | 1 GiB | ZIP expansion cap |
| `MAX_ARCHIVE_MEMBERS` | 50 000 | ZIP entry cap |

### Dependencies (`pyproject.toml`)

- added `google-adk>=1.14,<2`, `litellm>=1.75.5,<2`, and `appdirs>=1.4,<2`;
- CrewAI now uses the `crewai[anthropic,google-genai]` extras.

In a clean virtualenv these resolved to CrewAI 1.15.30, google-adk 1.14.1, litellm 1.106.4, and pydantic 2.12.5.

### API

- `GET /runs/{id}/session` (new): `{agent_runtime: {role: framework}, state: {...ADK session state}}`.
- `POST /runs`:
  - returns 422 for an invalid `repo_url` or `config_overrides`;
  - returns 413 for oversized uploads.
- `POST /runs/{id}/cancel` on a running run returns the current state plus a "cancellation requested" event; the worker finalizes `ABORTED`.
- `ProjectState` gained `agent_runtime` and `agent_session`, and `AnalysisReport` gained `languages`. All have defaults, so state files saved before this change still load.

### Files

New:

- `agents/executor.py`, `agents/adk_adapter.py`
- `tools/safe_git.py`, `tools/languages.py`
- `rules/javascript_deprecations.yaml`
- `benchmarks/node-legacy-app/`
- six test modules

Substantially changed:

- `core/workflow.py`, `core/guards.py`, `core/config.py`
- `api/runs.py`
- `agents/testing.py`, `agents/test_generation.py`, `agents/analysis.py`, `agents/crewai_adapter.py`
- `tools/ast_tools.py`, `tools/sandbox.py`
- `sandbox/Dockerfile`
- the dashboard's Overview and Analysis panels

---

## Testing on a Docker machine

Docker was not available where these changes were made. Everything below the Docker boundary is covered by tests, but the following has **not** been executed yet:

1. **Build the images.** `docker compose up --build`. Then confirm the sandbox build prints Node, npm, and corepack versions, and the backend installs google-adk and litellm.
2. **Python regression run.** Run `.\scripts\run-demo.ps1 -Benchmark flask-legacy-app` and expect the same behaviour as before.
3. **JavaScript run.** Run `.\scripts\run-demo.ps1 -Benchmark node-legacy-app`. Expect:
   - the baseline lists four `tests/behavior.test.js::…` IDs;
   - the plan replaces `request`, `url.parse`, `new Buffer`, and `util.isArray`.
4. **Hybrid runtime.** With `AGENT_FRAMEWORK=hybrid`, open the dashboard Overview:
   - *Agent runtime* shows analysis/planning = ADK and the rest = CrewAI;
   - the ADK session-state stage advances.
   - `GET /runs/<id>/session` returns the same data.
5. **Read-only `.git` mount on Docker Desktop for Windows.** This is the change most likely to need adjustment. Check that the nested bind of `<host runs root>/<id>/workspace/.git` is accepted. If it is not, the sandbox fails to start with a clear `DockerUnavailable` error.
6. **pnpm/yarn projects.** Corepack downloads the package manager during the install phase, which has network access. Confirm on a pnpm repo.
7. **Cancel and restart.** Cancel a running run and confirm it ends `ABORTED`. Restart the backend mid-run and confirm the run becomes `FAILED` with the "service restarted" event.

To run the unit suite: `python -m pip install -e ".[dev]"`, then `python -m pytest`. Expect 172 tests passing. The real-CrewAI and real-ADK tests run whenever those packages are installed.

## Update 26 October 2026: first Docker test round

### Every run failed at baseline: fixed

**Symptom.** Every run failed at the baseline step within about 7 seconds, with 0 tests passed and 0 failed. The install log said `npm ci` could not find a `package-lock.json`.

**Root cause.**

- The backend runs inside a container, so it starts sandbox containers through the host's Docker daemon.
- Without `REFORGE_DOCKER_HOST_RUNS_ROOT`, it passed its container-internal path `/app/runs/<id>/workspace` as the bind source.
- That path doesn't exist on the host, so Docker silently created an empty folder there. The sandbox therefore got an empty `/work`.
- Confirmed on the Linux host: root-owned phantom folders appeared at `/app/runs/<id>`.

**Fix** (`tools/sandbox.py`):

- **Automatic host-path detection.** The backend reads its own container's mounts through the Docker API and translates `/app/runs/...` to the real host path, so the variable is no longer needed.
- **Mount verification.** Each sandbox command first checks a one-time token written into `.git/`. If `/work` is the wrong directory, the run fails with a clear "workspace mount" error instead of a misleading npm or pip failure.

**Visibility.** The dashboard now shows the install log, and an explicit message when a repository has no runnable tests.

**Clean-up on hosts that ran the old build:** `sudo rm -rf /app/runs`, plus `sudo rmdir /app` if it is now empty. Both were created by Docker, not by you.

### Human approval before any change (`REQUIRE_PLAN_APPROVAL=true`, the default)

After planning and recording the baseline, a run stops at the new `AWAIT_APPROVAL` stage, and its worker thread exits. Nothing in the repository has changed at that point. The reviewer decides in the dashboard, or through `POST /runs/{id}/review`, with one of three outcomes:

- **`approved`**, optionally with `approved_steps`. Steps left out are marked `declined`, and so is any step that depends on one of them. The run resumes and applies only the approved steps. Declined steps do not make a run PARTIAL.
- **`revision_requested`**, with feedback. The planner receives the feedback and the previous plan, produces a new version, and the run waits for approval again. The baseline is reused, not re-run.
- **`rejected`**. The run ends ABORTED with a report, and no code is changed.

A replacement plan produced by the repair loop (REPLAN) also requires approval. Every decision is stored in `ProjectState.reviews`. Paused runs survive a backend restart, and benchmark suites always run unattended.

### Reliable file edits (fixes "patch does not apply")

Agents used to write unified diffs, and models often get line counts or context wrong, which made `git apply` reject the change. Transformation and repair agents now return **search/replace edits** (`FileEdit`):

- ReForge requires each `search` snippet to appear exactly once in the current file. If it doesn't, the error names the closest matching line so the agent's retry can correct it.
- ReForge computes the new file contents itself and derives the unified diff, which goes through the same mutation guards.
- Files are written with their original line endings, CRLF included.

Unified-diff patches remain accepted as a fallback.

### Dependency-rollback guard

The guard now treats a patch as a rollback only if it removes a *different* constraint for a dependency and adds back its original one. Moving or copying a declaration at its unchanged version (consolidating manifests, moving a package to devDependencies) is allowed.

### Repositories without tests

After the mount fix, CharitEase installed successfully (`added 106 packages`) but still failed. Its only test script is npm's placeholder, so there was no baseline to protect changes. ReForge now handles these repositories in two steps.

1. **Generated `node:test` tests.** For JavaScript projects without Jest or Vitest, the generator writes characterization tests for Node's built-in test runner. That needs no dependencies, and Node 22 is in the sandbox.
   - The runner executes each file with `node --test --test-reporter=junit`, one file at a time, so test IDs are `path::suite test`.
   - Projects whose own test script is `node --test` are also detected and run this way.
2. **Syntax-check baseline.** If no test passes even after generation, the baseline becomes one syntax check per source file: `node --check` for JavaScript and `ast.parse` for Python. Modules are not imported, because importing an app often connects to a database or starts a server.
   - The run proceeds and the checks guard every change.
   - The run record sets `smoke_baseline: true`, and the dashboard and report state plainly that behavior was not tested.

Both paths were verified with real Node 22 and Python against a framework-free project.

### Dashboard: deployment-console layout (Vercel/Railway style)

- **Runs page:** a full-width list showing repository, status, run ID, duration and age, with search and filters.
- **Run page:** a large status header, then a *What happened* card in plain language with next steps.
- **Steps:** a vertical list (Analyze → Plan → Record baseline → Apply changes → Verify → Write report). Each step shows a status icon, duration and a one-line result, and expands to its details: the install log, the tests recorded, the plan, per-change errors and repairs.
- **Changes:** per-file colored diffs.
- **Logs:** a terminal-style event stream.
- **Theme:** light only, with Geist type.

### Dashboard redesign (first pass, superseded by the layout above)

- **Look:** neutral, light-only styling with no gradients or glows.
- **Run list:** runs are named by repository, with search and status filters.
- **Starting runs:** a "New run" dialog.
- **Run page:** a stage timeline shows where a run failed, and a banner gives the reason.
- **Changes and report:** the diff and report load automatically; the diff is colored.
- **Navigation:** Benchmarks is its own page, and URLs are shareable (`#/runs/<id>/tests`).
- **Code:** split into `components/`, `views/` and `lib/`.

`.gitignore` no longer hides `dashboard/src/lib/`; its Python `lib/` rule had matched that folder.

### Google ADK web UI

`docker compose up` now also starts **adk-web** on port 8001. It runs ADK's developer UI against the same session database the backend writes. Open http://127.0.0.1:8001/dev-ui/?app=reforge and pick a session, whose ID is the run ID. You can also use **Open in ADK web UI** in a run's Agents panel. The tabs show:

- **Events:** every agent result and orchestrator stage update, each carrying its `state_delta`;
- **State:** the session state.

The `reforge` app (`adk_apps/reforge/agent.py`) is a read-only run inspector. If you chat with it, it answers from the session state and calls your configured model. ReForge's ADK user ID is now `user`, matching the dev UI's default.

### Where data is stored

Everything lives under `./runs` on the host, mounted at `/app/runs` in the containers. There is no database server.

| Path | Contents |
|---|---|
| `runs/<run-id>/state.json` | The full run record: stage, analysis, plan, test reports, repairs, metrics, trace, agent runtime, and a mirror of the ADK session state |
| `runs/<run-id>/workspace/` | The repository as a Git repo. ReForge's changes are commits on branch `reforge/<run-id>` |
| `runs/<run-id>/report.md`, `report.json` | Final reports |
| `runs/<run-id>/upload.zip` | The original upload, for ZIP runs |
| `runs/<run-id>/cancel.requested` | Cancellation marker |
| `runs/adk_sessions.db` | SQLite store of Google ADK sessions: events and state per run |
| `runs/evaluation/` | Benchmark-suite runs and summary JSON |

CrewAI keeps only throwaway runtime files, in `.reforge-crewai/` inside the backend container. To start fresh, stop the stack and delete `./runs`.

## Known limitations

- JavaScript deprecation rules are line-based regexes, not an AST. They skip comment lines but can still match inside strings or multi-line comments.
- Characterization-test generation is supported only for Jest and Vitest, not Mocha or `node:test`.
- `npm test` scripts that wrap the framework (e.g. `cross-env … jest`) run the framework through `npx --no-install`, which drops the wrapper's environment variables.
- Codemods (pyupgrade, ruff, bump-pydantic) remain Python-only; JavaScript changes are LLM-authored patches checked by the guards.
- ADK sessions hold *advisory* memory. The deterministic `ProjectState` remains the source of truth, and an ADK sync failure is logged but never fails a run.

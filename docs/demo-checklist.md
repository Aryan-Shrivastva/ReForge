# ReForge demo checklist

## Before presenting

1. Start Docker Desktop and run `docker compose up --build` from the project root.
2. Confirm `http://127.0.0.1:5173` loads the dashboard and `http://127.0.0.1:8000/health` returns `ok`.
3. Keep `.env` local and do not display its contents.

## Five-minute walkthrough

1. Open the dashboard and select a completed successful run.
2. In **Overview**, show the deterministic pipeline and repair/replan counters.
3. In **Analysis** and **Plan**, show evidence-backed findings and step statuses.
4. In **Tests**, compare the frozen baseline with final results.
5. In **Trace**, point out an agent decision, a rejected/guarded action, and a repair checkpoint.
6. In **Report**, download the Markdown or JSON artifact and show the reproduction details.
7. In **Benchmarks**, show aggregate evaluation metrics and the saved JSON summary path.

## Recommended examples

- Use `flask-legacy-app` to show dependency repair and a successful report.
- Use `no-tests-characterization` to show generated baseline tests protecting behavior.
- Use `stdlib-deprecations` to show that ReForge refuses a modernization when it conflicts with frozen behavior.

## Talking points

- The LLM proposes; deterministic guards, Git checkpoints, Docker isolation, and tests decide.
- Repository code never runs directly on the host.
- A successful run means the frozen baseline remained valid, not merely that an agent produced a patch.

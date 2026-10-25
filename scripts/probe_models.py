"""Compare LLMs on the two things ReForge needs most: schema-valid JSON and applicable diffs.

Calls an OpenAI-compatible endpoint (default: Ollama Cloud at https://ollama.com/v1)
with ReForge's real Plan schema and a real patch task, then checks each answer
deterministically: Pydantic validation for the plan, ``git apply --check`` for
the patch.  Reports pass rate, latency, and token use per model.

Usage (OLLAMA_API_KEY or OPENAI_API_KEY in the environment or .env):

    python scripts/probe_models.py
    python scripts/probe_models.py --models beam gpt-oss:120b --rounds 3
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from reforge.backend.app.agents.base import StructuredAgent  # noqa: E402
from reforge.backend.app.agents.planning import validate_plan  # noqa: E402
from reforge.backend.app.core.state import Plan, TransformationProposal  # noqa: E402
from reforge.backend.app.tools.repo_tools import normalise_unified_diff_hunks  # noqa: E402

# Free Ollama Cloud models (github.com/syedkhalid0/OllamaCloud-Free-Models, October 2026).
FREE_MODELS = [
    "beam",
    "nemotron-3-ultra",
    "gpt-oss:120b",
    "nemotron-3-super",
    "gemma4:31b",
    "nemotron-3-nano:30b",
    "gpt-oss:20b",
]

SOURCE = """const url = require('url');

function hostOf(address) {
  return url.parse(address).hostname;
}

module.exports = { hostOf };
"""

PLAN_PROMPT = f"""You are ReForge's Planning Agent. Produce a small, ordered modernization plan.
Analysis: the Node.js file src/client.js calls the deprecated url.parse() (DEP0169) and the project depends on the
deprecated `request` package (declared in package.json). Tests live in tests/behavior.test.js and must not be edited.
Known workspace files: ["package.json", "src/client.js", "src/encoding.js", "tests/behavior.test.js"]
Step ids must match ^S\\d+$, dependencies must form an acyclic DAG, kind is one of dependency/config/api_replacement/refactor/other.
Return only one JSON object matching this Plan schema: {json.dumps(Plan.model_json_schema())}
"""

PATCH_PROMPT = f"""You are ReForge's Transformation Agent. Replace the deprecated url.parse() call in src/client.js with the
WHATWG URL API (new URL(address).hostname) and drop the now-unused `url` require. Preserve behavior.
Return a JSON TransformationProposal whose patches contain one clean unified diff with a/ and b/ paths whose hunk
context copies the file exactly. Use codemod "none". No Markdown fences.
Target file src/client.js:
{SOURCE}
Schema: {json.dumps(TransformationProposal.model_json_schema())}
"""


def chat(base_url: str, api_key: str, model: str, prompt: str, timeout: int) -> tuple[str, dict[str, int], float]:
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
    request = Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    started = time.monotonic()
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - user-configured provider URL.
        payload = json.loads(response.read().decode("utf-8"))
    elapsed = time.monotonic() - started
    content = payload["choices"][0]["message"].get("content") or ""
    return content, payload.get("usage") or {}, elapsed


def check_plan(text: str) -> str | None:
    try:
        plan = Plan.model_validate(json.loads(StructuredAgent._strip_fence(text)))
        validate_plan(plan)
    except Exception as exc:  # noqa: BLE001 - any failure is a probe failure.
        return f"{type(exc).__name__}: {str(exc)[:160]}"
    return None


def check_patch(text: str) -> str | None:
    try:
        proposal = TransformationProposal.model_validate(json.loads(StructuredAgent._strip_fence(text)))
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:160]}"
    if not proposal.patches:
        return "no patch proposed"
    diff = normalise_unified_diff_hunks("\n".join(patch.unified_diff.rstrip() for patch in proposal.patches) + "\n")
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "src" / "client.js"
        target.parent.mkdir()
        target.write_text(SOURCE, encoding="utf-8", newline="\n")
        subprocess.run(["git", "init", "-q"], cwd=directory, check=True)
        checked = subprocess.run(["git", "apply", "--check", "-"], cwd=directory, input=diff.encode(), capture_output=True)
        if checked.returncode != 0:
            return f"git apply: {checked.stderr.decode(errors='replace').strip()[:160]}"
        subprocess.run(["git", "apply", "-"], cwd=directory, input=diff.encode(), check=True)
        patched = target.read_text(encoding="utf-8")
    if "url.parse" in patched or "new URL(" not in patched:
        return "patch applied but did not replace url.parse with new URL()"
    return None


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="+", default=FREE_MODELS)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL") or "https://ollama.com/v1")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    api_key = os.getenv("OLLAMA_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Set OLLAMA_API_KEY (or OPENAI_API_KEY) in .env first.")
        return 2

    results = []
    for model in args.models:
        record = {"model": model, "passed": 0, "attempts": 0, "seconds": 0.0, "tokens": 0, "failures": []}
        for round_index in range(args.rounds):
            for task, prompt, check in (("plan", PLAN_PROMPT, check_plan), ("patch", PATCH_PROMPT, check_patch)):
                record["attempts"] += 1
                try:
                    text, usage, elapsed = chat(args.base_url, api_key, model, prompt, args.timeout)
                except (HTTPError, URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                    detail = exc.read().decode(errors="replace")[:160] if isinstance(exc, HTTPError) else str(exc)[:160]
                    record["failures"].append(f"{task}#{round_index + 1} request: {type(exc).__name__} {detail}")
                    continue
                record["seconds"] += elapsed
                record["tokens"] += int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
                failure = check(text)
                if failure:
                    record["failures"].append(f"{task}#{round_index + 1}: {failure}")
                else:
                    record["passed"] += 1
                print(f"{model:22} {task:5} #{round_index + 1}: {'PASS' if not failure else 'FAIL'} {elapsed:5.1f}s", flush=True)
        results.append(record)

    print("\nmodel                  pass   avg s/call  tokens   ")
    ranked = sorted(results, key=lambda item: (-item["passed"] / max(item["attempts"], 1), item["seconds"]))
    for item in ranked:
        calls = max(item["attempts"] - sum("request" in failure for failure in item["failures"]), 1)
        print(f"{item['model']:22} {item['passed']}/{item['attempts']}   {item['seconds'] / calls:9.1f}  {item['tokens']:>7}")
        for failure in item["failures"]:
            print(f"    - {failure}")
    output = ROOT / "runs" / f"model-probe-{int(time.time())}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(ranked, indent=2), encoding="utf-8")
    print(f"\nRecommended: {ranked[0]['model']}  (details: {output})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Chinook continuous evaluation loop

A working NL→SQL evaluation loop using **NVIDIA Nemotron 3 Super** (`nvidia/nemotron-3-super-120b-a12b`). The agent is intentionally small; the project focuses on detecting regressions, preserving evidence and turning failures into permanent cases.

## Run in ten minutes

Requires Python 3.12+ and an NVIDIA API key. Runtime and tests have **no third-party dependencies**. Commands run from the repository root on macOS/Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
export PYTHONPATH=src
export NVIDIA_API_KEY='your-key'  # or set securely via your shell/secret manager
python -m eval_loop fetch-data --require-pin
python -m unittest discover -s tests -v
python -m eval_loop validate
python -m eval_loop run --config configs/nemotron-v3.json --id my-first-run
python -m eval_loop compare --baseline runs/baseline-promoted --candidate runs/my-first-run
python -m eval_loop report
open reports/index.html          # macOS; otherwise open this file in your browser
```

Use a new `--id` for each run; existing evidence is never overwritten. A live run makes about 38 calls (15 agents, 15 judges, 8 calibration examples), paced at 30 requests/minute with four workers. Expect roughly 1–3 minutes plus any retries. Do not run multiple processes against the same low-quota key: pacing is per process. `make test`, `make eval`, `make report` are shortcuts.

Recorded final verification: **15/15 cases**, **2/2 refusals**, **8/8 judge labels**; the independent live repeat also passed all 15 cases. The automated suite contains 14 tests. The identical-config repeat exposed p95 latency jitter (3.14s → 7.53s), so the relative latency gate uses a documented 10s minimum ceiling; quality gates were unchanged.

No API key? All tests and the committed report work offline after fetching the public database. `make demo` verifies that the saved regression is rejected without making API calls. Missing live credentials cause a hard failure, never a pretend pass. Keys are read from the environment only; `.env` is not automatically loaded.

## What is implemented

- 15 authored cases, easy through hard, including **two refusals**. Each includes gold SQL/refusal, ordering, category, difficulty and rationale. The original 14-case suite is frozen in `evals/cases-v1.json`.
- Execution accuracy on pinned Chinook **and deterministic counterexample data**: duplicate-preserving row comparison, NULLs, date boundaries, outer/anti joins, historical prices, aggregation and ties.
- A separate model grader measured against eight fixed authored labels, with agreement and TP/TN/FP/FN/error counts.
- Bounded concurrent execution, retries, pacing, token/cost budgets, read-only SQL, query timeout and result limits.
- Immutable JSONL traces plus JSON run summaries and a disposable SQLite summary index. Every trace records prompts, response, model, request ID, usage, finish reason and timing when available.
- Paired baseline diff, safety/quality/latency/token gates, portable HTML history and trace drill-down.
- Implemented capture→review→promote workflow; the active suite contains a case promoted from an actual failed live run.
- GitHub Actions on pushes, PRs and daily at **03:17 UTC**, with run/report artifacts even when the gate fails.

## Evidence and gate

Open `reports/index.html` for the recorded live runs and `runs/regression/comparison.json` for the machine-readable diff. `runs/baseline` and `runs/regression` use the same original 14-case suite. The deliberate change reduces the agent output cap from 2048 to 32 tokens, simulating an unsafe cost optimization. The shipping configuration restores the output cap and improves inclusive timestamp handling; `runs/baseline-promoted` evaluates the expanded 15-case suite with the strengthened fixture.

```sh
# Expected exit status: 1 (a detected regression)
python -m eval_loop compare --baseline runs/baseline --candidate runs/regression
# Expected exit status: 0; reproduces the saved shipping baseline gate
python -m eval_loop compare --baseline runs/baseline-promoted --candidate runs/baseline-promoted
```

Gate thresholds live in `configs/gates.json`: zero paired regressions or pass-rate drop, ≥90% absolute pass rate, 100% refusals, ≥85% judge-label agreement, zero errors, p95 agent latency ≤60s and ≤max(10s, 2× baseline), total tokens ≤1.75× baseline. Changes to suites, labels, database or fixture version require an explicit baseline refresh. A red run cannot become green simply by deleting a difficult test. Exit codes: `0` success, `1` regression, `2` invalid input/infrastructure/setup error. Individual provider errors are saved in complete runs and rejected by the subsequent gate.

## Turn a failure into a test

The committed example is `failures/artist-count.json`; provenance in the active suite points to its failed run and original case. Promotion requires an explicitly supplied, executable oracle and reviewer identity. It never asks the model to label itself.

To try the workflow without modifying the active suite:

```sh
cp evals/cases-v1.json /tmp/eval-cases.json
python -m eval_loop capture --run runs/regression --case all-artists --output /tmp/artist-failure.json
python -m eval_loop promote --draft /tmp/artist-failure.json \
  --id artists-a-album-count --reviewer 'Your name' \
  --question 'Return ArtistId and album count for every artist whose name starts with A, including artists with zero albums, ordered by ArtistId.' \
  --expected-sql "SELECT a.ArtistId,COUNT(al.AlbumId) FROM Artist a LEFT JOIN Album al ON al.ArtistId=a.ArtistId WHERE a.Name LIKE 'A%' GROUP BY a.ArtistId ORDER BY a.ArtistId" \
  --ordered --cases /tmp/eval-cases.json
python -m eval_loop validate --cases /tmp/eval-cases.json
```

For real promotion omit `--cases` to write the active suite, review its diff, and record a new baseline with a new ID. Explicitly update the CI baseline path only after reviewing its results. The CLI rejects duplicate IDs/questions, passed source cases, invalid SQL and incomplete runs.

## CI setup and provider settings

Push this repository to GitHub and add an Actions secret named `NVIDIA_API_KEY`. Configure the `offline` and `live` jobs as required checks in branch protection. The workflow fails if the secret is absent. Fork PRs run offline tests only; trusted repository pushes and the daily schedule run the live suite. CI uploads each run/report for 90 days, without committing bot-generated history to the main branch. Download historical artifacts under `runs/` and regenerate the report to combine them.

`configs/nemotron-v3.json` is the shipping configuration. Change `base_url`, `api_key_env`, model and provider-specific `extra_body` for another OpenAI-compatible provider. Keep the config version distinct when making changes. Supported API contract: chat completions with JSON text and prompt/completion usage. The runner rejects missing usage and truncated output. NVIDIA model availability is account-dependent: the old Nano ID returned 410 and a listed alias returned 404; the working Super model was verified live. See [NVIDIA’s model page](https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b).

Costs are **unknown**, not $0: NVIDIA’s account-specific token prices were not supplied. Set `input_usd_per_million` and `output_usd_per_million` to record estimated USD and enforce `max_estimated_cost_usd`. The token cap is always enforced, including conservative reservations for uncertain failed requests. Latency excludes local pacing waits (reported separately), but includes provider retries. Failed requests may be billed without reported usage.

## Design, data and limitations

Read [the architecture note](docs/ARCHITECTURE.md) for boundaries, scale to 10,000 cases × 50 versions/week, trade-offs and the two-week plan. Runtime dependency count is zero; optional packaging pins setuptools. CI pins Python 3.12.12; local testing used Python 3.14.3. Seed=42 and temperature=0 reduce variability, but hosted weights, provider seed support, serving kernels and latency remain nondeterministic.

Chinook is downloaded from [release v1.4.5](https://github.com/lerocha/chinook-database/releases/tag/v1.4.5) and verified against `data/manifest.json`; upstream data is MIT-licensed. The downloaded database is not committed. Operational trial runs are retained under `diagnostics/` (deprecated endpoint, rate limiting, an incorrect prompt the model resisted, and a malformed response before the final prompt fix). No benchmark scores or synthetic responses are presented as live runs. Tests use explicitly identified offline doubles.

This is a local/CI reference system, not a production SQL security boundary or a statistically validated benchmark. Static reports and final aggregation are memory-linear; persistent cross-run history, resume, shared rate limiting and independent expert labels remain future work. GitHub publishing and hosted workflow execution require a repository and account access.

# Pentagi SAGE Benchmark Harness

This directory contains the benchmark used to compare pentagi runs with the
SAGE memory wrapper enabled vs. disabled. It exists because PR #253's
maintainer review concluded SAGE's value wasn't measurable; this harness
produces the missing measurements. **It IS the v2 PR's evidence — its job
is to be re-runnable by a skeptical reviewer.**

## What it does

For each cell `(task × model × condition × repetition)` the runner:

1. Stands up the task's docker-compose stack (if any).
2. (sage_on only) Pre-seeds memories into a SAGE node so the agent can
   recall context from a hypothetical prior session.
3. Sets `SAGE_WRAPPER_ENABLED=true|false` in pentagi's environment —
   identical config in every other respect.
4. Calls pentagi's REST API (`POST /api/v1/flows/`) and polls until the
   flow finishes or `timeout_seconds` elapses.
5. Verifies the success criterion, captures completion / turns / wallclock
   / tokens / tool-call count.
6. Tears down the stack, appends one row to `results/<ts>/runs.csv`.

`analyze.py` then computes per-cell mean ± 95 % CI and a paired-bootstrap
delta (10 000 resamples) of completion rate and `tokens_in` per model.
`plot.py` emits side-by-side bar charts.

## Requirements

- Python ≥ 3.10
- Docker (only for tasks that ship a `compose_file`)
- A pentagi backend reachable at `PENTAGI_API` (default
  `http://localhost:8443/api/v1`) with an issued bearer token in
  `PENTAGI_API_TOKEN`
- A SAGE node reachable at `SAGE_BASE_URL` (default
  `http://localhost:7654`) — only required for `sage_on` runs
- Python deps:

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r bench/requirements.txt
```

## Running a sweep

Discover tasks and models, run the smoke path:

```sh
python bench/runner.py --dry-run \
  --tasks demo/example \
  --models example \
  --conditions sage_off \
  --n 1
```

This prints the plan, never touches docker or pentagi, and writes one
placeholder row per cell to `bench/results/<UTC-timestamp>/runs.csv`. Use
this to validate config changes before burning real API budget.

A real sweep:

```sh
export OPENAI_API_KEY=...
export PENTAGI_API_TOKEN=...
python bench/runner.py \
  --tasks 'synthetic/*' \
  --models gpt-4o-mini \
  --conditions sage_on,sage_off \
  --n 5
```

Then summarize:

```sh
python bench/analyze.py --runs bench/results/<ts>/runs.csv
```

This drops `summary.md`, `summary.json`, `summary_cells.csv`,
`summary_deltas.csv`, and three PNGs alongside the runs.csv.

## Adding a task

1. Create `bench/tasks/<group>/<task_name>/`.
2. Drop a `task.yaml` matching the schema in `contracts.md`.
3. (Optional) Add a `docker-compose.yml` next to it if the task has its
   own stack.
4. (Optional, for multi-session evaluation) Populate `seed_memories:` so
   the runner pre-loads prior context into SAGE before sage_on runs.

## Adding a model

1. Drop `bench/models/<name>.yaml` matching the model schema in
   `contracts.md`.
2. The runner will pick it up automatically next sweep.

## Reading results

`summary.md` opens with the headline table — per-model paired-bootstrap
deltas. The interesting rows are:

- `Δ completion-rate (95 % CI)` — does SAGE help the agent finish more
  tasks? Positive means yes.
- `Δ tokens_in (95 % CI)` — net token cost. SAGE injects recall budget;
  but if it reduces thrashing this can come out neutral or negative.

Per-cell tables follow with one row per `(task, model)` for every metric.
The bar charts (`plot_*.png`) show the same data visually.

## Reproducibility

- Every sweep writes a `plan.json` capturing the exact selectors / count /
  versions used.
- `runs.csv` rows include a `run_id`, timestamp, and `dry_run` flag so
  mixed datasets can be filtered.
- The bootstrap RNG is seeded (1729) so the headline deltas are
  bit-identical across reruns of `analyze.py` on the same `runs.csv`.

## Layout

```
bench/
  README.md            - this file
  contracts.md         - YAML schemas (W3 / W4 integration glue)
  requirements.txt     - Python deps
  runner.py            - sweep runner (CLI, click)
  analyze.py           - stats summary
  plot.py              - PNG bar charts
  config_loader.py     - shared task/model YAML loader
  tasks/
    demo/              - demo tasks pointing at upstream pentagi prompts
    synthetic/         - W3 lands docker-compose CTF tasks here
  models/              - W4 lands per-model profiles here
  results/             - sweep outputs; one subdir per run
```

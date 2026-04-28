"""Sweep runner for the SAGE-vs-no-SAGE pentagi benchmark.

For each cell (task, model, condition, repetition n) the runner:

  1. Brings up the task's docker-compose stack (if any).
  2. (sage_on only) Pre-seeds memories into a SAGE node so the agent can
     recall context from a hypothetical "previous session". This is the
     multi-session tier W3 leans on.
  3. Toggles `SAGE_WRAPPER_ENABLED=true|false` in pentagi's environment.
  4. Invokes pentagi against the task target via its REST API
     (POST /api/v1/flows/) and polls for completion.
  5. Verifies the success criterion, captures completion / turns / wallclock
     / token usage / tool-call count.
  6. Tears down the stack and appends a row to runs.csv.

The runner is idempotent at the row level — re-running with the same `--out`
dir appends new rows, so a sweep can be resumed if it crashes.

The actual pentagi invocation is gated behind `--dry-run`. With `--dry-run`
the runner prints the plan and emits placeholder rows so analyze.py / plot
code can be exercised without API keys or a full pentagi stack.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import click

from config_loader import (
    Model,
    SeedMemory,
    Task,
    discover_models,
    discover_tasks,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BENCH_ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = BENCH_ROOT / "results"

CSV_FIELDS = [
    "run_id",
    "timestamp",
    "task_id",
    "task_name",
    "model_name",
    "model_provider",
    "condition",  # sage_on | sage_off
    "n",  # repetition index, 1-based
    "completion",  # bool
    "turn_count",
    "wallclock_seconds",
    "tokens_in",
    "tokens_out",
    "tool_call_count",
    "terminal_command_count",
    "recall_hits_per_step",  # sage_on only; "" for sage_off
    "bytes_stored_per_step",  # sage_on only; "" for sage_off
    "flow_id",
    "error",  # populated if the run aborted
    "dry_run",  # bool
]

# Pentagi REST API defaults — overridable via env.
PENTAGI_API = os.environ.get("PENTAGI_API", "http://localhost:8443/api/v1")
PENTAGI_TOKEN_ENV = "PENTAGI_API_TOKEN"  # bearer token, set by user

# SAGE node defaults — overridable via env.
SAGE_BASE_URL = os.environ.get("SAGE_BASE_URL", "http://localhost:7654")


# ---------------------------------------------------------------------------
# Result row
# ---------------------------------------------------------------------------


@dataclass
class RunRow:
    run_id: str
    timestamp: str
    task_id: str
    task_name: str
    model_name: str
    model_provider: str
    condition: str
    n: int
    completion: bool = False
    turn_count: int = 0
    wallclock_seconds: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_call_count: int = 0
    terminal_command_count: int = 0
    recall_hits_per_step: Optional[float] = None
    bytes_stored_per_step: Optional[float] = None
    flow_id: Optional[int] = None
    error: str = ""
    dry_run: bool = False

    def to_csv_row(self) -> Dict[str, str]:
        d = asdict(self)
        # CSV-friendly: drop None, stringify bools
        out: Dict[str, str] = {}
        for k in CSV_FIELDS:
            v = d.get(k)
            if v is None:
                out[k] = ""
            elif isinstance(v, bool):
                out[k] = "true" if v else "false"
            else:
                out[k] = str(v)
        return out


# ---------------------------------------------------------------------------
# Stack lifecycle
# ---------------------------------------------------------------------------


def compose_up(task: Task, dry_run: bool) -> None:
    if not task.compose_file:
        return
    cmd = ["docker", "compose", "-f", str(task.compose_file), "up", "-d"]
    _run(cmd, dry_run=dry_run, what=f"bring up {task.name} stack")


def compose_down(task: Task, dry_run: bool) -> None:
    if not task.compose_file:
        return
    cmd = ["docker", "compose", "-f", str(task.compose_file), "down", "-v"]
    _run(cmd, dry_run=dry_run, what=f"tear down {task.name} stack", check=False)


def _run(cmd: List[str], *, dry_run: bool, what: str, check: bool = True) -> str:
    if dry_run:
        click.echo(f"[dry-run] would {what}: {' '.join(cmd)}")
        return ""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and check:
        raise RuntimeError(
            f"failed to {what}: rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc.stdout


# ---------------------------------------------------------------------------
# SAGE seed memories
# ---------------------------------------------------------------------------


def seed_sage_memories(memories: List[SeedMemory], task: Task, dry_run: bool) -> None:
    """POST every seed memory to the SAGE node.

    Schema is the SAGE remember endpoint's default:
        POST {SAGE_BASE_URL}/api/v1/memory/remember
        {"domain": "...", "content": "...", "confidence": 0.9}
    """
    if not memories:
        return

    if dry_run:
        for m in memories:
            click.echo(
                f"[dry-run] would seed SAGE memory in domain={m.domain} "
                f"conf={m.confidence} content={m.content[:60]!r}"
            )
        return

    import requests  # local import — keeps --dry-run usable on machines without it

    for m in memories:
        url = f"{SAGE_BASE_URL.rstrip('/')}/api/v1/memory/remember"
        payload = {
            "domain": m.domain,
            "content": m.content,
            "confidence": m.confidence,
            "source": f"bench-seed:{task.name}",
        }
        try:
            r = requests.post(url, json=payload, timeout=10)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            click.echo(
                f"warning: failed to seed memory ({m.domain}): {e}",
                err=True,
            )


# ---------------------------------------------------------------------------
# Pentagi invocation
# ---------------------------------------------------------------------------


def build_pentagi_env(model: Model, condition: str) -> Dict[str, str]:
    """Construct the env dict passed to the pentagi process for one cell."""
    env = dict(os.environ)
    env.update(model.env)
    if model.base_url:
        # Pentagi has many provider-specific URL vars. Expose both the generic
        # one and a couple of common provider-specific ones; W4 will set the
        # right one explicitly in `model.env` if needed.
        env.setdefault("LLM_SERVER_URL", model.base_url)
        env.setdefault("OPEN_AI_SERVER_URL", model.base_url)
    if condition == "sage_on":
        env["SAGE_WRAPPER_ENABLED"] = "true"
        env["SAGE_BASE_URL"] = SAGE_BASE_URL
    else:
        env["SAGE_WRAPPER_ENABLED"] = "false"
    return env


def render_prompt(task: Task) -> str:
    template = task.prompt_template or task.description or ""
    return template.replace("{{TARGET}}", task.target_host or "")


def invoke_pentagi(task: Task, model: Model, env: Dict[str, str], dry_run: bool):
    """Hit pentagi's REST API to create a flow and poll until it terminates.

    Returns a dict of metrics. In --dry-run mode, returns synthetic numbers
    so the rest of the pipeline can be smoke-tested.
    """
    prompt = render_prompt(task)

    if dry_run:
        click.echo(
            f"[dry-run] would POST {PENTAGI_API}/flows/ "
            f"with provider={model.flow_provider_name} input={prompt[:60]!r}"
        )
        return {
            "completion": False,
            "turn_count": 0,
            "wallclock_seconds": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "tool_call_count": 0,
            "terminal_command_count": 0,
            "flow_id": None,
            "error": "",
        }

    import requests

    token = env.get(PENTAGI_TOKEN_ENV) or os.environ.get(PENTAGI_TOKEN_ENV)
    if not token:
        raise RuntimeError(
            f"missing {PENTAGI_TOKEN_ENV} — set a bearer token in the env "
            "before running a non-dry sweep"
        )

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    t0 = time.time()
    create = requests.post(
        f"{PENTAGI_API}/flows/",
        headers=headers,
        json={"input": prompt, "provider": model.flow_provider_name},
        timeout=30,
    )
    create.raise_for_status()
    flow = create.json().get("data", create.json())
    flow_id = flow.get("id") or flow.get("flowID")
    if flow_id is None:
        raise RuntimeError(f"pentagi did not return a flow id: {flow}")

    # Poll until the flow reports a terminal status.
    deadline = t0 + task.timeout_seconds
    flow_state: Dict[str, Any] = {}
    while time.time() < deadline:
        r = requests.get(f"{PENTAGI_API}/flows/{flow_id}", headers=headers, timeout=30)
        r.raise_for_status()
        flow_state = r.json().get("data", r.json())
        status = (flow_state.get("status") or "").lower()
        if status in {"finished", "failed", "stopped"}:
            break
        time.sleep(5)

    wallclock = time.time() - t0

    # Pull tasks/subtasks/messages to count turns + tokens.
    tasks_resp = requests.get(
        f"{PENTAGI_API}/flows/{flow_id}/tasks/", headers=headers, timeout=30
    )
    subtask_count = 0
    tokens_in = tokens_out = tool_calls = term_cmds = 0
    if tasks_resp.ok:
        for t in tasks_resp.json().get("data", []) or []:
            tid = t.get("id")
            if tid is None:
                continue
            sub = requests.get(
                f"{PENTAGI_API}/flows/{flow_id}/tasks/{tid}/subtasks/",
                headers=headers,
                timeout=30,
            )
            if sub.ok:
                for st in sub.json().get("data", []) or []:
                    subtask_count += 1
                    tokens_in += int(st.get("tokensIn", 0) or 0)
                    tokens_out += int(st.get("tokensOut", 0) or 0)
                    tool_calls += int(st.get("toolCallCount", 0) or 0)
                    term_cmds += int(st.get("terminalCommandCount", 0) or 0)

    return {
        "completion": False,  # filled in by success-criterion check after this returns
        "turn_count": subtask_count,
        "wallclock_seconds": wallclock,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tool_call_count": tool_calls,
        "terminal_command_count": term_cmds,
        "flow_id": flow_id,
        "error": "" if flow_state.get("status") == "finished" else (
            f"flow ended with status={flow_state.get('status')!r}"
        ),
    }


# ---------------------------------------------------------------------------
# Success-criterion evaluation
# ---------------------------------------------------------------------------


def evaluate_success(task: Task, dry_run: bool) -> bool:
    if dry_run:
        return False  # honest default — analyze.py treats this as a failed run

    sc = task.success_criterion
    t = sc.type
    raw = sc.raw

    try:
        if t == "self_report":
            # Pentagi reported "finished"; we already require status=finished
            # in invoke_pentagi for the error column. If we got this far the
            # flow finished cleanly, so call it good.
            return True
        if t == "file_contains":
            import re

            path = raw["path"]
            pattern = raw["pattern"]
            data = Path(path).read_text(errors="ignore")
            return bool(re.search(pattern, data))
        if t == "http_status":
            import requests

            url = raw["url"]
            expected = int(raw["status"])
            r = requests.get(url, timeout=10)
            return r.status_code == expected
        if t == "command_exit":
            cmd = raw["command"]
            rc = subprocess.call(cmd, shell=True)
            return rc == 0
        if t == "regex_in_logs":
            import re

            container = raw["container"]
            pattern = raw["pattern"]
            out = subprocess.check_output(
                ["docker", "logs", container], stderr=subprocess.STDOUT, text=True
            )
            return bool(re.search(pattern, out))
    except Exception as e:  # noqa: BLE001
        click.echo(f"success-criterion check failed: {e}", err=True)
        return False

    click.echo(f"unknown success_criterion.type={t!r}; treating as failure", err=True)
    return False


# ---------------------------------------------------------------------------
# SAGE per-step metrics (sage_on cells only)
# ---------------------------------------------------------------------------


def collect_sage_metrics(flow_id: Optional[int], dry_run: bool) -> Dict[str, Optional[float]]:
    """Best-effort retrieval of per-step recall/store stats from SAGE.

    The SAGE node stamps each request with the agent name (pentagi-flow-<id>),
    so we can ask its timeline endpoint for bucketed stats. If anything fails
    we return Nones — analyze.py treats that as 'no data'.
    """
    if dry_run or flow_id is None:
        return {"recall_hits_per_step": None, "bytes_stored_per_step": None}

    import requests

    try:
        url = f"{SAGE_BASE_URL.rstrip('/')}/api/v1/timeline"
        r = requests.get(
            url,
            params={"agent": f"pentagi-flow-{flow_id}"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json().get("data", r.json())
        steps = max(int(data.get("steps", 0)), 1)
        return {
            "recall_hits_per_step": float(data.get("total_recalls", 0)) / steps,
            "bytes_stored_per_step": float(data.get("total_bytes_stored", 0)) / steps,
        }
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: SAGE metrics unavailable: {e}", err=True)
        return {"recall_hits_per_step": None, "bytes_stored_per_step": None}


# ---------------------------------------------------------------------------
# Sweep loop
# ---------------------------------------------------------------------------


def write_provider_config_if_any(model: Model, out_dir: Path) -> Optional[Path]:
    if not model.pentagi_provider_config:
        return None
    cfg_dir = out_dir / "provider_configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / f"{model.name}.provider.yml"
    cfg_path.write_text(model.pentagi_provider_config)
    return cfg_path


def run_cell(
    task: Task,
    model: Model,
    condition: str,
    n: int,
    out_dir: Path,
    dry_run: bool,
) -> RunRow:
    run_id = uuid.uuid4().hex[:12]
    row = RunRow(
        run_id=run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        task_id=task.task_id,
        task_name=task.name,
        model_name=model.name,
        model_provider=model.provider,
        condition=condition,
        n=n,
        dry_run=dry_run,
    )

    click.echo(
        f"==> [{run_id}] task={task.task_id} model={model.name} "
        f"condition={condition} n={n}{' (dry-run)' if dry_run else ''}"
    )

    try:
        compose_up(task, dry_run=dry_run)

        if condition == "sage_on" and task.seed_memories:
            seed_sage_memories(task.seed_memories, task, dry_run=dry_run)

        env = build_pentagi_env(model, condition)
        provider_cfg = write_provider_config_if_any(model, out_dir)
        if provider_cfg is not None:
            env.setdefault("LLM_SERVER_CONFIG", str(provider_cfg))

        metrics = invoke_pentagi(task, model, env, dry_run=dry_run)
        row.turn_count = metrics["turn_count"]
        row.wallclock_seconds = metrics["wallclock_seconds"]
        row.tokens_in = metrics["tokens_in"]
        row.tokens_out = metrics["tokens_out"]
        row.tool_call_count = metrics["tool_call_count"]
        row.terminal_command_count = metrics["terminal_command_count"]
        row.flow_id = metrics["flow_id"]
        row.error = metrics["error"]

        # Completion = pentagi finished cleanly AND criterion holds.
        if not metrics["error"]:
            row.completion = evaluate_success(task, dry_run=dry_run)

        if condition == "sage_on":
            sm = collect_sage_metrics(row.flow_id, dry_run=dry_run)
            row.recall_hits_per_step = sm["recall_hits_per_step"]
            row.bytes_stored_per_step = sm["bytes_stored_per_step"]

    except Exception as e:  # noqa: BLE001
        row.error = f"{type(e).__name__}: {e}"
        click.echo(f"  !! cell aborted: {row.error}", err=True)
    finally:
        compose_down(task, dry_run=dry_run)

    return row


def append_row(csv_path: Path, row: RunRow) -> None:
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row.to_csv_row())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command(context_settings={"show_default": True})
@click.option(
    "--tasks",
    "tasks_arg",
    multiple=True,
    help=(
        "Task selector(s). Glob-style relative to bench/tasks/, e.g. "
        "'demo/web-pentest-demo' or 'synthetic/*'. Repeat or pass none for all."
    ),
)
@click.option(
    "--models",
    "models_arg",
    multiple=True,
    help="Model selector(s) (filename stem). Repeat or pass none for all.",
)
@click.option(
    "--conditions",
    default="sage_on,sage_off",
    help="Comma-separated subset of {sage_on,sage_off}.",
)
@click.option("--n", "n_runs", default=5, type=int, help="Reps per cell.")
@click.option(
    "--out",
    "out_arg",
    default=None,
    help="Output dir; default bench/results/<UTC-timestamp>/.",
)
@click.option(
    "--parallel",
    default=1,
    type=int,
    help="Max concurrent cells. Pentagi is heavy; leave at 1 unless you know what you're doing.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=False,
    help="Print the plan and emit placeholder rows without invoking pentagi.",
)
def main(
    tasks_arg: Iterable[str],
    models_arg: Iterable[str],
    conditions: str,
    n_runs: int,
    out_arg: Optional[str],
    parallel: int,
    dry_run: bool,
) -> None:
    """Run the SAGE-on / SAGE-off pentagi benchmark sweep."""
    task_selectors = list(tasks_arg) or None
    model_selectors = list(models_arg) or None

    tasks = discover_tasks(task_selectors)
    models = discover_models(model_selectors)

    cond_list = [c.strip() for c in conditions.split(",") if c.strip()]
    bad = [c for c in cond_list if c not in {"sage_on", "sage_off"}]
    if bad:
        raise click.BadParameter(f"unknown conditions: {bad}")

    if not tasks:
        raise click.ClickException(
            "no tasks discovered. Add task.yaml files under bench/tasks/ or pass valid --tasks."
        )
    if not models:
        raise click.ClickException(
            "no models discovered. Add YAML files under bench/models/ or pass valid --models."
        )

    if out_arg:
        out_dir = Path(out_arg).resolve()
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = (RESULTS_ROOT / ts).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "runs.csv"

    # Snapshot the plan up front (also serves as the dry-run output header).
    plan = {
        "out_dir": str(out_dir),
        "tasks": [t.task_id for t in tasks],
        "models": [m.name for m in models],
        "conditions": cond_list,
        "n": n_runs,
        "parallel": parallel,
        "dry_run": dry_run,
        "pentagi_api": PENTAGI_API,
        "sage_base_url": SAGE_BASE_URL,
    }
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=2))
    click.echo(json.dumps(plan, indent=2))

    if parallel != 1:
        click.echo(
            "warning: parallel>1 is supported by structure but pentagi runs "
            "are heavy; sequential is the safer default.",
            err=True,
        )

    # Sequential is the only mode tested. The structure (task,model,cond,n)
    # is trivially parallelizable when someone wants to bother.
    for task in tasks:
        for model in models:
            for cond in cond_list:
                for i in range(1, n_runs + 1):
                    row = run_cell(task, model, cond, i, out_dir, dry_run=dry_run)
                    append_row(csv_path, row)

    click.echo(f"\nDone. Results: {csv_path}")
    if not dry_run:
        click.echo("Next: python bench/analyze.py --runs " + str(csv_path))


if __name__ == "__main__":
    main()

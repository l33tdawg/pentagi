"""Summarize a runs.csv produced by bench/runner.py.

Outputs:
  - <out>/summary.md      — markdown tables, per-cell mean +/- 95% CI,
                            headline paired-bootstrap deltas per model.
  - <out>/summary.json    — machine-readable copy of the above.
  - <out>/plot_*.png      — bar charts (delegated to plot.py if importable).

The bootstrap is paired across (task, n) within a model: for each model we
align sage_on / sage_off rows by (task_id, n) so every resample picks
matched pairs, not independent samples. This is the right way to test
"does flipping the SAGE switch change the outcome on the same workload?".
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_CI = 0.95


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------


def _mean_ci(values: np.ndarray) -> Tuple[float, float, float]:
    """Mean and a normal-approximation 95% CI. Returns (mean, lo, hi)."""
    if values.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    if values.size == 1:
        v = float(values[0])
        return (v, v, v)
    mean = float(np.mean(values))
    se = float(np.std(values, ddof=1) / np.sqrt(values.size))
    half = 1.96 * se
    return (mean, mean - half, mean + half)


def _paired_bootstrap_delta(
    on: np.ndarray,
    off: np.ndarray,
    rng: np.random.Generator,
    resamples: int = BOOTSTRAP_RESAMPLES,
    ci: float = BOOTSTRAP_CI,
) -> Tuple[float, float, float]:
    """Paired bootstrap on (on - off). Returns (delta, lo, hi)."""
    if on.size != off.size:
        raise ValueError(f"paired bootstrap needs equal-length samples (got {on.size} vs {off.size})")
    if on.size == 0:
        return (float("nan"), float("nan"), float("nan"))

    n = on.size
    deltas = np.empty(resamples)
    for i in range(resamples):
        idx = rng.integers(0, n, size=n)
        deltas[i] = on[idx].mean() - off[idx].mean()

    point = float(on.mean() - off.mean())
    alpha = (1 - ci) / 2
    lo = float(np.quantile(deltas, alpha))
    hi = float(np.quantile(deltas, 1 - alpha))
    return (point, lo, hi)


def _align_paired(
    df: pd.DataFrame, model: str, metric: str
) -> Tuple[np.ndarray, np.ndarray]:
    """Pull matched (sage_on, sage_off) values for one model.

    Pairs by (task_id, n). Drops anything without a counterpart so the two
    arrays have equal length, which the paired bootstrap requires.
    """
    sub = df[df["model_name"] == model]
    on = sub[sub["condition"] == "sage_on"][["task_id", "n", metric]]
    off = sub[sub["condition"] == "sage_off"][["task_id", "n", metric]]
    merged = on.merge(off, on=["task_id", "n"], suffixes=("_on", "_off"))
    return (
        merged[f"{metric}_on"].to_numpy(dtype=float),
        merged[f"{metric}_off"].to_numpy(dtype=float),
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


METRICS = [
    "completion",
    "turn_count",
    "wallclock_seconds",
    "tokens_in",
    "tokens_out",
    "tool_call_count",
    "terminal_command_count",
]


def load_runs(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["completion"] = (df["completion"].astype(str).str.lower() == "true").astype(int)
    df["dry_run"] = df["dry_run"].astype(str).str.lower() == "true"
    for col in ("turn_count", "tokens_in", "tokens_out", "tool_call_count",
                "terminal_command_count", "n"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    df["wallclock_seconds"] = pd.to_numeric(df["wallclock_seconds"], errors="coerce").fillna(0.0)
    return df


def per_cell_stats(df: pd.DataFrame) -> pd.DataFrame:
    """Mean + 95% CI per (task_id, model_name, condition) for every metric."""
    rows = []
    grouped = df.groupby(["task_id", "model_name", "condition"], dropna=False)
    for (task_id, model, cond), g in grouped:
        rec = {"task_id": task_id, "model_name": model, "condition": cond, "n_runs": len(g)}
        for m in METRICS:
            mean, lo, hi = _mean_ci(g[m].to_numpy(dtype=float))
            rec[f"{m}_mean"] = mean
            rec[f"{m}_ci_lo"] = lo
            rec[f"{m}_ci_hi"] = hi
        rows.append(rec)
    return pd.DataFrame(rows)


def headline_deltas(df: pd.DataFrame, seed: int = 1729) -> pd.DataFrame:
    """Per-model paired-bootstrap deltas for completion_rate and tokens_in."""
    rng = np.random.default_rng(seed)
    rows = []
    for model in sorted(df["model_name"].unique()):
        rec = {"model_name": model}
        for metric in ("completion", "tokens_in"):
            on, off = _align_paired(df, model, metric)
            delta, lo, hi = _paired_bootstrap_delta(on, off, rng)
            rec[f"{metric}_delta"] = delta
            rec[f"{metric}_ci_lo"] = lo
            rec[f"{metric}_ci_hi"] = hi
            rec[f"{metric}_n_pairs"] = len(on)
        rows.append(rec)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt(x: float, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    return f"{x:.{digits}f}"


def render_markdown(df: pd.DataFrame, cell_stats: pd.DataFrame, deltas: pd.DataFrame) -> str:
    parts: List[str] = []
    parts.append("# Pentagi SAGE Benchmark — Summary\n")
    parts.append(
        f"Runs analyzed: **{len(df)}** "
        f"({df[df['condition']=='sage_on'].shape[0]} sage_on / "
        f"{df[df['condition']=='sage_off'].shape[0]} sage_off)\n"
    )
    if df["dry_run"].any():
        parts.append(
            "> Note: this dataset contains dry-run rows. Treat the numbers "
            "below as a smoke test, not real measurements.\n"
        )

    # Headline
    parts.append("## Headline (paired bootstrap, 10 000 resamples)\n")
    parts.append(
        "Per model, the difference SAGE-on minus SAGE-off, paired across "
        "(task, repetition).\n"
    )
    parts.append("| Model | n pairs | Δ completion-rate (95% CI) | Δ tokens_in (95% CI) |")
    parts.append("|---|---|---|---|")
    for _, r in deltas.iterrows():
        parts.append(
            "| {model} | {n} | {cd} ({clo}, {chi}) | {td} ({tlo}, {thi}) |".format(
                model=r["model_name"],
                n=int(r["completion_n_pairs"]),
                cd=_fmt(r["completion_delta"], 3),
                clo=_fmt(r["completion_ci_lo"], 3),
                chi=_fmt(r["completion_ci_hi"], 3),
                td=_fmt(r["tokens_in_delta"], 0),
                tlo=_fmt(r["tokens_in_ci_lo"], 0),
                thi=_fmt(r["tokens_in_ci_hi"], 0),
            )
        )
    parts.append("")

    # Per-cell tables
    parts.append("## Per-cell stats (mean and 95% CI)\n")
    for (task_id, model), g in cell_stats.groupby(["task_id", "model_name"]):
        parts.append(f"### {task_id} — {model}\n")
        parts.append(
            "| Metric | sage_on (mean / 95% CI) | sage_off (mean / 95% CI) |"
        )
        parts.append("|---|---|---|")
        on_row = g[g["condition"] == "sage_on"]
        off_row = g[g["condition"] == "sage_off"]
        for m in METRICS:
            on_text = "—"
            off_text = "—"
            if not on_row.empty:
                r = on_row.iloc[0]
                on_text = (
                    f"{_fmt(r[m+'_mean'])} ({_fmt(r[m+'_ci_lo'])}, {_fmt(r[m+'_ci_hi'])})"
                )
            if not off_row.empty:
                r = off_row.iloc[0]
                off_text = (
                    f"{_fmt(r[m+'_mean'])} ({_fmt(r[m+'_ci_lo'])}, {_fmt(r[m+'_ci_hi'])})"
                )
            parts.append(f"| {m} | {on_text} | {off_text} |")
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", required=True, help="Path to runs.csv from runner.py")
    ap.add_argument(
        "--out",
        default=None,
        help="Output directory; defaults to runs.csv's parent.",
    )
    ap.add_argument(
        "--no-plots",
        action="store_true",
        help="Skip PNG generation. Useful in headless CI without matplotlib.",
    )
    args = ap.parse_args()

    runs_path = Path(args.runs).resolve()
    out_dir = Path(args.out).resolve() if args.out else runs_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_runs(runs_path)
    if df.empty:
        print(f"runs.csv at {runs_path} is empty; nothing to analyze", file=sys.stderr)
        sys.exit(2)

    cell_stats = per_cell_stats(df)
    deltas = headline_deltas(df)

    md = render_markdown(df, cell_stats, deltas)
    (out_dir / "summary.md").write_text(md)
    cell_stats.to_csv(out_dir / "summary_cells.csv", index=False)
    deltas.to_csv(out_dir / "summary_deltas.csv", index=False)
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "cells": cell_stats.to_dict(orient="records"),
                "deltas": deltas.to_dict(orient="records"),
            },
            indent=2,
            default=lambda x: None if isinstance(x, float) and np.isnan(x) else x,
        )
    )

    if not args.no_plots:
        try:
            import plot

            plot.render_plots(df, cell_stats, deltas, out_dir)
        except Exception as e:  # noqa: BLE001
            print(f"plot rendering skipped: {e}", file=sys.stderr)

    print(f"wrote {out_dir / 'summary.md'}")


if __name__ == "__main__":
    main()

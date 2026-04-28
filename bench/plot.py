"""PNG bar-chart generation for the bench summary.

Imported by analyze.py; can also be run standalone:
    python plot.py --runs <results>/runs.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

# Lazy matplotlib import so analyze.py can `--no-plots` on machines without it.
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def _grouped_bar(
    df: pd.DataFrame, metric_mean: str, ylabel: str, title: str, out_path: Path
) -> None:
    """One bar chart: x=model, two bars per group (sage_on / sage_off)."""
    pivot = df.pivot_table(
        index="model_name",
        columns="condition",
        values=metric_mean,
        aggfunc="mean",
    ).fillna(0.0)

    models = list(pivot.index)
    if not models:
        return

    x = np.arange(len(models))
    width = 0.38

    fig, ax = plt.subplots(figsize=(max(6, 1.8 * len(models)), 4.5))
    on_vals = pivot.get("sage_on", pd.Series([0] * len(models), index=models))
    off_vals = pivot.get("sage_off", pd.Series([0] * len(models), index=models))

    ax.bar(x - width / 2, on_vals.values, width, label="sage_on")
    ax.bar(x + width / 2, off_vals.values, width, label="sage_off")
    ax.set_xticks(x)
    ax.set_xticklabels(models, rotation=20, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def render_plots(
    df: pd.DataFrame,
    cell_stats: pd.DataFrame,
    deltas: pd.DataFrame,
    out_dir: Path,
) -> None:
    if cell_stats.empty:
        return

    _grouped_bar(
        cell_stats,
        "completion_mean",
        "Completion rate",
        "Completion rate by model (averaged across tasks)",
        out_dir / "plot_completion.png",
    )
    _grouped_bar(
        cell_stats,
        "tokens_in_mean",
        "Tokens in (mean per run)",
        "Token-cost by model (sage_on vs sage_off)",
        out_dir / "plot_tokens.png",
    )
    _grouped_bar(
        cell_stats,
        "turn_count_mean",
        "Turns (mean per run)",
        "Turn count by model (sage_on vs sage_off)",
        out_dir / "plot_turns.png",
    )


def _standalone() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from analyze import headline_deltas, load_runs, per_cell_stats

    runs_path = Path(args.runs).resolve()
    out_dir = Path(args.out).resolve() if args.out else runs_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_runs(runs_path)
    cell = per_cell_stats(df)
    delt = headline_deltas(df)
    render_plots(df, cell, delt, out_dir)
    print(f"plots written to {out_dir}")


if __name__ == "__main__":
    _standalone()

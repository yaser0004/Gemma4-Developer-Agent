"""Compare agent variants run on the same tasks (results dirs written by swegemma's Evaluator.run).

Usage: python scripts/compare_arms.py <baseline_dir> <other_dir> [<other_dir> ...] [--tasks N_SCORER=120]

Prints per variant: resolved, how many ended by an error/time cap, the duration distribution, and the
projected scorer wall time (sequential, N_SCORER tasks drawn like the local sample, divided by the
concurrency slowdown given via --slowdown). Against the baseline it prints the paired win/loss table.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def load(run_dir: Path) -> dict[str, dict]:
    rows = {}
    for line in (run_dir / "task_results.jsonl").read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["instance_id"]] = row
    return rows


def describe(name: str, rows: dict[str, dict], n_scorer: int, slowdown: float, setup_verify_s: float) -> None:
    secs = sorted(r["duration_seconds"] for r in rows.values())
    capped = sum(1 for r in rows.values() if "exceeded" in (r.get("error") or ""))
    crashed = sum(1 for r in rows.values() if (r.get("error") or "") and "exceeded" not in r["error"])
    no_patch = sum(1 for r in rows.values() if not r.get("agent_patch_size"))
    mean_agent = statistics.mean(secs) / slowdown if secs else 0.0
    projected_h = n_scorer * (mean_agent + setup_verify_s) / 3600
    pct = lambda q: secs[min(len(secs) - 1, int(q * len(secs)))] if secs else 0  # noqa: E731
    print(f"{name:20s} resolved {sum(r['resolved'] for r in rows.values()):3d}/{len(rows)}  "
          f"capped {capped}  crashed {crashed}  no_patch {no_patch}  "
          f"tool_calls mean {statistics.mean(r['tool_calls'] for r in rows.values()):.1f}  "
          f"sec p50 {pct(.5):.0f} p90 {pct(.9):.0f}  projected scorer {projected_h:.1f} h")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dirs", nargs="+", type=Path)
    parser.add_argument("--n-scorer", type=int, default=120)
    parser.add_argument("--slowdown", type=float, default=1.0, help="concurrency slowdown factor (calibration)")
    parser.add_argument("--setup-verify-s", type=float, default=40.0, help="per-task setup + verification seconds")
    args = parser.parse_args()
    runs = {d.name: load(d) for d in args.dirs}
    for name, rows in runs.items():
        describe(name, rows, args.n_scorer, args.slowdown, args.setup_verify_s)
    base_name, base = next(iter(runs.items()))
    for name, rows in list(runs.items())[1:]:
        shared = sorted(set(base) & set(rows))
        wins = [i for i in shared if rows[i]["resolved"] and not base[i]["resolved"]]
        losses = [i for i in shared if base[i]["resolved"] and not rows[i]["resolved"]]
        print(f"{name} vs {base_name}: +{len(wins)} -{len(losses)} net {len(wins) - len(losses):+d} on {len(shared)} shared tasks")


if __name__ == "__main__":
    main()

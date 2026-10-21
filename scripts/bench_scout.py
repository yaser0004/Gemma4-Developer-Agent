"""Offline benchmark for the scout skill: does it put the gold file in front of the agent?

For each task with a local snapshot, extracts the snapshot's Python files into a scratch dir and
runs scout.py exactly as the skill runner does (from another cwd, repo reachable via $PWD), with the
whole problem statement as its argument. The agent passes a curated term list instead, so these
numbers are a noisy lower bound, but they need no LLM and no GPU.

Reports gold-file recall@1/@3/@5 among scout's ranked source files, output size and run time, per
repo and per specificity label. A task counts only if its gold patch changes a non-test .py file.

Usage: python scripts/bench_scout.py --data ../RESOURCES/competition_data --work ../ai/scratch/bench_scout
           [--labels ../ai/analysis/task_specificity.jsonl] [--scout variants/v15_scout/...] [--only ID ...]
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_SCOUT = REPO / "variants" / "v15_scout" / "skills" / "repo-tools" / "scripts" / "scout.py"
TEST_PATH = re.compile(r"(^|/)(tests?/|testing/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")


def gold_files(patch: str) -> set[str]:
    return {f for f in re.findall(r"^--- a/(\S+)", patch, re.M) if f.endswith(".py") and not TEST_PATH.search(f)}


def extract_python(snapshot: Path, dest: Path) -> None:
    # Only .py files: scout greps nothing else, and fastapi snapshots are ~200 MB each.
    with tarfile.open(snapshot) as tar:
        members = [m for m in tar.getmembers() if m.isfile() and m.name.endswith(".py")]
        tar.extractall(dest, members=members, filter="data")


def ranked_files(output: str) -> list[str]:
    files, inside = [], False
    for line in output.splitlines():
        if line.startswith("Best source files"):
            inside = True
        elif inside and line.startswith("  "):
            files.append(line.split()[0])
        elif inside:
            break
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, required=True, help="competition data dir (tasks.jsonl, snapshots/)")
    ap.add_argument("--work", type=Path, required=True, help="scratch dir for extracted snapshots (outside the repo)")
    ap.add_argument("--labels", type=Path)
    ap.add_argument("--scout", type=Path, default=DEFAULT_SCOUT)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--jsonl", type=Path, help="write one result row per task here")
    args = ap.parse_args()
    args.work = args.work.resolve()  # scout resolves the repo from $PWD, which must be absolute

    labels = {}
    if args.labels:
        for line in args.labels.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                labels[row["instance_id"]] = row["label"]
    tasks = [json.loads(line) for line in (args.data / "tasks.jsonl").read_text().splitlines() if line.strip()]
    rows = []
    for task in tasks:
        iid = task["instance_id"]
        snapshot = args.data / "snapshots" / f"{iid}.tgz"
        gold = gold_files(task["patch"])
        if not snapshot.is_file() or not gold or (args.only and iid not in args.only):
            continue
        dest = args.work / iid
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        try:
            extract_python(snapshot, dest)
            env = {"PATH": os.environ["PATH"], "PWD": str(dest), "HOME": str(args.work)}
            t0 = time.perf_counter()
            res = subprocess.run([sys.executable, "-I", str(args.scout), task["problem_statement"]],
                                 cwd=args.work, env=env, capture_output=True, text=True, timeout=120)
            secs = time.perf_counter() - t0
        finally:
            shutil.rmtree(dest, ignore_errors=True)
        ranked = ranked_files(res.stdout)
        hit = next((i for i, f in enumerate(ranked) if f in gold), None)
        row = {"instance_id": iid, "repo": iid.rsplit("_", 1)[0], "label": labels.get(iid, "-"),
               "rank": hit, "chars": len(res.stdout), "seconds": round(secs, 2),
               "error": res.stderr[-300:] if res.returncode else None}
        rows.append(row)
        print(f"{iid:20s} rank {hit if hit is not None else '-':>2}  {row['chars']:5d} chars  {secs:5.2f} s"
              + (f"  ERROR {row['error']}" if row["error"] else ""), flush=True)

    if args.jsonl:
        args.jsonl.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def summary(name: str, group: list[dict]) -> None:
        n = len(group)
        at = lambda k: sum(1 for r in group if r["rank"] is not None and r["rank"] < k) / n  # noqa: E731
        secs = sorted(r["seconds"] for r in group)
        chars = sorted(r["chars"] for r in group)
        print(f"{name:24s} n={n:3d}  recall@1 {at(1):.0%}  @3 {at(3):.0%}  @5 {at(5):.0%}  "
              f"p95 {secs[int(.95 * (n - 1))]:.2f} s  chars p50 {chars[n // 2]} max {chars[-1]}")

    if not rows:
        print("no tasks benchmarked")
        return 1
    print()
    summary("all", rows)
    for key in ("repo", "label"):
        groups = collections.defaultdict(list)
        for r in rows:
            groups[r[key]].append(r)
        for name, group in sorted(groups.items()):
            summary(f"{key}={name}", group)
    return 0


if __name__ == "__main__":
    sys.exit(main())

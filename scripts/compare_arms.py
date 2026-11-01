"""Compare agent variants run on the same tasks (results dirs written by swegemma's Evaluator.run).

Usage: python scripts/compare_arms.py <baseline_dir> <other_dir> [<other_dir> ...] [--tasks N_SCORER=120]

Prints per variant: resolved, how many ended by an error/time cap, the duration distribution, and the
projected scorer wall time (sequential, N_SCORER tasks drawn like the local sample, divided by the
concurrency slowdown given via --slowdown). Against the baseline it prints the paired win/loss table,
overall and per repo.

With --tasks (the competition tasks.jsonl) it also prints a failure taxonomy per variant, read from the
saved patches and traces:
- buckets: resolved / no_patch / gold_file (patch touches a file the gold patch touches, tests still fail)
  / wrong_file, crossed with the task label from --labels (task_specificity.jsonl) when given;
- thought volume per LLM call (the "(N chars)" counts in logs/*.log), the dominant wall-time cost;
- compactions: prompt-token drops of more than 20% between consecutive LLM calls in a trace;
- the tool-call index of the first source edit (edit_file/write_file);
- safety counters: tool calls the parser mangled ("mandatory input parameters are not present"),
  identical consecutive retries, run_skill_script calls with list-form args (these can hang the
  scorer's vLLM tool parser for the rest of a run), and stalls (gaps over 400 s between steps);
- triage compliance: short-statement tasks (see short_statement) that ended within 8 tool calls.

--caps 450,540,600 replays each run at shorter time caps (seconds of run time; a run counts as
resolved only if it finished within the cap) and projects scorer hours twice: expected (all tasks)
and worst case (only tasks with a non-short statement, i.e. a hidden set where triage never fires).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from collections import Counter
import statistics
from pathlib import Path


STALL_GAP_S = 400  # must exceed timeout_seconds: a legitimate run_command can take 240-300 s
TRIAGE_MAX_CALLS = 8
_SHORT_NOISE = (re.compile(r"<!--.*?-->", re.S), re.compile(r"https?://\S+"),
                re.compile(r"(?im)^\s*[-*]?\s*(fixes|closes|resolves)\b.*$"))


def short_statement(text: str, limit: int = 80) -> bool:
    """The prompt's triage rule: almost nothing left once links, HTML comments and Fixes lines go."""
    for pattern in _SHORT_NOISE:
        text = pattern.sub("", text)
    return len(text.strip()) < limit


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


def _literal(value):
    # Trace fields are stored as Python reprs of dicts/lists (ATIF export), not JSON.
    if isinstance(value, str):
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return None
    return value


def patched_files(patch: str) -> set[str]:
    return set(re.findall(r"^--- a/(\S+)", patch, re.M))


def trace_stats(trace: dict) -> tuple[int, int | None]:
    """Return (compactions, tool-call index of the first edit) for one trace."""
    compactions, first_edit, calls, prev_prompt = 0, None, 0, None
    for step in trace.get("steps", []):
        metrics = _literal(step.get("metrics"))
        if metrics:
            prompt = metrics.get("prompt_tokens", 0)
            if prev_prompt and prompt < 0.8 * prev_prompt:
                compactions += 1
            prev_prompt = prompt
        for call in _literal(step.get("tool_calls")) or []:
            calls += 1
            if first_edit is None and call.get("function_name") in ("edit_file", "write_file"):
                first_edit = calls
    return compactions, first_edit


def safety_stats(trace: dict) -> Counter:
    """Count parser-mangled calls, identical retries, list-form skill args and stalls in one trace."""
    stats: Counter = Counter()
    prev_call, prev_elapsed = None, None
    for step in trace.get("steps", []):
        if "mandatory input parameters are not present" in str(step.get("observation", "")):
            stats["rejected"] += 1
        extra = _literal(step.get("extra")) or {}
        elapsed = extra.get("elapsed_s") if isinstance(extra, dict) else None
        if isinstance(elapsed, (int, float)):
            if prev_elapsed is not None and elapsed - prev_elapsed > STALL_GAP_S:
                stats["stalls"] += 1
            prev_elapsed = elapsed
        for call in _literal(step.get("tool_calls")) or []:
            args = call.get("arguments") or {}
            key = (call.get("function_name"), repr(args))
            if key == prev_call:
                stats["identical_retries"] += 1
            prev_call = key
            if call.get("function_name") == "run_skill_script" and isinstance(args, dict) and (
                    isinstance(args.get("args"), list) or args.get("positional_args")):
                stats["list_skill_args"] += 1
    return stats


def project(rows: dict[str, dict], cap: float, slowdown: float, setup_verify_s: float,
            n_scorer: int) -> tuple[int, float]:
    """Resolved within `cap` run-seconds and projected scorer hours if every run stopped there."""
    if not rows:
        return 0, 0.0
    resolved = sum(1 for r in rows.values() if r["resolved"] and r["duration_seconds"] <= cap)
    mean_s = statistics.mean(min(r["duration_seconds"], cap) for r in rows.values()) / slowdown
    return resolved, n_scorer * (mean_s + setup_verify_s) / 3600


def diagnose(name: str, run_dir: Path, rows: dict[str, dict], gold: dict[str, set[str]],
             labels: dict[str, str], short_ids: set[str]) -> None:
    buckets: Counter = Counter()
    safety: Counter = Counter()
    compactions, first_edits = 0, []
    for iid, row in rows.items():
        patch_path = run_dir / "patches" / f"{iid}.patch"
        patch = patch_path.read_text(errors="replace") if patch_path.exists() else ""
        if row["resolved"]:
            bucket = "resolved"
        elif not patch.strip():
            bucket = "no_patch"
        elif patched_files(patch) & gold.get(iid, set()):
            bucket = "gold_file"
        else:
            bucket = "wrong_file"
        buckets[(bucket, labels.get(iid, "-"))] += 1
        trace_path = run_dir / "traces" / f"trace_{iid}.json"
        if trace_path.exists():
            trace = json.loads(trace_path.read_text())
            safety += safety_stats(trace)
            c, fe = trace_stats(trace)
            compactions += c
            if fe is not None:
                first_edits.append(fe)
    thoughts = sorted(int(n) for log in (run_dir / "logs").glob("*.log")
                      for n in re.findall(r"\((\d+) chars\)", log.read_text(errors="replace")))
    by_bucket = Counter()
    for (bucket, _), n in buckets.items():
        by_bucket[bucket] += n
    print(f"  {name}: buckets {dict(by_bucket)}")
    if labels:
        print("    by label: " + ", ".join(f"{b}/{lab} {n}" for (b, lab), n in sorted(buckets.items())))
    if thoughts:
        q = lambda f: thoughts[min(len(thoughts) - 1, int(f * len(thoughts)))]  # noqa: E731
        print(f"    thought chars/call p50 {q(.5)} p90 {q(.9)} p99 {q(.99)} total {sum(thoughts)}")
    edit_p50 = statistics.median(first_edits) if first_edits else "-"
    print(f"    compactions {compactions}  first edit call p50 {edit_p50} "
          f"(edited {len(first_edits)}/{len(rows)})")
    short = [rows[i] for i in rows if i in short_ids]
    if short:
        fast = sum(1 for r in short if r["tool_calls"] <= TRIAGE_MAX_CALLS)
        print(f"    triage: {fast}/{len(short)} short-statement tasks ended within {TRIAGE_MAX_CALLS} calls, "
              f"resolved {sum(r['resolved'] for r in short)}")
    gate = "FAIL" if safety["list_skill_args"] or safety["stalls"] else "ok"
    print(f"    safety {gate}: rejected calls {safety['rejected']}, identical retries "
          f"{safety['identical_retries']}, list-form skill args {safety['list_skill_args']}, "
          f"stalls >{STALL_GAP_S}s {safety['stalls']}")


def paired(name: str, rows: dict[str, dict], base_name: str, base: dict[str, dict]) -> None:
    shared = sorted(set(base) & set(rows))
    wins = [i for i in shared if rows[i]["resolved"] and not base[i]["resolved"]]
    losses = [i for i in shared if base[i]["resolved"] and not rows[i]["resolved"]]
    per_repo = Counter()
    for i in wins:
        per_repo[rows[i]["repo"]] += 1
    for i in losses:
        per_repo[rows[i]["repo"]] -= 1
    repos = ", ".join(f"{repo} {n:+d}" for repo, n in sorted(per_repo.items()))
    print(f"{name} vs {base_name}: +{len(wins)} -{len(losses)} net {len(wins) - len(losses):+d} "
          f"on {len(shared)} shared tasks ({repos or 'no change'})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dirs", nargs="+", type=Path)
    parser.add_argument("--n-scorer", type=int, default=120)
    parser.add_argument("--slowdown", type=float, default=1.0, help="concurrency slowdown factor (calibration)")
    parser.add_argument("--setup-verify-s", type=float, default=40.0, help="per-task setup + verification seconds")
    parser.add_argument("--tasks", type=Path, help="tasks.jsonl with gold patches; enables the taxonomy")
    parser.add_argument("--labels", type=Path, help="task_specificity.jsonl (instance_id -> label)")
    parser.add_argument("--caps", help="comma-separated run-time caps in seconds to replay (needs --tasks "
                        "for the worst-case projection)")
    args = parser.parse_args()
    runs = {d.name: load(d) for d in args.dirs}
    for name, rows in runs.items():
        describe(name, rows, args.n_scorer, args.slowdown, args.setup_verify_s)
    base_name, base = next(iter(runs.items()))
    for name, rows in list(runs.items())[1:]:
        paired(name, rows, base_name, base)
    tasks = []
    if args.tasks:
        tasks = [json.loads(line) for line in args.tasks.read_text().splitlines() if line.strip()]
    short_ids = {t["instance_id"] for t in tasks if short_statement(t.get("problem_statement", ""))}
    if args.caps:
        print("cap replay (resolved, expected h, worst-case h without short-statement tasks):")
        for cap in (float(c) for c in args.caps.split(",")):
            cells = []
            for name, rows in runs.items():
                resolved, hours = project(rows, cap, args.slowdown, args.setup_verify_s, args.n_scorer)
                long_rows = {i: r for i, r in rows.items() if i not in short_ids}
                _, worst = project(long_rows, cap, args.slowdown, args.setup_verify_s, args.n_scorer)
                cells.append(f"{name} {resolved} {hours:.2f}h/{worst:.2f}h")
            print(f"  {cap:5.0f}s  " + "  ".join(cells))
    if args.tasks:
        gold = {t["instance_id"]: patched_files(t["patch"]) for t in tasks}
        labels = {}
        if args.labels:
            for line in args.labels.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    labels[row["instance_id"]] = row["label"]
        print("taxonomy:")
        for d in args.dirs:
            diagnose(d.name, d, runs[d.name], gold, labels, short_ids)


if __name__ == "__main__":
    main()

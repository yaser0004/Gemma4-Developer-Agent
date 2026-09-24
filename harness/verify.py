"""Phase-2 verification: grade a patch the way the official harness does.

Follows HARNESS_README.md section 8.2. In a fresh sandbox: apply the agent
patch (4-pass fallback), reset every file the task's test_patch touches (so
agent edits to those tests are discarded), apply test_patch, then run the
hermetic pytest command. Resolved iff pytest exits 0.

Usage:
    python -m harness.verify --split splits/dev_b.txt --patches gold --results-dir ../ai/analysis/runs/gold
    python -m harness.verify --task-ids httpx_3672 --patches none
    python -m harness.verify --split splits/dev_b.txt --patches ../ai/analysis/runs/x/patches

`--patches none` grades with an empty patch; tests should FAIL (fail-to-pass
check). Unlike the official Phase 2, which skips empty patches as unresolved,
this actually runs the tests so the verifier itself can be validated.

Resolution criterion. Officially: pytest exit code 0. Locally some tests cannot
pass in any patch (e.g. requests tests that need the pytest-httpbin plugin or
network, which the official environment provides through unpublished caches).
So `--patches gold` and `--patches none` record per-test reference outcomes in
<data_dir>/../reference_results/, and every other run is graded SWE-bench
style: resolved iff every test that passes with the gold patch also passes.
Tasks where no test goes from failing (baseline) to passing (gold) cannot be
verified locally; `verifiable` in the reference marks them. The exit-code
verdict is always reported too, as `resolved_exit`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from xml.etree import ElementTree
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harness.sandbox import DEFAULT_DATA_DIR, Sandbox, load_tasks, read_split

PATCH_FILE = "/tmp/agent.patch"
JUNIT_FILE = "/tmp/swegemma_junit.xml"
PYTEST_TIMEOUT = 900

# README 8.2 step 2, in order. Pass 2 (symlink-normalized paths) is omitted: it
# only matters for repos with symlinked package directories.
APPLY_ATTEMPTS = [
    ["git", "apply", "--unsafe-paths", "-p1", PATCH_FILE],
    ["git", "apply", "--unsafe-paths", "-3", PATCH_FILE],
    ["git", "apply", "--unsafe-paths", "--ignore-space-change", "--ignore-whitespace", PATCH_FILE],
    ["git", "apply", "--unsafe-paths", "--recount", PATCH_FILE],
    ["git", "apply", "--unsafe-paths", "-p0", PATCH_FILE],
]
GNU_PATCH_FLAGS = [["-p1"], ["-p0"], ["-p1", "-l"], ["-p0", "-l"]]


def patched_files(patch: str) -> list[str]:
    """Paths a unified diff writes to (`+++ b/<path>`), excluding deletions."""
    files = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[4:].split("\t", 1)[0].strip()
            if path == "/dev/null":
                continue
            files.append(path[2:] if path.startswith("b/") else path)
    return list(dict.fromkeys(files))


def target_test_files(test_patch: str) -> list[str]:
    """Test files the grader runs: python files touched by test_patch."""
    return [f for f in patched_files(test_patch) if f.endswith(".py")]


def pytest_command(targets: list[str], junit: bool = False) -> str:
    """README 8.2 step 4, verbatim flags (plus a JUnit report for per-test grading)."""
    quoted = " ".join(f"'{t}'" for t in targets)
    return (
        f"PYTHONSAFEPATH=1 python3 -m pytest {quoted} -p no:anyio -o timeout=0 "
        "-o norecursedirs='.* build dist venv' -o python_classes='Test* *Test' -q"
        + (f" --junitxml={JUNIT_FILE}" if junit else "")
    )


def parse_junit(xml_text: str) -> dict[str, str]:
    """Map test id -> passed/failed/error/skipped from a pytest JUnit report."""
    # The report is written inside the sandbox where patched code runs; refuse DTDs
    # so entity-expansion payloads never reach the XML parser.
    if not xml_text.strip() or "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        return {}
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return {}
    outcomes = {}
    for case in root.iter("testcase"):
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        kinds = {child.tag for child in case}
        outcome = ("error" if "error" in kinds else "failed" if "failure" in kinds
                   else "skipped" if "skipped" in kinds else "passed")
        # A test reported twice (e.g. setup error after a pass) keeps its worst outcome.
        if outcomes.get(test_id) in (None, "passed", "skipped"):
            outcomes[test_id] = outcome
    return outcomes


def reference_path(data_dir: Path, instance_id: str) -> Path:
    return data_dir.parent / "reference_results" / f"{instance_id}.json"


def load_reference(data_dir: Path, instance_id: str) -> dict:
    path = reference_path(data_dir, instance_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def update_reference(data_dir: Path, instance_id: str, key: str, passed: list[str]) -> dict:
    ref = load_reference(data_dir, instance_id)
    ref[key] = sorted(passed)
    if "gold_passed" in ref and "baseline_passed" in ref:
        ref["fail_to_pass"] = sorted(set(ref["gold_passed"]) - set(ref["baseline_passed"]))
        ref["verifiable"] = bool(ref["fail_to_pass"])
    path = reference_path(data_dir, instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ref, indent=1), encoding="utf-8")
    return ref


def judge(outcomes: dict[str, str], reference: dict) -> bool | None:
    """Resolved iff every gold-passing test passes; None when there is no usable reference."""
    gold = reference.get("gold_passed")
    if not gold:
        return None
    return all(outcomes.get(t) == "passed" for t in gold)


def apply_patch(box: Sandbox, patch: str) -> tuple[bool, str]:
    """Apply `patch` with the harness's fallback chain. Returns (ok, method or error)."""
    # tasks.jsonl stores patches without the final newline, which git apply rejects as corrupt.
    if not patch.endswith("\n"):
        patch += "\n"
    box.put_file(PATCH_FILE, patch.encode("utf-8"))
    for argv in APPLY_ATTEMPTS:
        if box.run(*argv, timeout=120).exit_code == 0:
            return True, " ".join(argv[1:-1])
    for flags in GNU_PATCH_FLAGS:
        base = ["patch", "--batch", "--forward", *flags, "-i", PATCH_FILE]
        if box.run(*base[:1], "--dry-run", *base[1:], timeout=120).exit_code == 0:
            if box.run(*base, timeout=120).exit_code == 0:
                return True, "patch " + " ".join(flags)
    err = box.run("git", "apply", "--unsafe-paths", "--check", PATCH_FILE, timeout=120)
    return False, (err.stderr or err.stdout)[-1500:]


_SAFE_PATH = re.compile(r"^[\w./@+-]+$")


def verify_task(task: dict, patch: str | None, data_dir: Path = DEFAULT_DATA_DIR, record: str | None = None) -> dict:
    """Grade one patch. `patch=None` means grade the unmodified baseline.

    `record` ("gold_passed" or "baseline_passed") stores this run's passing tests
    as reference data instead of judging against it.
    """
    started = time.monotonic()
    result = {"instance_id": task["instance_id"], "resolved": False, "resolved_exit": False,
              "exit_code": None, "apply": None, "error": None}
    targets = target_test_files(task["test_patch"])
    bad = [t for t in patched_files(task["test_patch"]) if not _SAFE_PATH.match(t)]
    if bad:
        result["error"] = f"refusing unusual test paths: {bad}"
        return result
    try:
        with Sandbox() as box:
            box.bootstrap(task, data_dir, commit_message="eval_baseline")
            if patch:
                ok, how = apply_patch(box, patch)
                result["apply"] = how if ok else None
                if not ok:
                    result["error"] = f"Failed to apply agent_patch: {how}"
                    return result
            files = patched_files(task["test_patch"])
            box.run("git", "checkout", "HEAD", "--", *files, timeout=60)
            box.run("git", "clean", "-f", "--", *files, timeout=60)
            ok, how = apply_patch(box, task["test_patch"])
            if not ok:
                result["error"] = f"Failed to apply test_patch: {how}"
                return result
            # README 8.2 step 4: refresh pytest.ini/conftest.py after the test patch.
            box.exec("python3 /sandbox/setup.py --fast-path >/dev/null 2>&1", timeout=120)
            res = box.exec(pytest_command(targets, junit=True), timeout=PYTEST_TIMEOUT)
            outcomes = parse_junit(box.run("cat", JUNIT_FILE, timeout=60).stdout)
            result["exit_code"] = res.exit_code
            result["resolved_exit"] = res.exit_code == 0
            result["tests"] = {k: sum(v == k for v in outcomes.values()) for k in ("passed", "failed", "error", "skipped")}
            if record:
                ref = update_reference(data_dir, task["instance_id"], record, [t for t, o in outcomes.items() if o == "passed"])
                result["verifiable"] = ref.get("verifiable")
                result["fail_to_pass"] = len(ref.get("fail_to_pass", []))
            verdict = judge(outcomes, load_reference(data_dir, task["instance_id"]))
            result["resolved"] = result["resolved_exit"] if verdict is None else verdict
            result["test_output"] = (res.stdout + res.stderr)[-20000:]
            if res.timed_out:
                result["error"] = f"pytest timed out after {PYTEST_TIMEOUT}s"
    except Exception as exc:  # noqa: BLE001 - one broken task must not stop a batch run
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["seconds"] = round(time.monotonic() - started, 1)
    return result


def _load_patch(mode: str, task: dict) -> str | None:
    if mode == "gold":
        return task["patch"]
    if mode == "none":
        return None
    path = Path(mode) / f"{task['instance_id']}.patch"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--split", type=Path, help="file with one instance_id per line")
    parser.add_argument("--task-ids", nargs="*")
    parser.add_argument("--patches", required=True, help="'gold', 'none', or a directory of <instance_id>.patch files")
    parser.add_argument("--results-dir", type=Path)
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args(argv)

    ids = args.task_ids or (read_split(args.split) if args.split else None)
    tasks = load_tasks(args.data_dir, ids)
    if args.results_dir:
        (args.results_dir / "test_outputs").mkdir(parents=True, exist_ok=True)

    def grade(task: dict) -> dict:
        patch = _load_patch(args.patches, task)
        if patch == "":
            # Official Phase 2 is skipped for an empty agent patch: unresolved.
            return {"instance_id": task["instance_id"], "resolved": False, "error": "empty patch", "seconds": 0}
        record = {"gold": "gold_passed", "none": "baseline_passed"}.get(args.patches)
        return verify_task(task, patch, args.data_dir, record=record)

    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for r in pool.map(grade, tasks):
            results.append(r)
            print(f"{r['instance_id']:<24} resolved={r['resolved']!s:<5} exit={r.get('exit_code')} tests={r.get('tests')} "
                  f"verifiable={r.get('verifiable')} {r.get('seconds', 0):>6}s {r.get('error') or ''}"[:260], flush=True)
            if args.results_dir:
                output = r.pop("test_output", "")
                (args.results_dir / "test_outputs" / f"{r['instance_id']}.log").write_text(output, encoding="utf-8")
                with (args.results_dir / "verify.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(r) + "\n")

    resolved = sum(r["resolved"] for r in results)
    resolved_exit = sum(bool(r.get("resolved_exit")) for r in results)
    print(f"resolved {resolved}/{len(results)} (exit-code criterion: {resolved_exit})")
    if args.results_dir:
        (args.results_dir / "verify_summary.json").write_text(json.dumps(
            {"patches": args.patches, "resolved": resolved, "resolved_exit": resolved_exit, "total": len(results),
             "resolved_ids": [r["instance_id"] for r in results if r["resolved"]],
             "unverifiable_ids": [r["instance_id"] for r in results if r.get("verifiable") is False]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

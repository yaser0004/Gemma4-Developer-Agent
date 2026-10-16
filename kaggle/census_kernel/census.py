"""Kaggle CPU kernel: which public tasks does the official verifier discriminate?

For every public task run swegemma's Phase 2 verification twice in the Kaggle environment: with the
gold patch and with an empty patch. A task is usable for local evaluation when gold resolves and the
empty patch does not. Writes /kaggle/working/census.json and splits/eval_main.txt (one id per line).
No GPU and no model are needed.
"""

import asyncio
import glob
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR = Path("/kaggle/input/competitions/gemma-4-developer-agent")
WHEELHOUSE_DIR = Path("/kaggle/input/datasets/metric/gemma-4-developer-agent-wheelhouse")
WORK = Path("/kaggle/working")
CONCURRENCY = 3
os.environ.update({"LITELLM_LOCAL_MODEL_COST_MAP": "True", "OTEL_SDK_DISABLED": "true", "TRANSFORMERS_NO_TF": "1"})


def install() -> None:
    for pattern in ("/usr/local/lib/python*/dist-packages/*cutlass*.pth", "/usr/local/lib/python*/site-packages/*cutlass*.pth"):
        for pth in glob.glob(pattern):
            try:
                os.unlink(pth)
            except OSError:
                pass
    tmp = Path("/tmp/wheelhouse")
    tmp.mkdir(parents=True, exist_ok=True)
    for w in WHEELHOUSE_DIR.glob("*.whl"):
        if "cutlass" in w.name.lower():
            continue
        name = w.name.replace("cu128", "+cu128") if ("cu128" in w.name and "+" not in w.name) else w.name
        if not (tmp / name).exists():
            os.symlink(w, tmp / name)
    wheels = sorted(str(w) for w in tmp.glob("*.whl"))
    print(f"python {sys.version.split()[0]}; installing {len(wheels)} wheels", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--force-reinstall", *wheels], check=True)
    importlib.invalidate_caches()


async def census() -> None:
    from adk_submission import ModelRegistry
    from swegemma.config import EvalConfig, build_submission_limits
    from swegemma.deduplication import resolve_task_snapshot_paths
    from swegemma.evaluate import Evaluator
    from swegemma.harness.verification import verify_task
    from swegemma.models import load_tasks

    limits, gen_constraints = build_submission_limits()
    config = EvalConfig(
        tasks_path=DATA_DIR / "tasks.jsonl", snapshots_dir=DATA_DIR / "snapshots",
        results_dir=WORK / "results" / "_census", submission_dir=DATA_DIR / "sample_submission", models=ModelRegistry(),
        sandbox="subprocess", timeout_seconds=300, verification_timeout_seconds=300,
        limits=limits, generation_constraints=gen_constraints, wheels_dir=DATA_DIR / "wheels",
        display_mode="quiet", verbose=False,
    )
    evaluator = Evaluator(config)
    sem = asyncio.Semaphore(CONCURRENCY)
    rows: dict[str, dict] = {}

    async def one(task) -> None:
        async with sem:
            snap, base, patch_path = resolve_task_snapshot_paths(config.snapshots_dir, task.instance_id, task.repo)
            row = {"repo": task.repo}
            for label, patch in (("gold", task.patch), ("empty", "")):
                t0 = time.time()
                try:
                    res = await verify_task(evaluator.docker, config, task, snap, base_snapshot_path=base,
                                            patch_path=patch_path, agent_patch=patch)
                    row[label] = bool(res.resolved)
                    row[f"{label}_error"] = (getattr(res, "error", None) or "")[:200]
                except Exception as exc:  # noqa: BLE001 - record and keep going
                    row[label] = False
                    row[f"{label}_error"] = f"{type(exc).__name__}: {exc}"[:200]
                row[f"{label}_seconds"] = round(time.time() - t0, 1)
            rows[task.instance_id] = row
            print(json.dumps({task.instance_id: row}), flush=True)

    tasks = load_tasks(DATA_DIR / "tasks.jsonl")
    await asyncio.gather(*(one(t) for t in tasks))
    keep = sorted(i for i, r in rows.items() if r.get("gold") and not r.get("empty"))
    (WORK / "census.json").write_text(json.dumps(rows, indent=2))
    (WORK / "eval_main.txt").write_text("\n".join(keep) + "\n")
    print(f"usable {len(keep)}/{len(rows)}", flush=True)


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    install()
    asyncio.run(census())

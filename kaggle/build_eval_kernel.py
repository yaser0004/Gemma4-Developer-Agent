"""Build a Kaggle GPU kernel that evaluates agent variants with the official harness.

Usage:
    python kaggle/build_eval_kernel.py --user <kaggle_username> --out dist/eval_kernel \\
        --variant variants/v6_anchor --variant variants/v7_budget \\
        --tasks-file splits/eval_main.txt [--concurrency 3] [--time-scale 1.0]
    kaggle kernels push -p dist/eval_kernel
    kaggle kernels status <user>/gemma4-agent-eval
    kaggle kernels output <user>/gemma4-agent-eval -p <dir>

All variants in one kernel must declare the same model (vLLM is started once). Each variant runs
with its own eval_config.yaml budgets, exactly like the scorer; --time-scale multiplies
max_time_minutes to compensate for the slowdown measured under --concurrency.
Uses Kaggle GPU quota. The kernel is private and only talks to its own local vLLM server.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_submission import ALLOWED_MODELS, validate  # noqa: E402

SLUG = "gemma4-agent-eval"
TEMPLATE = Path(__file__).with_name("eval_kernel_template.py")
MODEL_VERSION = 2  # v1 is silently dropped when attached
WHEELHOUSE = "metric/gemma-4-developer-agent-wheelhouse"
MODEL_LINE = re.compile(r"^\s*model:\s*(?:openai/|hosted_vllm/|custom/|google/)?([^\s#]+)", re.M)


def zip_variants(variants: list[Path]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for variant in variants:
            for f in sorted(p for p in variant.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
                zf.write(f, f"{variant.name}/{f.relative_to(variant).as_posix()}")
    return buf.getvalue()


def declared_models(variants: list[Path]) -> set[str]:
    return {m for v in variants for y in v.rglob("*.y*ml") for m in MODEL_LINE.findall(y.read_text(encoding="utf-8"))}


def read_task_ids(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variant", type=Path, action="append", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tasks-file", type=Path, help="one instance_id per line")
    group.add_argument("--task-ids", nargs="+")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--time-scale", type=float, default=1.0)
    parser.add_argument("--slug", default=SLUG)
    args = parser.parse_args()

    names = [v.name for v in args.variant]
    if len(set(names)) != len(names):
        raise SystemExit(f"variant directory names must be unique: {names}")
    for variant in args.variant:
        errors = validate(variant)
        if errors:
            raise SystemExit(f"{variant} is invalid:\n  " + "\n  ".join(errors))
    models = declared_models(args.variant)
    if len(models) != 1 or not models <= ALLOWED_MODELS:
        raise SystemExit(f"variants must share exactly one allowed model, got {sorted(models)}")
    model = next(iter(models))
    task_ids = args.task_ids or read_task_ids(args.tasks_file)

    script = TEMPLATE.read_text(encoding="utf-8")
    for key, value in {
        "__VARIANTS_ZIP_B64__": base64.b64encode(zip_variants(args.variant)).decode("ascii"),
        "__TASK_IDS__": json.dumps(task_ids),
        "__CONCURRENCY__": str(args.concurrency),
        "__TIME_SCALE__": repr(args.time_scale),
        "__MODEL_NAME__": model,
    }.items():
        script = script.replace(key, value)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "eval_kernel.py").write_text(script, encoding="utf-8")
    (args.out / "kernel-metadata.json").write_text(json.dumps({
        "id": f"{args.user}/{args.slug}",
        "title": args.slug,
        "code_file": "eval_kernel.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": True,
        "enable_tpu": False,
        "enable_internet": False,  # NvidiaL4 is only granted with internet off (as in the organizers' notebook)
        "machine_shape": "NvidiaL4",
        "competition_sources": ["gemma-4-developer-agent"],
        "dataset_sources": [WHEELHOUSE],
        "model_sources": [f"google/gemma-4/other/{model}/{MODEL_VERSION}"],
        "kernel_sources": [],
    }, indent=2), encoding="utf-8")
    print(f"kernel written to {args.out}: model={model} variants={names}, {len(task_ids)} tasks, concurrency={args.concurrency}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

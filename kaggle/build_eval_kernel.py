"""Build a Kaggle GPU kernel that evaluates agent variants with the official harness.

Usage:
    python kaggle/build_eval_kernel.py --user <kaggle_username> --out dist/eval_kernel \\
        --variant variants/v2_localizer --variant variants/v2_nothink \\
        --task-ids requests_7205 rich_3518 ... [--max-time-minutes 15]
    kaggle kernels push -p dist/eval_kernel
    kaggle kernels status <user>/gemma4-agent-eval
    kaggle kernels output <user>/gemma4-agent-eval -p <dir>

Uses Kaggle GPU quota. The kernel is private and only talks to its own local vLLM server.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_submission import validate  # noqa: E402

SLUG = "gemma4-agent-eval"
TEMPLATE = Path(__file__).with_name("eval_kernel_template.py")
MODEL_SOURCE = "google/gemma-4/other/gemma-4-31b-it-qat-w4a16-ct/2"  # v1 is silently dropped when attached
WHEELHOUSE = "metric/gemma-4-developer-agent-wheelhouse"


def zip_variants(variants: list[Path]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for variant in variants:
            for f in sorted(p for p in variant.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
                zf.write(f, f"{variant.name}/{f.relative_to(variant).as_posix()}")
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variant", type=Path, action="append", required=True)
    parser.add_argument("--task-ids", nargs="+", required=True)
    parser.add_argument("--max-time-minutes", type=float, default=15.0,
                        help="per task; T4s are several times slower than the scorer's 4x L4")
    parser.add_argument("--max-tool-calls", type=int, default=40)
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--gold-control", action="store_true",
                        help="first verify each task's gold patch (checks the grading environment itself)")
    args = parser.parse_args()

    names = [v.name for v in args.variant]
    if len(set(names)) != len(names):
        raise SystemExit(f"variant directory names must be unique: {names}")
    for variant in args.variant:
        errors = validate(variant)
        if errors:
            raise SystemExit(f"{variant} is invalid:\n  " + "\n  ".join(errors))

    script = TEMPLATE.read_text(encoding="utf-8")
    script = (script.replace("__VARIANTS_ZIP_B64__", base64.b64encode(zip_variants(args.variant)).decode("ascii"))
                    .replace("__TASK_IDS__", json.dumps(args.task_ids))
                    .replace("__MAX_TIME_MINUTES__", repr(args.max_time_minutes))
                    .replace("__MAX_TOOL_CALLS__", str(args.max_tool_calls))
                    .replace("__MAX_TURNS__", str(args.max_turns))
                    .replace("__GOLD_CONTROL__", str(bool(args.gold_control))))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "eval_kernel.py").write_text(script, encoding="utf-8")
    (args.out / "kernel-metadata.json").write_text(json.dumps({
        "id": f"{args.user}/{SLUG}",
        "title": SLUG,
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
        "model_sources": [MODEL_SOURCE],
        "kernel_sources": [],
    }, indent=2), encoding="utf-8")
    print(f"kernel written to {args.out}: variants={names}, {len(args.task_ids)} tasks")
    return 0


if __name__ == "__main__":
    sys.exit(main())

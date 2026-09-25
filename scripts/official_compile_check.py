"""Compile submissions with the organizers' own adk_submission compiler (no model calls).

Catches rules that only the official compiler enforces (e.g. skill paths must be
relative to the submission root with no `..`). Needs the official wheels, which
live in a separate virtualenv because they pin google-adk 1.x:

    python -m venv .venv-official
    .venv-official/bin/pip install "google-adk==1.36.1" "google-genai==2.11.0" "litellm>=1.84,<1.85" \\
        docker networkx numpy pandas cachetools python-dotenv rich pyyaml pydantic tokenizers
    .venv-official/bin/pip install --no-deps <wheelhouse>/swegemma-*.whl <wheelhouse>/adk_submission-*.whl \\
        <wheelhouse>/adk_eval_core-*.whl        # Kaggle dataset metric/gemma-4-developer-agent-wheelhouse

Usage: .venv-official/bin/python scripts/official_compile_check.py submission [variants/... ...]
"""

import sys
from pathlib import Path

from adk_submission import compile_submission
from swegemma.models.registry import setup_gemma_model_registry

HARNESS_TOOLS = ["run_command", "submit_patch", "get_status", "read_file", "edit_file", "write_file",
                 "get_code_neighbors", "search_similar_code", "get_code_subgraph"]


def _stub(name: str):
    def tool(**kwargs):
        return name
    tool.__name__ = name
    tool.__doc__ = name
    return tool


def describe(agent, depth: int = 0) -> None:
    names = [getattr(t, "name", getattr(t, "__name__", type(t).__name__)) for t in getattr(agent, "tools", [])]
    print("  " * depth + f"{type(agent).__name__} {agent.name}: {names}")
    for tool in getattr(agent, "tools", []):
        if hasattr(tool, "agent"):
            describe(tool.agent, depth + 1)
    for sub in getattr(agent, "sub_agents", []):
        describe(sub, depth + 1)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    tools = {name: _stub(name) for name in HARNESS_TOOLS}
    # Unreachable endpoint on purpose: compilation must not call the model.
    models = setup_gemma_model_registry(api_base="http://127.0.0.1:9/v1", api_key="unused")
    failed = 0
    for sub in argv:
        try:
            agent = compile_submission(submission_dir=Path(sub), tool_registry=tools, model_registry=models)
        except Exception as exc:  # noqa: BLE001 - report every compiler error type the same way
            failed += 1
            print(f"FAIL {sub}: {type(exc).__name__}: {exc}")
            continue
        print(f"OK {sub}")
        describe(agent, 1)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

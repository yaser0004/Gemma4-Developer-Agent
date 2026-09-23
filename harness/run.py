"""Run a submission on tasks end to end: agent session (Phase 1) + grading (Phase 2).

Replicates HARNESS_README.md sections 4-5 and 8-9: container A bootstrap, the
initial prompt, the outer loop with continuation nudges, fallback patch
extraction, then verification in a fresh container B. Output layout follows
README section 9.2.

Usage:
    export GOOGLE_API_KEY=...
    python -m harness.run --submission submission --split splits/dev_b.txt \
        --results-dir ../ai/analysis/runs/<name> [--limit 5] [--max-tool-calls 40]

Budgets default to submission/eval_config.yaml. The Gemini API proxy is much
slower per call than the competition's local vLLM, so for local runs prefer
limiting tool calls and turns and give the wall-clock budget headroom
(--max-time-minutes). Token counts per task are logged so time on the real
hardware can be estimated separately.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig
from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from harness.compile import compile_submission
from harness.sandbox import DEFAULT_DATA_DIR, Sandbox, load_tasks, read_split
from harness.tools import Budget, CodeGraph, ToolContext
from harness.verify import verify_task
from scripts.validate_submission import load_yaml

log = logging.getLogger("harness.run")

MAX_MODEL_LEN = 32768
MAX_NUDGES = 3
NUDGE_TOOL_CALL_CUT = (
    "Your previous response reached the token limit before the tool call finished closing (<|tool_call|> was cut off). "
    "Do NOT repeat your prior reasoning in thought—emit your next tool call immediately, and if calling edit_file or "
    "write_file, split the change into smaller incremental edits."
)
NUDGE_MAX_TOKENS = (
    "Your previous response reached the token limit while thinking before a tool call was completed. Do NOT repeat your "
    "analysis in thought—keep reasoning under a few sentences and emit your next tool call immediately, or call "
    "submit_patch when you have completed and verified your changes."
)
NUDGE_CONTINUE = "Please continue your work using the available tools, or call submit_patch when you have completed and verified your changes."


def load_budget(submission: Path, overrides: dict[str, Any]) -> Budget:
    budget = Budget()
    path = submission / "eval_config.yaml"
    if path.is_file():
        ev = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("evaluation", {})
        budget.command_timeout_seconds = int(ev.get("timeout_seconds", budget.command_timeout_seconds))
        budget.tool_calls = ev.get("max_tool_calls", budget.tool_calls)
        budget.time_minutes = float(ev.get("max_time_minutes", budget.time_minutes))
        budget.turns = int(ev.get("max_turns", budget.turns))
    for key, value in overrides.items():
        if value is not None:
            setattr(budget, key, value)
    return budget


def max_output_tokens(submission: Path) -> int:
    """Largest max_output_tokens any agent requests; vLLM needs prompt + this <= max_model_len."""
    found = [int(m) for m in _find_key(_yaml_tree(submission), "max_output_tokens")]
    return max(found, default=16384)


def _yaml_tree(submission: Path) -> list[Any]:
    return [load_yaml(p, submission) for p in submission.rglob("*.y*ml") if p.suffix in (".yaml", ".yml")]


def _find_key(obj: Any, key: str) -> list[Any]:
    if isinstance(obj, dict):
        return [v for k, v in obj.items() if k == key] + [x for v in obj.values() for x in _find_key(v, key)]
    if isinstance(obj, list):
        return [x for v in obj for x in _find_key(v, key)]
    return []


class ContextOverflow(Exception):
    pass


def build_agent_prompt(task: dict, budget: Budget, has_graph: bool, layout: str) -> str:
    """README 5.2. Sections 1-4, 6, 7 follow the README; section 5's exact wording is not published."""
    parts = [f"You are evaluating a software engineering task for repository {task['repo']}.\n\nProblem Statement:\n{task['problem_statement']}"]
    if task.get("hints_text", "").strip():
        parts.append(f"## Hints:\n{task['hints_text']}")
    lines = [f"- Time allowance: {budget.time_minutes:.1f} minutes"]
    if budget.tool_calls is not None:
        lines.append(f"- Tool calls allowance: {budget.tool_calls} calls")
    lines.append(f"- Max loop iterations: {budget.turns} turns")
    parts.append("## Task Budget (Session terminates when any budget is exhausted)\n" + "\n".join(lines))
    parts.append(
        "## Execution Environment Rules\n"
        f"- Single command timeout: {budget.command_timeout_seconds} seconds (commands exceeding this fail without ending the session)\n"
        f"- Command output limit: {budget.max_stdout_chars} characters\n"
        f"- File view limit: {budget.max_file_lines} lines per read_file call\n"
        f"- File character limit: {budget.max_file_chars} characters per read_file call\n"
        "- Environment is offline (no network/PyPI access). All repository and test dependencies are ALREADY pre-installed. "
        "Do NOT attempt to run pip install or download packages."
    )
    parts.append(
        "## Instructions\n"
        "0. Work strictly inside /workspace; the repository is checked out there.\n"
        "1. Inspect the existing code and follow its conventions.\n"
        "2. Make the minimal source changes needed to resolve the problem statement.\n"
        "3. Prefer quick inline checks (python3 -c \"...\") over running full test suites.\n"
        "4. When your changes are complete and verified, call submit_patch.\n"
        "5. After submit_patch, reply with a brief final text summary."
    )
    if has_graph:
        parts.append(
            "## Code Intelligence Tools\n"
            "This repository has pre-built code graph and embedding data. Use these tools for fast, targeted navigation:\n"
            "- `search_similar_code(query)`: Find semantically similar functions/classes by keyword.\n"
            "- `get_code_neighbors(node)`: Find callers, callees, and definitions related to a symbol.\n"
            "- `get_code_subgraph(nodes)`: Get the induced subgraph for a set of symbols."
        )
    parts.append(f"## Workspace Layout\n{layout}")
    return "\n\n".join(parts)


def _event_record(event: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {"author": event.author}
    if event.content and event.content.parts:
        texts, thoughts = [], []
        for p in event.content.parts:
            if p.text:
                (thoughts if getattr(p, "thought", False) else texts).append(p.text)
        if texts:
            rec["text"] = "\n".join(texts)
        if thoughts:
            rec["thought"] = "\n".join(thoughts)
    calls = event.get_function_calls()
    if calls:
        rec["calls"] = [{"name": c.name, "args": dict(c.args or {})} for c in calls]
    responses = event.get_function_responses()
    if responses:
        rec["responses"] = [{"name": r.name, "response": json.dumps(r.response)[:4000]} for r in responses]
    if event.usage_metadata:
        u = event.usage_metadata
        rec["usage"] = {"prompt": u.prompt_token_count, "output": u.candidates_token_count, "thoughts": u.thoughts_token_count}
    if event.finish_reason:
        rec["finish_reason"] = str(event.finish_reason)
    if event.error_code:
        rec["error"] = f"{event.error_code}: {event.error_message}"
    return rec


async def run_agent_session(task: dict, ctx: ToolContext, agent: Any, prompt: str, out_limit: int, enforce_context: bool) -> dict[str, Any]:
    """README 5.3 outer loop. Returns session stats; the trace is collected into stats['trace']."""
    app = App(
        name="swegemma_eval",
        root_agent=agent,
        events_compaction_config=EventsCompactionConfig(
            compaction_interval=15, overlap_size=2, token_threshold=MAX_MODEL_LEN, event_retention_size=5),
    )
    sessions = InMemorySessionService()
    runner = Runner(app=app, session_service=sessions)
    state = {"problem_description": task["problem_statement"]}
    if task.get("hints_text", "").strip():
        state["hints"] = task["hints_text"].strip()
    session = await sessions.create_session(app_name=app.name, user_id="eval_user", state=state)

    stats: dict[str, Any] = {"trace": [], "llm_calls": 0, "prompt_tokens_max": 0, "output_tokens": 0,
                             "thought_tokens": 0, "nudges": 0, "error": None, "stop_reason": None}
    message, consecutive_nudges = prompt, 0
    ctx.start_agent_session()
    while True:
        turn_has_tool_call, last_text, finish_reason = False, "", ""
        turns_left = ctx.budget.turns - stats["llm_calls"]
        try:
            async with asyncio.timeout(max(1.0, ctx.remaining_seconds())):
                events = runner.run_async(
                    user_id="eval_user", session_id=session.id,
                    new_message=types.Content(role="user", parts=[types.Part(text=message)]),
                    run_config=RunConfig(max_llm_calls=max(1, turns_left)),
                )
                # aclosing: close the generator in this task's context, not at GC time
                # (otherwise OpenTelemetry context detach errors are printed).
                async with contextlib.aclosing(events):
                    async for event in events:
                        rec = _event_record(event)
                        rec["t"] = round(ctx.elapsed(), 1)
                        stats["trace"].append(rec)
                        if "usage" in rec:
                            stats["llm_calls"] += 1
                            prompt_tokens = rec["usage"]["prompt"] or 0
                            stats["prompt_tokens_max"] = max(stats["prompt_tokens_max"], prompt_tokens)
                            stats["output_tokens"] += rec["usage"]["output"] or 0
                            stats["thought_tokens"] += rec["usage"]["thoughts"] or 0
                            if enforce_context and prompt_tokens + out_limit > MAX_MODEL_LEN:
                                raise ContextOverflow(prompt_tokens)
                        if "calls" in rec:
                            turn_has_tool_call = True
                        last_text = rec.get("text", last_text)
                        finish_reason = rec.get("finish_reason", finish_reason)
                        if ctx.patch_submitted and event.is_final_response():
                            break
        except TimeoutError:
            stats["stop_reason"] = "time budget"
            break
        except ContextOverflow as exc:
            stats["stop_reason"] = "context overflow"
            stats["error"] = f"prompt {exc.args[0]} + max_output_tokens {out_limit} > {MAX_MODEL_LEN} (vLLM would reject)"
            break
        except LlmCallsLimitExceededError:
            stats["stop_reason"] = "turn budget"
            break
        except Exception as exc:  # noqa: BLE001 - model/API errors end the session like agent_error does officially
            stats["stop_reason"] = "agent error"
            stats["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
            break

        if ctx.patch_submitted:
            stats["stop_reason"] = "submitted"
            break
        if reason := ctx.out_of_budget():
            stats["stop_reason"] = reason
            break
        consecutive_nudges = 0 if turn_has_tool_call else consecutive_nudges + 1
        if consecutive_nudges > MAX_NUDGES:
            stats["stop_reason"] = "max nudges"
            break
        if "<|tool_call>" in last_text and "<tool_call|>" not in last_text:
            message = NUDGE_TOOL_CALL_CUT
        elif "MAX_TOKENS" in finish_reason.upper() or "LENGTH" in finish_reason.upper():
            message = NUDGE_MAX_TOKENS
        else:
            message = NUDGE_CONTINUE
        stats["nudges"] += 1
    return stats


def workspace_layout(box: Sandbox) -> str:
    res = box.exec("find . -maxdepth 3 -not -path './.git*' -not -name '__pycache__' -not -path '*/__pycache__/*' "
                   "-not -name '*.pyc' | sort | head -150", timeout=60)
    return res.stdout.strip()


def run_task(task: dict, args: argparse.Namespace, budget: Budget, out_limit: int) -> dict[str, Any]:
    iid = task["instance_id"]
    result: dict[str, Any] = {"instance_id": iid, "repo": task["repo"], "resolved": False}
    started = time.monotonic()
    graph = CodeGraph.load(args.data_dir, iid)
    stats: dict[str, Any] = {}
    patch = ""
    try:
        with Sandbox() as box:
            box.bootstrap(task, args.data_dir)
            result["setup_seconds"] = round(time.monotonic() - started, 1)
            ctx = ToolContext(sandbox=box, budget=budget, graph=graph)
            agent = compile_submission(args.submission, ctx.create_tools())
            prompt = build_agent_prompt(task, budget, graph is not None, workspace_layout(box))
            stats = asyncio.run(run_agent_session(task, ctx, agent, prompt, out_limit, not args.no_enforce_context))
            patch = ctx.submitted_patch if ctx.patch_submitted else ctx.current_patch()
            result.update(submitted=ctx.patch_submitted, tool_calls=ctx.tool_calls_used,
                          agent_seconds=round(ctx.elapsed(), 1), tool_log=ctx.calls)
    except Exception as exc:  # noqa: BLE001 - keep the batch going
        result["error"] = f"{type(exc).__name__}: {exc}"

    trace = stats.pop("trace", [])
    result.update({k: v for k, v in stats.items() if v is not None})
    result["patch_size"] = len(patch)
    out = args.results_dir
    (out / "patches" / f"{iid}.patch").write_text(patch, encoding="utf-8")
    (out / "traces" / f"trace_{iid}.json").write_text(json.dumps({"instance_id": iid, "trace": trace, "tool_log": result.pop("tool_log", [])}, indent=1), encoding="utf-8")
    (out / "logs" / f"{iid}.log").write_text(_transcript(trace), encoding="utf-8")

    if patch.strip():
        verdict = verify_task(task, patch, args.data_dir)
        (out / "test_outputs" / f"{iid}.log").write_text(verdict.pop("test_output", ""), encoding="utf-8")
        result["resolved"] = verdict["resolved"]
        result["verify"] = {k: verdict.get(k) for k in ("exit_code", "apply", "error", "seconds")}
    result["total_seconds"] = round(time.monotonic() - started, 1)
    return result


def _transcript(trace: list[dict]) -> str:
    lines = []
    for r in trace:
        head = f"[{r.get('t', '')}s] {r['author']}"
        if "usage" in r:
            head += f" (prompt={r['usage']['prompt']} out={r['usage']['output']} thoughts={r['usage']['thoughts']})"
        lines.append(head)
        if "thought" in r:
            lines.append("  THOUGHT: " + r["thought"][:1500].replace("\n", "\n    "))
        if "text" in r:
            lines.append("  TEXT: " + r["text"][:3000].replace("\n", "\n    "))
        for c in r.get("calls", []):
            lines.append(f"  CALL {c['name']}: {json.dumps(c['args'])[:3000]}")
        for resp in r.get("responses", []):
            lines.append(f"  RESULT {resp['name']}: {resp['response'][:2000]}")
        if "error" in r:
            lines.append(f"  ERROR: {r['error']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--split", type=Path)
    parser.add_argument("--task-ids", nargs="*")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-tool-calls", type=int)
    parser.add_argument("--max-time-minutes", type=float)
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--no-enforce-context", action="store_true",
                        help="do not stop sessions whose prompt + max_output_tokens exceeds 32768")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    budget = load_budget(args.submission, {"tool_calls": args.max_tool_calls, "time_minutes": args.max_time_minutes,
                                           "turns": args.max_turns})
    out_limit = max_output_tokens(args.submission)
    ids = args.task_ids or (read_split(args.split) if args.split else None)
    tasks = load_tasks(args.data_dir, ids)[: args.limit]
    for sub in ("patches", "traces", "logs", "test_outputs"):
        (args.results_dir / sub).mkdir(parents=True, exist_ok=True)
    done = set()
    results_file = args.results_dir / "task_results.jsonl"
    if results_file.is_file():
        done = {json.loads(l)["instance_id"] for l in results_file.read_text(encoding="utf-8").splitlines() if l.strip()}
    (args.results_dir / "run_config.json").write_text(json.dumps(
        {"submission": str(args.submission.resolve()), "budget": vars(budget), "max_output_tokens": out_limit,
         "enforce_context": not args.no_enforce_context, "tasks": [t["instance_id"] for t in tasks]}, indent=2))

    def one(task: dict) -> dict:
        r = run_task(task, args, budget, out_limit)
        print(f"{r['instance_id']:<24} resolved={r['resolved']!s:<5} submitted={r.get('submitted')!s:<5} "
              f"calls={r.get('tool_calls')} llm={r.get('llm_calls')} maxprompt={r.get('prompt_tokens_max')} "
              f"{r.get('agent_seconds')}s stop={r.get('stop_reason')} {r.get('error') or ''}"[:250], flush=True)
        with results_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(r) + "\n")
        return r

    todo = [t for t in tasks if t["instance_id"] not in done]
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(one, todo))

    rows = [json.loads(l) for l in results_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    summary = {
        "resolved": sum(r["resolved"] for r in rows), "total": len(rows),
        "resolution_rate": round(sum(r["resolved"] for r in rows) / max(1, len(rows)), 4),
        "submitted": sum(bool(r.get("submitted")) for r in rows),
        "empty_patch": sum(r.get("patch_size", 0) == 0 for r in rows),
        "stop_reasons": {k: sum(r.get("stop_reason") == k for r in rows) for k in {r.get("stop_reason") for r in rows}},
        "per_repo": {repo: f"{sum(r['resolved'] for r in rows if r['repo'] == repo)}/{sum(r['repo'] == repo for r in rows)}"
                     for repo in sorted({r["repo"] for r in rows})},
    }
    (args.results_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

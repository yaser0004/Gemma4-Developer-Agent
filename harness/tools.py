"""Replica of the 9 swegemma agent tools (HARNESS_README.md section 6).

Signatures, JSON response shapes, budget gating, and truncation limits follow
the README. The official implementation and its tool descriptions are not
public, so docstrings here (which ADK sends to the model as tool descriptions)
are our best approximation.
"""

from __future__ import annotations

import difflib
import functools
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import numpy as np

from harness.sandbox import Sandbox


@dataclass
class Budget:
    time_minutes: float = 60.0
    tool_calls: int | None = None
    turns: int = 500
    command_timeout_seconds: int = 300
    max_stdout_chars: int = 5000
    max_file_lines: int = 150
    max_file_chars: int = 10000


class EditError(Exception):
    pass


def _ok(**payload: Any) -> str:
    return json.dumps({"status": "ok", **payload})


def _error(error_type: str, message: str, **details: Any) -> str:
    return json.dumps({"status": "error", "error_type": error_type, "error_message": message, "details": details})


def resolve_workspace_path(filepath: str) -> str:
    """Map an agent-supplied path to a relative path under /workspace, rejecting traversal."""
    if not isinstance(filepath, str) or not filepath.strip() or "\x00" in filepath:
        raise ValueError("filepath must be a non-empty string")
    path = filepath.strip()
    if path.rstrip("/") in ("", "/workspace"):
        raise ValueError("filepath must name a file")
    for prefix in ("/workspace/", "/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    if ".." in PurePosixPath(path).parts:
        raise ValueError(f"path traversal is not allowed: {filepath}")
    return path


# --- edit_file matching (adk-eval-core apply_replacement replica) -----------

_DELIMS = r"()\[\]{}:<>="


def _reindent(new_string: str, old_first_line: str, matched_first_line: str) -> str:
    old_indent = old_first_line[: len(old_first_line) - len(old_first_line.lstrip())]
    new_indent = matched_first_line[: len(matched_first_line) - len(matched_first_line.lstrip())]
    if old_indent == new_indent:
        return new_string
    out = []
    for line in new_string.split("\n"):
        if line.startswith(old_indent):
            line = new_indent + line[len(old_indent):]
        elif line.strip():
            line = new_indent + line.lstrip()
        out.append(line)
    return "\n".join(out)


def apply_replacement(content: str, old_string: str, new_string: str, allow_multiple: bool = False) -> tuple[str, int, str]:
    """Replace old_string with new_string using exact -> flexible -> regex matching.

    Returns (new_content, occurrences, strategy). Raises EditError on no match
    or on an ambiguous match when allow_multiple is False.
    """
    if not old_string:
        raise EditError("old_string must not be empty")
    content = content.replace("\r\n", "\n")
    old_string = old_string.replace("\r\n", "\n")
    new_string = new_string.replace("\r\n", "\n")

    def check(count: int) -> None:
        if count > 1 and not allow_multiple:
            raise EditError(f"old_string matches {count} locations; add more context to make it unique or set allow_multiple=true")

    # Tier 1: exact substring.
    count = content.count(old_string)
    if count:
        check(count)
        return content.replace(old_string, new_string), count, "exact"

    # Tier 2: line-by-line, ignoring leading/trailing whitespace; re-indent new_string.
    lines = content.split("\n")
    old_lines = old_string.strip("\n").split("\n")
    stripped_old = [l.strip() for l in old_lines]
    n = len(old_lines)
    starts = [i for i in range(len(lines) - n + 1) if [l.strip() for l in lines[i:i + n]] == stripped_old]
    if starts:
        check(len(starts))
        for i in reversed(starts):
            replacement = _reindent(new_string.strip("\n"), old_lines[0], lines[i]).split("\n")
            lines[i:i + n] = replacement
        return "\n".join(lines), len(starts), "flexible"

    # Tier 3: tokens split around code delimiters, joined by flexible whitespace.
    tokens = [t for t in re.split(rf"(\s+|[{_DELIMS}])", old_string) if t and not t.isspace()]
    if tokens:
        pattern = re.compile(r"\s*".join(re.escape(t) for t in tokens))
        matches = list(pattern.finditer(content))
        if matches:
            check(len(matches))
            for m in reversed(matches):
                content = content[: m.start()] + new_string + content[m.end():]
            return content, len(matches), "regex"

    raise EditError("old_string not found in file (tried exact, whitespace-flexible, and regex matching)")


# --- graph data ---------------------------------------------------------------


class CodeGraph:
    """Pre-computed AST/call graph and node embeddings for one repository snapshot."""

    def __init__(self, graph_path: Path, embeddings_path: Path | None) -> None:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
        self.nodes: dict[str, dict] = {n["id"]: n for n in data.get("nodes", [])}
        self.out_edges: dict[str, list[tuple[str, str]]] = {}
        self.in_edges: dict[str, list[tuple[str, str]]] = {}
        for e in data.get("edges", []):
            self.out_edges.setdefault(e["source"], []).append((e["target"], e.get("type", "")))
            self.in_edges.setdefault(e["target"], []).append((e["source"], e.get("type", "")))
        self.vectors: dict[str, np.ndarray] = {}
        if embeddings_path and embeddings_path.is_file() and embeddings_path.stat().st_size > 100:
            with np.load(embeddings_path) as npz:
                self.vectors = {k: npz[k].astype(np.float32) for k in npz.files}

    @classmethod
    def load(cls, data_dir: Path, instance_id: str) -> "CodeGraph | None":
        graph = data_dir / "graphs" / f"{instance_id}.json"
        if not graph.is_file() or graph.stat().st_size <= 100:
            return None
        return cls(graph, data_dir / "embeddings" / f"{instance_id}.npz")

    def resolve(self, name: str, candidates: Any = None) -> str | None:
        """4-tier resolution: exact -> '.'/'/' suffix -> case-insensitive -> substring."""
        keys = list(candidates if candidates is not None else self.nodes)
        if name in keys:
            return name
        for tier in (
            lambda k: k.endswith("." + name) or k.endswith("/" + name),
            lambda k: k.lower() == name.lower(),
            lambda k: name.lower() in k.lower(),
        ):
            hits = sorted((k for k in keys if tier(k)), key=len)
            if hits:
                return hits[0]
        return None


# --- the tool context -------------------------------------------------------


@dataclass
class ToolContext:
    sandbox: Sandbox
    budget: Budget
    graph: CodeGraph | None = None
    tool_calls_used: int = 0
    patch_submitted: bool = False
    submitted_patch: str = ""
    agent_start_time: float | None = None
    calls: list[dict] = field(default_factory=list)

    def start_agent_session(self) -> None:
        self.agent_start_time = time.monotonic()

    def elapsed(self) -> float:
        return 0.0 if self.agent_start_time is None else time.monotonic() - self.agent_start_time

    def remaining_seconds(self) -> float:
        return self.budget.time_minutes * 60 - self.elapsed()

    def remaining_tool_calls(self) -> int | None:
        return None if self.budget.tool_calls is None else self.budget.tool_calls - self.tool_calls_used

    def out_of_budget(self) -> str | None:
        if self.remaining_seconds() <= 0:
            return "time budget exhausted"
        remaining = self.remaining_tool_calls()
        if remaining is not None and remaining <= 0:
            return "tool call budget exhausted"
        return None

    def current_patch(self) -> str:
        res = self.sandbox.exec("git add -N . && git diff HEAD", timeout=120)
        return res.stdout if res.exit_code == 0 else ""

    def create_tools(self) -> dict[str, Callable[..., str]]:
        return {f.__name__: f for f in _make_tools(self)}


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _make_tools(ctx: ToolContext) -> list[Callable[..., str]]:
    b = ctx.budget

    def budget_gated(count_tool_call: bool = True) -> Callable[[Callable[..., str]], Callable[..., str]]:
        def deco(fn: Callable[..., str]) -> Callable[..., str]:
            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> str:
                reason = ctx.out_of_budget()
                exhausted_time = ctx.remaining_seconds() <= 0
                if reason and (count_tool_call or exhausted_time):
                    out = _error("BudgetExceeded", f"{reason}; call submit_patch now" if not exhausted_time else reason)
                else:
                    if count_tool_call:
                        ctx.tool_calls_used += 1
                    try:
                        out = fn(*args, **kwargs)
                    except (ValueError, EditError) as exc:
                        out = _error("ValidationError" if isinstance(exc, ValueError) else "FileEditError", str(exc))
                    remaining = ctx.remaining_tool_calls()
                    if b.tool_calls is not None and b.tool_calls >= 20 and remaining is not None and remaining <= 10:
                        payload = json.loads(out)
                        payload["budget_warning"] = (
                            f"Only {remaining} tool call(s) remaining ({ctx.tool_calls_used}/{b.tool_calls} used). "
                            "Finalize your edits and call submit_patch soon."
                        )
                        out = json.dumps(payload)
                ctx.calls.append({"tool": fn.__name__, "args": {**dict(zip(fn.__code__.co_varnames, args)), **kwargs},
                                  "t": round(ctx.elapsed(), 1), "result_chars": len(out)})
                return out
            return wrapper
        return deco

    @budget_gated()
    def run_command(command: str) -> str:
        """Execute a shell command with /bin/bash -c inside /workspace and return its stdout, stderr and exit code.

        Args:
            command: The shell command to run.
        """
        timeout = int(min(b.command_timeout_seconds, max(5, ctx.remaining_seconds())))
        res = ctx.sandbox.exec(command, timeout=timeout)
        if res.timed_out:
            return _error("TimeoutExceeded", f"Command timed out after {timeout} seconds",
                          stdout=_truncate(res.stdout, b.max_stdout_chars), stderr=_truncate(res.stderr, b.max_stdout_chars))
        if res.exit_code == 0:
            return _ok(stdout=_truncate(res.stdout, b.max_stdout_chars), exit_code=0)
        return _error("CommandError", f"Command failed with exit code {res.exit_code}",
                      stdout=_truncate(res.stdout, b.max_stdout_chars), stderr=_truncate(res.stderr, b.max_stdout_chars),
                      exit_code=res.exit_code)

    @budget_gated(count_tool_call=False)
    def submit_patch() -> str:
        """Submit your changes: captures `git diff HEAD` of /workspace (including new files) as the final patch. Call this last, after verifying your fix."""
        patch = ctx.current_patch()
        ctx.submitted_patch = patch
        ctx.patch_submitted = True
        files = len(re.findall(r"^diff --git ", patch, flags=re.M))
        return _ok(patch_size=len(patch), files_changed=files)

    def get_status() -> str:
        """Return live budget consumption (tool calls and time remaining) and patch status. Free: does not count toward the tool call budget."""
        remaining = ctx.remaining_tool_calls()
        return json.dumps({
            "tool_calls_used": ctx.tool_calls_used,
            "patch_submitted": ctx.patch_submitted,
            "patch_size": len(ctx.submitted_patch),
            "tool_calls_remaining": remaining,
            "max_tool_calls": b.tool_calls,
            "time_seconds_remaining": round(max(0.0, ctx.remaining_seconds()), 1),
            "max_time_minutes": b.time_minutes,
            "agent_elapsed_seconds": round(ctx.elapsed(), 1),
            "max_turns": b.turns,
            "command_timeout_seconds": b.command_timeout_seconds,
        })

    def _read(rel: str) -> str:
        res = ctx.sandbox.run("cat", "--", rel, timeout=60)
        if res.exit_code != 0:
            raise ValueError(f"cannot read {rel}: {res.stderr.strip()[:300]}")
        return res.stdout

    @budget_gated()
    def read_file(filepath: str, start_line: int | None = None, end_line: int | None = None) -> str:
        """Read a file under /workspace, optionally a 1-indexed inclusive line range. Returns at most 150 lines / 10000 characters per call.

        Args:
            filepath: Path relative to /workspace (a leading /workspace/ is accepted).
            start_line: First line to return (1-indexed). Defaults to 1.
            end_line: Last line to return (inclusive). Defaults to the end of the file.
        """
        rel = resolve_workspace_path(filepath)
        lines = _read(rel).split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        total = len(lines)
        start = max(1, int(start_line or 1))
        end = min(total, int(end_line)) if end_line else total
        if start > max(total, 1):
            raise ValueError(f"start_line {start} is beyond the end of the file ({total} lines)")
        out, chars, truncated = [], 0, False
        for i in range(start, end + 1):
            line = lines[i - 1]
            if len(out) >= b.max_file_lines or chars + len(line) + 1 > b.max_file_chars:
                truncated = True
                break
            out.append(line)
            chars += len(line) + 1
        return _ok(filepath=rel, content="\n".join(out), start_line=start, end_line=start + len(out) - 1,
                   total_lines=total, is_truncated=truncated)

    @budget_gated()
    def edit_file(filepath: str, old_string: str, new_string: str, allow_multiple: bool = False) -> str:
        """Replace old_string with new_string in an existing file under /workspace. old_string must match uniquely unless allow_multiple is true; matching tolerates whitespace/indentation differences.

        Args:
            filepath: Path relative to /workspace.
            old_string: Existing text to replace (copy it from read_file output).
            new_string: Replacement text.
            allow_multiple: Replace every occurrence instead of requiring a unique match.
        """
        rel = resolve_workspace_path(filepath)
        try:
            content = _read(rel)
        except ValueError as exc:
            raise EditError(str(exc)) from exc
        if not content:
            raise EditError(f"{rel} is empty; use write_file")
        new_content, count, strategy = apply_replacement(content, old_string, new_string, allow_multiple)
        ctx.sandbox.put_file(f"/workspace/{rel}", new_content.encode("utf-8"))
        diff = "".join(difflib.unified_diff(content.replace("\r\n", "\n").splitlines(True), new_content.splitlines(True),
                                            f"a/{rel}", f"b/{rel}"))
        return _ok(filepath=rel, occurrences=count, strategy=strategy, diff=_truncate(diff, b.max_stdout_chars),
                   is_truncated=len(diff) > b.max_stdout_chars)

    @budget_gated()
    def write_file(filepath: str, content: str) -> str:
        """Create or overwrite a file under /workspace (parent directories are created).

        Args:
            filepath: Path relative to /workspace.
            content: Full file content.
        """
        rel = resolve_workspace_path(filepath)
        data = content.encode("utf-8")
        ctx.sandbox.put_file(f"/workspace/{rel}", data)
        return _ok(filepath=rel, size=len(data))

    def _need_graph() -> CodeGraph:
        if ctx.graph is None:
            raise ValueError("no code graph is available for this repository")
        return ctx.graph

    @budget_gated()
    def get_code_neighbors(node: str, edge_type: str | None = None, max_neighbors: int = 50) -> str:
        """Find callers, callees, and definitions related to a code symbol in the repository graph.

        Args:
            node: Symbol name, e.g. "FastAPI.get" or "Request" (short names are resolved).
            edge_type: Optional edge type filter such as "CALLS", "DEFINED_IN", or "IMPORTS".
            max_neighbors: Maximum number of neighbors to return.
        """
        g = _need_graph()
        resolved = g.resolve(node)
        if resolved is None:
            raise ValueError(f"symbol not found in code graph: {node}")
        want = edge_type.lower() if edge_type else None
        neighbors = [
            {"node": other, "edge": etype, "direction": direction}
            for direction, edges in (("out", g.out_edges.get(resolved, [])), ("in", g.in_edges.get(resolved, [])))
            for other, etype in edges
            if want is None or etype.lower() == want
        ][: max(1, int(max_neighbors))]
        return _ok(node=resolved, neighbors=neighbors, count=len(neighbors))

    @budget_gated()
    def search_similar_code(query: str, k: int = 10) -> str:
        """Find the top-k code symbols most similar to a given symbol, using pre-computed embeddings. Pass a symbol name (e.g. "parse_header"), not a sentence.

        Args:
            query: A class, function, or module symbol name.
            k: Number of results.
        """
        g = _need_graph()
        if not g.vectors:
            raise ValueError("no embeddings are available for this repository")
        key = g.resolve(query, g.vectors)
        if key is None:
            raise ValueError(f"symbol not found in embeddings: {query}; pass a function/class name")
        q = g.vectors[key]
        names = list(g.vectors)
        mat = np.stack([g.vectors[n] for n in names])
        sims = mat @ q / (np.linalg.norm(mat, axis=1) * np.linalg.norm(q) + 1e-9)
        order = [i for i in np.argsort(-sims) if names[i] != key][: max(1, int(k))]
        results = [{"node_name": names[i], "code": (g.nodes.get(names[i], {}).get("text") or "")[:1500],
                    "similarity": round(float(sims[i]), 4)} for i in order]
        return _ok(query=key, results=results, count=len(results))

    @budget_gated()
    def get_code_subgraph(nodes: list[str]) -> str:
        """Get the induced subgraph (nodes and the edges between them) for a list of code symbols.

        Args:
            nodes: Symbol names.
        """
        g = _need_graph()
        resolved = [r for r in (g.resolve(n) for n in nodes) if r]
        wanted = set(resolved)
        edges = [{"from": s, "to": t, "type": etype} for s in resolved for t, etype in g.out_edges.get(s, []) if t in wanted]
        return _ok(nodes=resolved, edges=edges, node_count=len(resolved), edge_count=len(edges))

    return [run_command, submit_patch, get_status, read_file, edit_file, write_file,
            get_code_neighbors, search_similar_code, get_code_subgraph]

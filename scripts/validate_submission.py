"""Validate (and optionally zip) a competition submission directory.

Mirrors the checks the official `adk-submission` compiler documents in
HARNESS_README.md sections 2-3 and the "Model Selection, Budget, and Harness
Rules" page. The official code is not public, so this is a best-effort replica:
a pass here does not guarantee acceptance, but a failure here almost certainly
means a wasted daily submission.

Usage:
    python scripts/validate_submission.py submission/
    python scripts/validate_submission.py submission/ --zip dist/submission.zip
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

ALLOWED_MODEL = "gemma-4-31b-it-qat-w4a16-ct"
PROVIDER_PREFIXES = ("openai/", "google/", "hosted_vllm/", "custom/")
ROOT_CONFIG_NAMES = ("agent.yaml", "agent.yml", "root_agent.yaml", "root_agent.yml")
ALLOWED_EXTENSIONS = {".yaml", ".yml", ".md", ".txt", ".py", ".json", ".safetensors"}
HARNESS_TOOLS = {
    "run_command", "submit_patch", "get_status", "read_file", "edit_file",
    "write_file", "get_code_neighbors", "search_similar_code", "get_code_subgraph",
}
AGENT_CLASSES = {"LlmAgent", "SequentialAgent", "ParallelAgent", "LoopAgent"}
FORBIDDEN_GEN_FIELDS = {"tools", "system_instruction", "http_options", "safety_settings", "response_schema"}
THINKING_LEVELS = {"MINIMAL", "LOW", "MEDIUM", "HIGH", "NONE"}
# ADK substitutes `{name}` in instructions from session state and raises KeyError
# when the key is missing, which would error every task. The harness always sets
# `problem_description`; `hints` only exists when a task has hints, so it must be
# written `{hints?}`. Double braces do not escape (ADK's pattern is `{+[^{}]*}+`).
STATE_PLACEHOLDER = re.compile(r"{+([^{}]*)}+")
STATE_NAME = re.compile(r"^(?:app:|user:|temp:)?[A-Za-z_][A-Za-z0-9_]*$")
ALWAYS_IN_STATE = {"problem_description"}

MAX_TOTAL_BYTES = 3 * 1024**3
MAX_FILES = 10_000
MAX_INCLUDE_DEPTH = 10
MAX_INSTRUCTION_CHARS = 1_000_000
MAX_TOTAL_INSTRUCTION_CHARS = 10_000_000
MAX_TOKENS = 32_768
MAX_LORA_RANK = 128


class SubmissionError(Exception):
    pass


def _safe_join(root: Path, base_dir: Path, rel: str) -> Path:
    """Resolve `rel` against `base_dir`, refusing anything that escapes `root`."""
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        raise SubmissionError(f"invalid path {rel!r}")
    posix = PurePosixPath(rel)
    if posix.is_absolute() or ".." in posix.parts:
        raise SubmissionError(f"path traversal or absolute path not allowed: {rel!r}")
    target = (base_dir / rel).resolve()
    if not target.is_relative_to(root.resolve()):
        raise SubmissionError(f"path escapes submission root: {rel!r}")
    return target


def load_yaml(path: Path, root: Path, _stack: tuple[Path, ...] = ()) -> Any:
    """Load YAML with the harness's sandboxed `!include` semantics.

    Includes resolve relative to the including file's directory; .md/.txt are
    inlined as strings, .yaml/.yml are parsed recursively.
    """
    path = path.resolve()
    if path in _stack:
        raise SubmissionError(f"!include cycle: {' -> '.join(p.name for p in (*_stack, path))}")
    if len(_stack) > MAX_INCLUDE_DEPTH:
        raise SubmissionError(f"!include depth exceeds {MAX_INCLUDE_DEPTH} at {path.name}")

    class Loader(yaml.SafeLoader):
        pass

    def include(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
        target = _safe_join(root, path.parent, loader.construct_scalar(node))
        if not target.is_file():
            raise SubmissionError(f"!include target not found: {node.value!r} (from {path.relative_to(root.resolve())})")
        if target.suffix in (".md", ".txt"):
            return target.read_text(encoding="utf-8")
        if target.suffix in (".yaml", ".yml"):
            return load_yaml(target, root, (*_stack, path))
        raise SubmissionError(f"!include only supports .md/.txt/.yaml/.yml: {node.value!r}")

    Loader.add_constructor("!include", include)
    with path.open(encoding="utf-8") as fh:
        return yaml.load(fh, Loader=Loader)  # noqa: S506 - SafeLoader subclass


def normalize_model_name(name: str) -> str:
    for prefix in PROVIDER_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _check_files(root: Path, errors: list[str]) -> None:
    total = count = 0
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if p.is_symlink():
            errors.append(f"symlinks are not allowed: {rel}")
            continue
        if p.is_dir():
            continue
        count += 1
        total += p.stat().st_size
        if p.suffix.lower() not in ALLOWED_EXTENSIONS:
            errors.append(f"disallowed file extension: {rel}")
    if count > MAX_FILES:
        errors.append(f"too many files: {count} > {MAX_FILES}")
    if total >= MAX_TOTAL_BYTES:
        errors.append(f"submission too large: {total} bytes >= {MAX_TOTAL_BYTES}")


def _check_generation_config(cfg: Any, where: str, errors: list[str]) -> None:
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        errors.append(f"{where}: generate_content_config must be a mapping")
        return
    for field in FORBIDDEN_GEN_FIELDS & cfg.keys():
        errors.append(f"{where}: generate_content_config.{field} is not allowed")

    def bounded(key: str, value: Any, lo: float, hi: float | None) -> None:
        if value is None:
            return
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < lo or (hi is not None and value > hi):
            errors.append(f"{where}: {key}={value!r} out of range [{lo}, {hi if hi is not None else 'inf'}]")

    bounded("max_output_tokens", cfg.get("max_output_tokens"), 1, MAX_TOKENS)
    bounded("temperature", cfg.get("temperature"), 0.0, None)
    bounded("top_p", cfg.get("top_p"), 0.0, 1.0)
    bounded("top_k", cfg.get("top_k"), 1, None)
    thinking = cfg.get("thinking_config")
    if thinking is not None:
        if not isinstance(thinking, dict):
            errors.append(f"{where}: thinking_config must be a mapping")
            return
        bounded("thinking_budget", thinking.get("thinking_budget"), 1, MAX_TOKENS)
        level = thinking.get("thinking_level")
        if level is not None and str(level).upper() not in THINKING_LEVELS:
            errors.append(f"{where}: thinking_level={level!r} not in {sorted(THINKING_LEVELS)}")


def _check_adapter(root: Path, name: Any, where: str, errors: list[str]) -> None:
    if not isinstance(name, str) or not name or "/" in name or name.startswith("."):
        errors.append(f"{where}: invalid adapter name {name!r}")
        return
    adir = root / "adapters" / name
    config = adir / "adapter_config.json"
    if not config.is_file() or not (adir / "adapter_model.safetensors").is_file():
        errors.append(f"{where}: adapter {name!r} needs adapters/{name}/adapter_config.json and adapter_model.safetensors")
        return
    try:
        rank = json.loads(config.read_text(encoding="utf-8")).get("r")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        errors.append(f"{where}: adapter {name!r} config is not valid JSON: {exc}")
        return
    if isinstance(rank, int) and rank > MAX_LORA_RANK:
        errors.append(f"{where}: adapter {name!r} rank {rank} > max_lora_rank {MAX_LORA_RANK}")


def _check_skill(root: Path, base_dir: Path, rel: Any, where: str, errors: list[str]) -> None:
    try:
        sdir = _safe_join(root, base_dir, rel)
    except SubmissionError as exc:
        errors.append(f"{where}: skill {exc}")
        return
    manifest = sdir / "SKILL.md"
    if not manifest.is_file():
        errors.append(f"{where}: skill {rel!r} has no SKILL.md")
        return
    text = manifest.read_text(encoding="utf-8")
    front = text.split("---", 2)[1] if text.startswith("---") and text.count("---") >= 2 else ""
    try:
        meta = yaml.safe_load(front) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict) or not meta.get("name"):
        errors.append(f"{where}: skill {rel!r} SKILL.md needs YAML frontmatter with `name:`")


class _Walker:
    """Walks the agent tree from the root config, collecting errors and models."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.errors: list[str] = []
        self.models: set[str] = set()
        self.agent_count = 0
        self.instruction_chars = 0
        self.visited: set[Path] = set()
        self.output_keys: set[str] = set()
        self.placeholders: list[tuple[str, str]] = []

    def walk_file(self, path: Path, depth: int) -> None:
        if path in self.visited:
            return
        self.visited.add(path)
        where = str(path.relative_to(self.root.resolve()))
        try:
            config = load_yaml(path, self.root)
        except (SubmissionError, yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            self.errors.append(f"{where}: {exc}")
            return
        self.walk_agent(config, path.parent, where, depth)

    def walk_ref(self, ref: Any, base_dir: Path, where: str, depth: int) -> None:
        if not isinstance(ref, dict) or "config_path" not in ref:
            # Inline agent definition.
            self.walk_agent(ref, base_dir, where, depth)
            return
        try:
            target = _safe_join(self.root, base_dir, ref["config_path"])
        except SubmissionError as exc:
            self.errors.append(f"{where}: {exc}")
            return
        if not target.is_file():
            self.errors.append(f"{where}: config_path not found: {ref['config_path']!r}")
            return
        self.walk_file(target, depth)

    def walk_agent(self, config: Any, base_dir: Path, where: str, depth: int) -> None:
        if not isinstance(config, dict):
            self.errors.append(f"{where}: agent config must be a mapping")
            return
        if depth > 50:
            self.errors.append(f"{where}: agent nesting deeper than 50")
            return
        self.agent_count += 1
        if not config.get("name"):
            self.errors.append(f"{where}: agent `name` is required")
        agent_class = config.get("agent_class", "LlmAgent")
        if agent_class not in AGENT_CLASSES:
            self.errors.append(f"{where}: unknown agent_class {agent_class!r}")
        if agent_class == "LoopAgent":
            iters = config.get("max_iterations", 500)
            if not isinstance(iters, int) or not 1 <= iters <= 500:
                self.errors.append(f"{where}: max_iterations={iters!r} must be in [1, 500]")

        if agent_class == "LlmAgent":
            model = config.get("model")
            if not isinstance(model, str) or not model:
                self.errors.append(f"{where}: LlmAgent needs a `model`")
            else:
                self.models.add(normalize_model_name(model))
            for key in ("instruction", "global_instruction", "description"):
                text = config.get(key)
                if text is not None and not isinstance(text, str):
                    self.errors.append(f"{where}: {key} must be a string")
                elif text and len(text) > MAX_INSTRUCTION_CHARS:
                    self.errors.append(f"{where}: {key} longer than {MAX_INSTRUCTION_CHARS} chars")
                elif text and key != "description":
                    self.instruction_chars += len(text)
                    for name in STATE_PLACEHOLDER.findall(text):
                        self.placeholders.append((where, name.strip()))
            if isinstance(config.get("output_key"), str):
                self.output_keys.add(config["output_key"])
            if config.get("adapter") is not None:
                _check_adapter(self.root, config["adapter"], where, self.errors)
            _check_generation_config(config.get("generate_content_config"), where, self.errors)
            for tool in config.get("tools") or []:
                if isinstance(tool, str):
                    if tool not in HARNESS_TOOLS:
                        self.errors.append(f"{where}: tool {tool!r} is not a harness tool")
                elif isinstance(tool, dict) and "agent_tool" in tool:
                    self.walk_ref(tool["agent_tool"], base_dir, where, depth + 1)
                elif isinstance(tool, dict):
                    self.walk_agent(tool, base_dir, where, depth + 1)
                else:
                    self.errors.append(f"{where}: unsupported tool entry {tool!r}")
            for skill in config.get("skills") or []:
                _check_skill(self.root, base_dir, skill, where, self.errors)

        for sub in config.get("sub_agents") or []:
            self.walk_ref(sub, base_dir, where, depth + 1)


def validate(submission: Path) -> list[str]:
    """Return a list of human-readable problems; empty means the submission looks valid."""
    root = submission
    if not root.is_dir():
        return [f"not a directory: {root}"]
    errors: list[str] = []
    _check_files(root, errors)

    roots = [root / n for n in ROOT_CONFIG_NAMES if (root / n).is_file()]
    if not roots:
        return errors + [f"missing root config (one of {', '.join(ROOT_CONFIG_NAMES)})"]
    if len(roots) > 1:
        return errors + [f"multiple root configs: {', '.join(p.name for p in roots)}"]

    walker = _Walker(root)
    walker.walk_file(roots[0].resolve(), depth=0)
    errors += walker.errors
    for where, name in walker.placeholders:
        if name.endswith("?") or name.startswith("artifact.") or not STATE_NAME.match(name):
            continue  # optional, artifact, or not a state name (ADK leaves those as-is)
        if name not in ALWAYS_IN_STATE | walker.output_keys:
            errors.append(f"{where}: instruction placeholder {{{name}}} is not in session state "
                          f"(use {{{name}?}} if optional)")
    if walker.agent_count > 500:
        errors.append(f"too many agents: {walker.agent_count} > 500")
    if walker.instruction_chars > MAX_TOTAL_INSTRUCTION_CHARS:
        errors.append(f"total instruction chars {walker.instruction_chars} > {MAX_TOTAL_INSTRUCTION_CHARS}")
    if len(walker.models) > 1:
        errors.append(f"single-model rule violated: {sorted(walker.models)}")
    elif walker.models and walker.models != {ALLOWED_MODEL}:
        errors.append(f"only {ALLOWED_MODEL} is supported, got {sorted(walker.models)}")
    return errors


def write_zip(submission: Path, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in submission.rglob("*") if p.is_file() and not p.is_symlink())
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, p.relative_to(submission).as_posix())
    return len(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("submission", type=Path)
    parser.add_argument("--zip", type=Path, help="write a submission zip here if validation passes")
    args = parser.parse_args(argv)

    errors = validate(args.submission)
    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    if errors:
        return 1
    print(f"OK: {args.submission}")
    if args.zip:
        n = write_zip(args.submission, args.zip)
        print(f"wrote {args.zip} ({n} files, {args.zip.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

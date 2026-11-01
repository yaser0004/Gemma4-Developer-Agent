"""One-call localization: where the issue's names live, the code around them, and the tests to run.

Usage: scout.py TERM [TERM ...]
  TERM: an identifier (Console, parse_header, Session.send), a file path, or a quoted phrase from
        the issue or an error message. Free text also works; identifiers are pulled out of it.

Prints the best-matching source files, the enclosing function/class of the strongest hit in the top
files (line-numbered, ready to copy into edit_file), and the test files to run.
"""

import collections
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath

MAX_TERMS = 12
MAX_OUTPUT = 3000
SNIPPET_FILES = 2
SNIPPET_LINES = 22
COMMON_FILES = 40  # a term found in more files than this says little about where the bug is

TEST_PATH = re.compile(r"(^|/)(tests?/|testing/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")
DOC_PATH = re.compile(r"(^|/)(docs?|docs_src|examples?|benchmarks?|scripts)/")
DEF_LINE = re.compile(r"^(\s*)(?:async\s+def|def|class)\s+(\w+)")
STOPWORDS = {
    "self", "cls", "none", "true", "false", "return", "import", "from", "class", "def", "print",
    "str", "int", "dict", "list", "set", "tuple", "bool", "float", "object", "type", "len", "args",
    "kwargs", "init", "main", "test", "tests", "value", "data", "name", "the", "and", "for", "with",
    "this", "that", "python", "pip", "error", "issue", "bug", "fix", "feature", "example",
}
# Plain English that carries no location signal; issue titles are often nothing else.
FILLER = STOPWORDS | {
    "when", "what", "which", "where", "there", "their", "these", "those", "then", "than", "into",
    "have", "has", "had", "should", "would", "could", "will", "does", "doesn", "about", "after",
    "before", "other", "also", "only", "used", "using", "uses", "because", "fixes", "fixed",
    "fixing", "change", "changes", "changed", "added", "adds", "adding", "docs", "documentation",
    "features", "checklist", "description", "default", "values", "case", "cases", "make", "makes",
    "made", "need", "needs", "work", "works", "like", "just", "still", "even", "being", "been",
    "were", "here", "very", "most", "each", "such", "same", "both", "many", "much", "please",
    "thanks", "version", "release", "code", "line", "lines", "file", "files", "call", "calls",
    "called", "returns", "method", "function", "errors", "bugs", "support", "allow", "allows",
    "instead", "currently", "expected", "actual", "behavior", "behaviour", "problem", "update",
    "updated", "following", "some", "more", "them", "they", "your", "with", "from", "into",
    "without", "while", "within", "correct", "correctly", "properly", "handle", "handling",
    "wrong", "missing", "always", "never", "already", "type", "tests", "test", "manually",
    "tested", "accept", "review", "pedantic", "args", "black", "changelog", "contributors",
}
# Module names too generic for "the term names this module" to mean anything.
GENERIC_STEMS = {"util", "utils", "base", "core", "main", "json", "init", "types", "common", "helper",
                 "helpers", "compat", "models", "model", "misc", "tools", "config", "constants"}
NON_CODE_EXT = {"md", "rst", "txt", "com", "org", "io", "net", "dev", "html", "htm", "yml", "yaml",
                "toml", "cfg", "ini", "json", "c", "h", "js", "css", "png", "svg", "lock"}


def repo_root() -> str:
    # The skill runs from a temp dir. bash sets PWD to the real workspace, which on the Kaggle
    # subprocess sandbox is not literally /workspace, so a hard-coded path would search nothing.
    for candidate in (os.environ.get("PWD"), *os.environ.get("PYTHONPATH", "").split(os.pathsep), "/workspace"):
        if candidate and os.path.isdir(os.path.join(candidate, ".git")):
            return candidate
    return os.environ.get("PWD") or "/workspace"


ROOT = repo_root()
# Untracked files count (new modules the agent created); outside a repo, search the plain tree.
SCOPE = "--untracked" if os.path.isdir(os.path.join(ROOT, ".git")) else "--no-index"


def grep(*args: str) -> list[str]:
    try:
        # Exclude the skill runner's temp file: it embeds this script and its arguments.
        res = subprocess.run(["git", "grep", "-n", "-I", SCOPE, *args, "--", "*.py", ":(exclude).adk_exec_*"],
                             cwd=ROOT, capture_output=True, text=True, check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return res.stdout.splitlines()


def extract_terms(text: str) -> tuple[list[str], list[str]]:
    """(names, words): identifiers, dotted names, paths and quoted phrases worth searching for, and
    the plain content words of free text, which are weaker evidence."""
    text = text.strip()
    if not text:
        return [], []
    if " " not in text:
        return [text.strip("`'\"()[]{}:,;")], []
    # PR templates and links are noise: drop HTML comments, URLs and checklist lines.
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", " ", text)
    text = "\n".join(line for line in text.splitlines() if not re.match(r"\s*[-*]\s*\[[ xX]\]", line))
    terms = re.findall(r"`([^`\n]{2,80})`", text)
    terms += re.findall(r"[\"']([^\"'\n]{8,80})[\"']", text)
    terms += re.findall(r"\b[\w/.-]+\.py\b", text)
    terms += re.findall(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b", text)
    terms += re.findall(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b", text)
    terms += re.findall(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b", text)
    words = [w for w in re.findall(r"\b[A-Za-z]{4,}\b", text) if w.lower() not in FILLER]
    return terms, words


def normalize(term: str) -> str | None:
    term = term.strip().strip("`'\"").rstrip("()")
    if len(term) < 3 or term.lower() in STOPWORDS:
        return None
    if " " not in term and "." in term and term.rsplit(".", 1)[1].lower() in NON_CODE_EXT:
        return None  # CHANGELOG.md, github.com and friends
    return term


def search(term: str, plain: bool = False) -> tuple[dict[str, list[tuple[int, bool]]], str]:
    """Hits per file as (line, is_definition). Returns the search key that was used.
    Plain words from prose are matched case-insensitively (Table/table)."""
    if term.endswith(".py") and " " not in term:
        path = term.lstrip("./")
        if os.path.isfile(os.path.join(ROOT, path)):
            return {path: [(1, True)]}, path
        term = PurePosixPath(path).stem
    owner, _, name = term.rpartition(".") if " " not in term else ("", "", term)
    identifier = re.fullmatch(r"[A-Za-z_]\w*", name) is not None
    flags = ["-i"] if plain else []
    lines = grep("-w", "-F", *flags, name) if identifier else grep("-F", *flags, term)
    hits: dict[str, list[tuple[int, bool]]] = collections.defaultdict(list)
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        path, lineno, text = parts
        is_def = identifier and bool(re.match(
            rf"\s*((async\s+)?def|class)\s+{re.escape(name)}\b|\s*{re.escape(name)}\s*(:[^=]*)?=[^=]", text,
            re.I if plain else 0))
        hits[path].append((int(lineno), is_def))
    if owner:
        # Class.method: prefer files that also mention the owner.
        owner_files = {line.split(":", 1)[0] for line in grep("-w", "-F", owner)}
        for path in list(hits):
            if path not in owner_files:
                hits[path] = [(n, False) for n, _ in hits[path]]
    return hits, name if identifier else term


def rank(terms: list[str], words: list[str] = ()):
    scores: collections.Counter = collections.Counter()
    best_line: dict[str, tuple[float, int]] = {}
    matched: dict[str, list[str]] = collections.defaultdict(list)
    for term, plain in [(t, False) for t in terms] + [(w, True) for w in words]:
        hits, key = search(term, plain)
        weight = (0.4 if plain else 1.0) * (0.3 if len(hits) > COMMON_FILES else 1.0)
        for path, found in hits.items():
            defs = [n for n, is_def in found if is_def]
            score = weight * (4.0 if defs else 1.0 + 0.25 * min(len(found) - 1, 4))
            stem = PurePosixPath(path).stem.lower().lstrip("_")
            if stem not in GENERIC_STEMS and len(stem) >= 4 and key.lower().rstrip("s") in (stem, stem.rstrip("s")):
                score += 3.0 * weight  # the term names the module itself (markdown -> markdown.py)
            if TEST_PATH.search(path):
                score *= 0.3
            elif DOC_PATH.search(path):
                score *= 0.5
            scores[path] += score
            matched[path].append(key)
            line = defs[0] if defs else found[0][0]
            if score > best_line.get(path, (0.0, 0))[0]:
                best_line[path] = (score, line)
    return scores, best_line, matched


def snippet(path: str, line: int) -> list[str]:
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8", errors="replace") as fh:
            src = fh.read().splitlines()
    except OSError:
        return []
    start = min(max(0, line - 1), max(0, len(src) - 1))
    if not src:
        return []
    # Walk up to the enclosing def/class so the model sees the whole unit it will edit.
    hit_indent = len(src[start]) - len(src[start].lstrip())
    if hit_indent and not DEF_LINE.match(src[start]):
        for i in range(start - 1, max(-1, start - 80), -1):
            m = DEF_LINE.match(src[i])
            if m and len(m.group(1)) < hit_indent:
                start = i
                break
    if line - 1 - start > SNIPPET_LINES - 6:
        start = max(0, line - 1 - 6)  # the hit is deep inside a long body: centre on it instead
    return [f"{n + 1:5d}  {src[n][:110]}" for n in range(start, min(len(src), start + SNIPPET_LINES))]


def related_tests(path: str, terms: list[str]) -> list[str]:
    stem = PurePosixPath(path).stem
    if stem == "__init__":
        stem = PurePosixPath(path).parent.name
    if not stem:
        return []
    counts: collections.Counter = collections.Counter()
    for line in grep("-w", "-F", stem):
        test = line.split(":", 1)[0]
        if TEST_PATH.search(test) and not test.endswith("conftest.py"):
            counts[test] += 1
    for test in list(counts):
        if stem in PurePosixPath(test).stem:
            counts[test] += 50
    keys = {t.rpartition(".")[2] for t in terms}
    for line in grep("-w", "-F", *[a for k in keys for a in ("-e", k)]) if keys else []:
        test = line.split(":", 1)[0]
        if test in counts:
            counts[test] += 2
    return [t for t, _ in counts.most_common(3)]


def main() -> int:
    # run_skill_script passes object-form args as `--key value`; the option names
    # carry no meaning here, only their values do.
    raw = [a for a in sys.argv[1:] if a.strip() and not re.fullmatch(r"--[\w-]+", a)]
    if not raw:
        print(__doc__)
        return 0
    terms: list[str] = []
    words: list[str] = []
    for arg in raw:
        names, prose = extract_terms(arg)
        for term in names:
            term = normalize(term)
            if term and term not in terms:
                terms.append(term)
        words += [w for w in prose if w.lower() not in {x.lower() for x in words}]
    terms = terms[:MAX_TERMS]
    lowered = {t.lower() for t in terms}
    words = [w for w in words if w.lower() not in lowered][:MAX_TERMS - len(terms) // 2]
    if not terms and not words:
        print("No searchable names in the arguments. Pass identifiers, file paths or quoted error text.")
        return 0

    scores, best_line, matched = rank(terms, words)
    out = [f"scout: {', '.join(terms + words)}"]
    source = [p for p, _ in scores.most_common() if not TEST_PATH.search(p)][:5]
    if not source:
        out.append("No matches in source files. Try other names from the issue, or git grep a phrase.")
        print("\n".join(out))
        return 0
    out.append("Best source files (score: matched terms):")
    for path in source:
        out.append(f"  {path}  ({scores[path]:.1f}: {', '.join(sorted(set(matched[path])))})")
    for path in source[:SNIPPET_FILES]:
        out.append(f"\n--- {path} ---")
        out.extend(snippet(path, best_line[path][1]))
    tests = related_tests(source[0], terms)
    if tests:
        out.append("\nTests for " + source[0] + ": " + ", ".join(tests))
        out.append(f"Run: python -m pytest -q -x {tests[0]}")
    text = "\n".join(out)
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT].rsplit("\n", 1)[0] + "\n[truncated]"
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

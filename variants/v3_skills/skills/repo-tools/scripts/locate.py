"""Find where identifiers or error messages from an issue live in the repository.

For each term: definitions (def/class/assignment) with file:line, then the files
that mention it most. Test files are listed separately. Output stays short so it
does not flood the context.

Usage: locate.py TERM [TERM ...]
  TERM: an identifier (Console, parse_header, Session.send) or a quoted phrase
        from an error message ("maximum recursion depth").
"""

import collections
import re
import subprocess
import sys

WORKSPACE = "/workspace"
MAX_DEFS = 8
MAX_FILES = 8
TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")


def grep(*args: str) -> list[str]:
    res = subprocess.run(["git", "grep", "-n", "-I", *args, "--", "*.py"], cwd=WORKSPACE,
                         capture_output=True, text=True, check=False)
    return res.stdout.splitlines()


def report(term: str) -> None:
    # "Class.method" -> search the last component; keep the owner as a ranking hint.
    owner, _, name = term.rpartition(".") if "." in term and " " not in term else ("", "", term)
    is_identifier = re.fullmatch(r"[A-Za-z_]\w*", name) is not None
    print(f"== {term}")

    if is_identifier:
        defs = grep("-E", rf"^\s*(async\s+def|def|class)\s+{re.escape(name)}\b|^\s*{re.escape(name)}\s*(:[^=]*)?=[^=]")
        defs.sort(key=lambda l: (TEST_PATH.search(l.split(":", 1)[0]) is not None, owner.lower() not in l.lower()))
        for line in defs[:MAX_DEFS]:
            print(f"  def  {line[:160]}")
        if len(defs) > MAX_DEFS:
            print(f"  ... {len(defs) - MAX_DEFS} more definitions")
        hits = grep("-w", "-F", name)
    else:
        hits = grep("-F", term)

    per_file = collections.Counter(line.split(":", 1)[0] for line in hits)
    if not per_file:
        print("  no matches")
        return
    source = [(f, n) for f, n in per_file.most_common() if not TEST_PATH.search(f)]
    tests = [(f, n) for f, n in per_file.most_common() if TEST_PATH.search(f)]
    if source:
        print("  source files: " + ", ".join(f"{f} ({n})" for f, n in source[:MAX_FILES]))
    if tests:
        print("  test files:   " + ", ".join(f"{f} ({n})" for f, n in tests[:MAX_FILES]))
    if not is_identifier:
        for line in hits[:5]:
            print(f"  hit  {line[:160]}")


def main() -> int:
    terms = [t for t in sys.argv[1:] if t.strip()]
    if not terms:
        print(__doc__)
        return 2
    for term in terms[:10]:
        report(term.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())

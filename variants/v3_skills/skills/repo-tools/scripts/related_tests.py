"""List the existing tests that exercise a source file, and how to run them.

Usage: related_tests.py SOURCE_PATH [SYMBOL]
  SOURCE_PATH: file you are changing, e.g. src/requests/utils.py
  SYMBOL:      optional function/class name to find the most relevant test functions
"""

import collections
import re
import subprocess
import sys
from pathlib import PurePosixPath

WORKSPACE = "/workspace"
TEST_GLOBS = ["*test*.py"]


def grep(*args: str, paths: list[str] = TEST_GLOBS) -> list[str]:
    res = subprocess.run(["git", "grep", "-I", *args, "--", *paths], cwd=WORKSPACE,
                         capture_output=True, text=True, check=False)
    return res.stdout.splitlines()


def module_names(path: str) -> list[str]:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[0] in ("src", "lib"):
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    names = [".".join(parts)] if parts else []
    if len(parts) > 1:
        names.append(".".join(parts[:-1]))  # the package, for `from pkg import name`
    return names


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1].removeprefix("/workspace/").lstrip("/")
    symbol = sys.argv[2] if len(sys.argv) > 2 else None

    counts: collections.Counter[str] = collections.Counter()
    for mod in module_names(path):
        for line in grep("-l", "-E", rf"(import|from)\s+{re.escape(mod)}\b"):
            counts[line] += 3
    stem = PurePosixPath(path).stem
    for line in grep("-l", "-w", "-F", stem):
        counts[line] += 1
    if symbol:
        for line in grep("-c", "-w", "-F", symbol):
            f, _, n = line.rpartition(":")
            counts[f] += 2 * int(n or 0)

    if not counts:
        print(f"No tests reference {path}. Look for a tests/ directory with `git ls-files | grep -i test | head`.")
        return 0
    print("Most relevant test files:")
    for f, _ in counts.most_common(6):
        print(f"  {f}")
    best = counts.most_common(1)[0][0]
    if symbol:
        funcs = grep("-n", "-E", r"def test\w*", paths=[best])
        related = grep("-n", "-w", "-F", symbol, paths=[best])[:5]
        print(f"\nLines in {best} mentioning {symbol}:")
        for line in related:
            print(f"  {line[:150]}")
        if funcs:
            print(f"({len(funcs)} test functions in that file)")
    print(f"\nRun: python -m pytest {best} -q -x" + (f" -k {symbol}" if symbol else "") + " 2>&1 | tail -20")
    return 0


if __name__ == "__main__":
    sys.exit(main())

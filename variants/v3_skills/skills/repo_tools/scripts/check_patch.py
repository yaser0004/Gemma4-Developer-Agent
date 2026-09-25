"""Audit the working tree before submit_patch.

Prints the diff stat and warns about changes that make the hidden tests fail or
pollute the patch: edited test files, harness files (pytest.ini, conftest.py),
stray scratch files, and Python files that no longer compile.

Usage: check_patch.py
"""

import py_compile
import re
import subprocess
import sys

WORKSPACE = "/workspace"
HARNESS_FILES = {"pytest.ini", "conftest.py"}
TEST_FILE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")
SCRATCH_NAME = re.compile(r"(^|/)(repro|reproduce|debug|scratch|tmp|temp|test_fix|check)[^/]*\.py$", re.I)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=WORKSPACE, capture_output=True, text=True, check=False).stdout


def main() -> int:
    status = [line for line in git("status", "--porcelain", "--untracked-files=all").splitlines() if line.strip()]
    if not status:
        print("NO CHANGES: the patch would be empty. Make your source edits before submit_patch.")
        return 0

    warnings = []
    python_files = []
    for line in status:
        code, path = line[:2], line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        name = path.rsplit("/", 1)[-1]
        if path in HARNESS_FILES:
            warnings.append(f"{path}: harness file changed; restore it with `git checkout -- {path}`")
        elif TEST_FILE.search(path):
            warnings.append(f"{path}: test file changed or added; hidden tests replace test files, so move any "
                            f"fix into source code and restore it (`git checkout -- {path}` or `rm {path}`)")
        elif code == "??" and (SCRATCH_NAME.search(path) or not path.endswith(".py")):
            warnings.append(f"{path}: untracked file will be included in the patch; delete it unless it is part "
                            f"of the fix (`rm {path}`)")
        if path.endswith(".py") and "D" not in code:
            python_files.append(path)

    for path in python_files:
        try:
            py_compile.compile(f"{WORKSPACE}/{path}", doraise=True)
        except py_compile.PyCompileError as exc:
            warnings.append(f"{path}: SYNTAX ERROR: {str(exc).strip()[:300]}")
        except FileNotFoundError:
            pass

    subprocess.run(["git", "add", "-N", "."], cwd=WORKSPACE, capture_output=True, check=False)
    print("Changed files:")
    print(git("diff", "HEAD", "--stat").rstrip() or "  (none)")
    if warnings:
        print("\nWARNINGS (fix these before submit_patch):")
        for w in warnings:
            print(f"- {w}")
    else:
        print("\nOK: only source files changed and they compile.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

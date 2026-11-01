"""Audit the working tree before submit_patch.

Prints the changed files and warns about what makes the hidden tests fail or pollutes the patch:
edited test files, harness files (pytest.ini, conftest.py), stray scratch files, and Python files
that no longer compile. Read-only: it never changes the tree or the git index.

Usage: check.py
"""

import os
import re
import subprocess
import sys

HARNESS_FILES = {"pytest.ini", "conftest.py"}
TEST_FILE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")
SCRATCH_NAME = re.compile(r"(^|/)(repro|reproduce|debug|scratch|tmp|temp|test_fix|check|verify)[^/]*$", re.I)


def repo_root() -> str:
    # Same rule as scout.py: the real workspace is PWD, not necessarily the literal /workspace.
    for candidate in (os.environ.get("PWD"), *os.environ.get("PYTHONPATH", "").split(os.pathsep), "/workspace"):
        if candidate and os.path.isdir(os.path.join(candidate, ".git")):
            return candidate
    return os.environ.get("PWD") or "/workspace"


ROOT = repo_root()


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              check=False, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def main() -> int:
    status = [line for line in git("status", "--porcelain", "--untracked-files=all").splitlines() if line.strip()]
    if not status:
        print("NO CHANGES: the patch would be empty. Edit the source before submit_patch.")
        return 0

    warnings = []
    changed = []
    for line in status:
        code, path = line[:2], line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.startswith(".adk_exec_"):
            continue  # the skill runner's own temp file, removed when this script exits
        changed.append(f"  {code} {path}")
        name = path.rsplit("/", 1)[-1]
        if name in HARNESS_FILES and "/" not in path:
            warnings.append(f"{path}: harness file changed; restore it with `git checkout -- {path}`")
        elif TEST_FILE.search(path):
            warnings.append(f"{path}: test file changed or added; the hidden tests replace test files. "
                            f"Restore it (`git checkout -- {path}` or `rm {path}`)")
        elif code == "??" and (SCRATCH_NAME.search(path) or not path.endswith(".py")):
            warnings.append(f"{path}: untracked file would be part of the patch; `rm {path}` "
                            f"unless the fix needs it")
        if path.endswith(".py") and "D" not in code:
            try:
                with open(os.path.join(ROOT, path), "rb") as fh:
                    compile(fh.read(), path, "exec")
            except SyntaxError as exc:
                warnings.append(f"{path}: SYNTAX ERROR line {exc.lineno}: {exc.msg}")
            except (OSError, ValueError):
                pass

    print("Changed files:")
    print("\n".join(changed[:30]))
    stat = git("diff", "HEAD", "--shortstat").strip()
    if stat:
        print(stat)
    if warnings:
        print("\nWARNINGS (fix before submit_patch):")
        print("\n".join(f"- {w}" for w in warnings))
    else:
        print("\nOK: only source files changed and they compile.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

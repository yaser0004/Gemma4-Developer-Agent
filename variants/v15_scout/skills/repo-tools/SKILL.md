---
name: repo-tools
description: Fast, low-output helpers for this repository. scout.py localizes an issue in one call (best source files, the code around the strongest hits, the tests to run); check.py audits the working tree before submit_patch.
---

# repo-tools

Run with `run_skill_script(skill_name="repo-tools", file_path="scripts/<script>", args=[...])`.

- `scripts/scout.py TERM [TERM ...]`: pass each exact identifier, `Class.method`, file path or quoted
  error text from the issue as its own list item. Prints the ranked source files with the terms they
  matched, the enclosing function or class of the strongest hit in the top two files (line-numbered),
  and the test file to run with a ready pytest command. Output stays under 3000 characters.
- `scripts/check.py` (no args): lists changed and untracked files and warns about edited tests,
  changed `pytest.ini`/`conftest.py`, stray scratch files and syntax errors. Run it right before
  `submit_patch`. It never changes files.

---
name: repo-tools
description: Fast, low-output helpers for this repository. scout.py localizes an issue in one call (best source files, the code around the strongest hits, the tests to run); check.py audits the working tree before submit_patch.
---

# repo-tools

Run scout with `run_skill_script(skill_name="repo-tools", file_path="scripts/scout.py", args={"terms": "<terms>"})`.
Always pass `args` as an object with one `terms` string, never as a list.

- `scripts/scout.py`: put every exact identifier, `Class.method`, file path and quoted error text from
  the issue into the one `terms` string, separated by spaces. Prints the ranked source files with the terms they
  matched, the enclosing function or class of the strongest hit in the top two files (line-numbered),
  and the test file to run with a ready pytest command. Output stays under 3000 characters.
- `scripts/check.py` (no args): lists changed and untracked files and warns about edited tests,
  changed `pytest.ini`/`conftest.py`, stray scratch files and syntax errors. Run it right before
  `submit_patch`. It never changes files.

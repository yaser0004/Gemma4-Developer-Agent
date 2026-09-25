---
name: repo_tools
description: Fast, low-output helpers for repository navigation and patch checking. locate.py finds definitions and files for identifiers or error text; related_tests.py finds the existing tests for a source file; check_patch.py audits the diff before submit_patch.
---

# repo_tools

Run with `run_skill_script(skill_name="repo_tools", file_path="scripts/<script>", args=[...])`.
Each script prints a short report (a few dozen lines at most).

- `scripts/locate.py TERM [TERM ...]`: for each identifier (e.g. `Console`, `Session.send`) or quoted
  error phrase, prints definitions as `file:line` and the source and test files that mention it most.
- `scripts/related_tests.py SOURCE_PATH [SYMBOL]`: ranks the existing test files for a source file and
  prints a targeted pytest command.
- `scripts/check_patch.py`: shows the changed files and warns about edited tests, changed
  `pytest.ini`/`conftest.py`, stray scratch files, and Python syntax errors. Run it right before submit_patch.

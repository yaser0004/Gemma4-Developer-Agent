"""scout.py / check.py run the way ADK runs skill scripts: from a temp dir, with the repo only
reachable through $PWD (the Kaggle subprocess sandbox does not use a literal /workspace)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "variants" / "v15_scout" / "skills" / "repo-tools" / "scripts"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def run(script: str, repo: Path, *args: str, cwd: Path | None = None) -> str:
    env = {"PATH": os.environ["PATH"], "PWD": str(repo), "HOME": str(repo)}
    res = subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=cwd or repo.parent,
                         env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return res.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    files = {
        "pkg/__init__.py": "",
        "pkg/render.py": (
            "import os\n\n\nclass Renderer:\n    def __init__(self):\n        self.width = 80\n\n"
            "    def render_table(self, rows):\n        if not rows:\n"
            "            raise ValueError('table must have rows')\n        return len(rows)\n"
        ),
        "pkg/util.py": "from pkg.render import Renderer\n\n\ndef helper():\n    return Renderer().render_table([1])\n",
        "tests/test_render.py": (
            "from pkg.render import Renderer\n\n\ndef test_render_table():\n"
            "    assert Renderer().render_table([1]) == 1\n"
        ),
        "docs/example.py": "from pkg.render import Renderer\nRenderer().render_table([2])\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "base"], cwd=root, check=True)
    return root


def test_scout_ranks_definition_first_and_shows_the_enclosing_method(repo, tmp_path):
    out = run("scout.py", repo, "render_table", cwd=tmp_path)
    files = [line.split()[0] for line in out.splitlines() if line.startswith("  ") and "(" in line]
    assert files[0] == "pkg/render.py"
    assert "def render_table(self, rows):" in out
    assert "tests/test_render.py" in out and "python -m pytest -q -x tests/test_render.py" in out


def test_scout_extracts_terms_from_free_text_and_error_messages(repo):
    out = run("scout.py", repo, "Calling `Renderer.render_table` with no rows: 'table must have rows'")
    assert "Renderer" in out.splitlines()[0]
    assert "pkg/render.py" in out


def test_scout_fails_soft(repo):
    assert "Usage" in run("scout.py", repo)
    assert "No searchable names" in run("scout.py", repo, "the a it")
    assert "No matches" in run("scout.py", repo, "definitely_not_in_repo_xyz")
    out = run("scout.py", repo, "naïve_ünïcode \"quoted 'mixed\" $(rm -rf /) ; ls")
    assert "Traceback" not in out
    assert (repo / "pkg" / "render.py").exists()


def test_scout_output_is_capped(repo):
    big = repo / "pkg" / "big.py"
    big.write_text("".join(f"def render_table_{i}():\n    render_table = {i}\n" for i in range(3000)))
    out = run("scout.py", repo, *[f"render_table_{i}" for i in range(12)])
    assert len(out) <= 3100


def test_check_flags_tests_scratch_and_syntax_errors_without_touching_the_index(repo):
    assert "NO CHANGES" in run("check.py", repo)
    (repo / "tests" / "test_render.py").write_text("x = 1\n")
    (repo / "reproduce_bug.py").write_text("print(1)\n")
    (repo / "pkg" / "render.py").write_text("def broken(:\n")
    out = run("check.py", repo)
    assert "tests/test_render.py: test file changed" in out
    assert "reproduce_bug.py: untracked file" in out
    assert "pkg/render.py: SYNTAX ERROR" in out
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert staged == ""


def test_check_ok_for_a_clean_source_edit(repo):
    (repo / "pkg" / "util.py").write_text("def helper():\n    return 2\n")
    (repo / ".adk_exec_1a2b3c4d.py").write_text("# skill runner temp file\n")
    assert "OK: only source files changed" in run("check.py", repo)

import shutil
import subprocess

import pytest

from harness.sandbox import DEFAULT_DATA_DIR, IMAGE, load_tasks
from harness.verify import patched_files, pytest_command, target_test_files, verify_task

PATCH = """\
diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1 +1 @@
-x = 1
+x = 2
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1 @@
+def test_x(): pass
diff --git a/tests/data.json b/tests/data.json
--- a/tests/data.json
+++ b/tests/data.json
@@ -1 +1 @@
-{}
+{"a": 1}
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-y = 1
"""


def test_patched_files_skips_deletions_and_strips_prefix():
    assert patched_files(PATCH) == ["pkg/mod.py", "tests/test_new.py", "tests/data.json"]


def test_targets_are_python_files_only():
    assert target_test_files(PATCH) == ["pkg/mod.py", "tests/test_new.py"]


def test_patched_files_handles_timestamps_and_duplicates():
    patch = "+++ b/a.py\t2024-01-01\n+++ b/a.py\n"
    assert patched_files(patch) == ["a.py"]


def test_pytest_command_matches_official_flags():
    cmd = pytest_command(["tests/test_a.py"])
    assert cmd.startswith("PYTHONSAFEPATH=1 python3 -m pytest 'tests/test_a.py' -p no:anyio -o timeout=0")
    assert "-o python_classes='Test* *Test' -q" in cmd


def test_unusual_test_paths_are_refused_before_any_container_starts():
    task = {"instance_id": "x", "test_patch": "+++ b/tests/it's.py\n"}
    result = verify_task(task, None)
    assert result["resolved"] is False
    assert "refusing unusual test paths" in result["error"]


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    res = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, check=False)
    return res.returncode == 0 and (DEFAULT_DATA_DIR / "snapshots" / "httpx_3672.tgz").is_file()


@pytest.mark.skipif(not _docker_ready(), reason="needs docker access, the sandbox image, and httpx_3672 snapshot")
def test_gold_resolves_and_baseline_fails():
    task = load_tasks(DEFAULT_DATA_DIR, ["httpx_3672"])[0]
    assert verify_task(task, task["patch"])["resolved"] is True
    baseline = verify_task(task, None)
    assert baseline["resolved"] is False and baseline["exit_code"] not in (None, 0)

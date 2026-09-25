"""Docker sandbox that replicates the official swegemma task container.

Follows HARNESS_README.md section 4: an offline, 4 GiB / 2 vCPU container
built from the organizers' Dockerfile.public, with the task snapshot extracted
into /workspace, dependencies installed by the organizers' sandbox/setup.py, and
a "baseline" commit so `git diff HEAD` captures only later edits.

Known divergence: the official harness streams cached per-repo site-packages
into the container before running `setup.py --fast-path`. Those caches are not
published, so after setup.py's fast path we install the repository's declared
runtime and test requirements from PyPI, limited to releases uploaded before
the task's date (see dependency_cutoff). The
container is online only during this setup; bootstrap() disconnects it and
verifies it is offline before the agent or any test runs.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

IMAGE = "swegemma-sandbox:public"
PIP_CACHE_VOLUME = "swegemma-pip-cache"
TEST_REQUIREMENT_FILES = ("requirements-tests.txt", "requirements-test.txt", "requirements-dev.txt", "test-requirements.txt")
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT.parent / "RESOURCES" / "competition_data"


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _docker(*args: str, input_bytes: bytes | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["docker", *args], input=input_bytes, capture_output=True, timeout=timeout, check=False)


def dependency_cutoff(task: dict, margin_days: int = 14) -> str:
    """Upload-date cutoff for dependency resolution: the task's creation date plus a margin
    (fixes usually merge within days of the PR/issue being opened)."""
    created = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
    return (created + timedelta(days=margin_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_tasks(data_dir: Path, ids: list[str] | None = None) -> list[dict]:
    tasks = [json.loads(line) for line in (data_dir / "tasks.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if ids is None:
        return tasks
    by_id = {t["instance_id"]: t for t in tasks}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise KeyError(f"unknown task ids: {missing}")
    return [by_id[i] for i in ids]


def read_split(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_image(data_dir: Path = DEFAULT_DATA_DIR, tag: str = IMAGE) -> None:
    """Build the organizers' Dockerfile.public.

    Its COPY lines expect imp.py, telnetlib.py, and wheels/ at the context root,
    which is not how the download is laid out, so assemble a temporary context.
    """
    with tempfile.TemporaryDirectory(prefix="swegemma-build-") as ctx:
        shutil.copy(data_dir / "docker" / "Dockerfile.public", Path(ctx) / "Dockerfile")
        shutil.copy(data_dir / "docker" / "imp.py", ctx)
        shutil.copy(data_dir / "docker" / "telnetlib.py", ctx)
        shutil.copytree(data_dir / "wheels", Path(ctx) / "wheels")
        res = subprocess.run(["docker", "build", "-t", tag, ctx], check=False)
    if res.returncode != 0:
        raise RuntimeError(f"docker build failed for {tag}")


class Sandbox:
    """One task container. Use as a context manager so it is always removed."""

    def __init__(self, image: str = IMAGE, memory: str = "4g", cpus: str = "2") -> None:
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.name = f"swegemma-{uuid.uuid4().hex[:12]}"

    def __enter__(self) -> "Sandbox":
        # Starts online so bootstrap can install dependencies; bootstrap() always
        # disconnects the network before returning, so agent commands and tests run offline.
        res = _docker(
            "run", "-d", "--rm", "--name", self.name,
            "--network", "bridge", "--memory", self.memory, "--cpus", self.cpus,
            "-v", f"{PIP_CACHE_VOLUME}:/root/.cache/pip",
            "-e", "TEST_TMPDIR=/tmp", "-w", "/workspace",
            self.image, "sleep", "infinity",
        )
        if res.returncode != 0:
            raise RuntimeError(f"docker run failed: {res.stderr.decode(errors='replace')}")
        return self

    def go_offline(self) -> None:
        res = _docker("network", "disconnect", "-f", "bridge", self.name, timeout=60)
        check = self.exec("python3 -c \"import socket; socket.create_connection(('pypi.org', 443), 3)\"", timeout=30)
        if check.exit_code == 0:
            raise RuntimeError(f"sandbox {self.name} still has network access: {res.stderr.decode(errors='replace')}")

    def __exit__(self, *exc: object) -> None:
        _docker("rm", "-f", self.name, timeout=60)

    def exec(self, command: str, timeout: int = 300, workdir: str = "/workspace", env: dict[str, str] | None = None) -> ExecResult:
        """Run `bash -c command` in the container, killing it after `timeout` seconds.

        The kill happens inside the container (coreutils `timeout`), because
        killing the local `docker exec` client would leave the process running.
        """
        env_args = [arg for k, v in (env or {}).items() for arg in ("-e", f"{k}={v}")]
        res = _docker(
            "exec", "-w", workdir, *env_args, self.name,
            "timeout", "-s", "KILL", str(timeout), "bash", "-c", command,
            timeout=timeout + 30,
        )
        return ExecResult(
            exit_code=res.returncode,
            stdout=res.stdout.decode("utf-8", errors="replace"),
            stderr=res.stderr.decode("utf-8", errors="replace"),
            timed_out=res.returncode == 137,
        )

    def run(self, *argv: str, timeout: int = 300) -> ExecResult:
        """Run argv directly (no shell), for commands built from untrusted strings."""
        res = _docker("exec", "-w", "/workspace", self.name, "timeout", "-s", "KILL", str(timeout), *argv, timeout=timeout + 30)
        return ExecResult(res.returncode, res.stdout.decode("utf-8", errors="replace"), res.stderr.decode("utf-8", errors="replace"), res.returncode == 137)

    def put_file(self, container_path: str, data: bytes) -> None:
        res = _docker("exec", "-i", self.name, "bash", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "_", container_path, input_bytes=data)
        if res.returncode != 0:
            raise RuntimeError(f"writing {container_path} failed: {res.stderr.decode(errors='replace')}")

    def copy_in(self, local: Path, container_path: str) -> None:
        res = _docker("cp", str(local), f"{self.name}:{container_path}")
        if res.returncode != 0:
            raise RuntimeError(f"docker cp {local} failed: {res.stderr.decode(errors='replace')}")

    def bootstrap(self, task: dict, data_dir: Path = DEFAULT_DATA_DIR, commit_message: str = "baseline") -> None:
        """README 4.2: snapshot -> git exclude + editable install + deps (setup.py) -> baseline commit."""
        snapshot = data_dir / "snapshots" / f"{task['instance_id']}.tgz"
        if not snapshot.is_file():
            raise FileNotFoundError(f"snapshot missing: {snapshot} (run scripts/fetch_data.sh with a split containing it)")
        self.copy_in(snapshot, "/tmp/snapshot.tgz")
        self.copy_in(data_dir / "sandbox" / "setup.py", "/tmp/sandbox_setup.py")
        cutoff = dependency_cutoff(task)
        pip = f"pip install -q --uploaded-prior-to={cutoff}"
        online_steps = [
            "tar --no-same-owner -xzf /tmp/snapshot.tgz -C /workspace && rm /tmp/snapshot.tgz",
            "mkdir -p /sandbox && mv /tmp/sandbox_setup.py /sandbox/setup.py",
            # Fast path, as officially run after the cached site-packages are streamed in:
            # git exclude, editable install (no deps), .pth paths, pytest.ini / conftest.py.
            f"python3 /sandbox/setup.py --fast-path {task['repo'].split('/')[-1]} >/tmp/setup.log 2>&1 || (tail -20 /tmp/setup.log; exit 1)",
            # Stand-in for the unpublished per-repo caches: the repo's declared runtime and
            # test requirements, resolved as of the task date so versions match its era
            # (the newest starlette, for one, breaks older fastapi test clients). Falls back
            # to an unconstrained install when an old release cannot build on Python 3.13.
            f"({pip} -e /workspace || pip install -q -e /workspace) >>/tmp/setup.log 2>&1 || true",
            "for f in " + " ".join(TEST_REQUIREMENT_FILES) + "; do "
            f'[ -f "$f" ] && ({pip} -r "$f" || pip install -q -r "$f") >>/tmp/setup.log 2>&1; done; true',
            # Newer repos keep test deps in PEP 735 [dependency-groups] instead of requirements files.
            "for g in $(python3 -c \"import tomllib; d=tomllib.load(open('pyproject.toml','rb'));"
            " print(' '.join(k for k in d.get('dependency-groups', {}) if 'test' in k or 'dev' in k))\" 2>/dev/null); do "
            f'({pip} --group "$g" || pip install -q --group "$g") >>/tmp/setup.log 2>&1; done; true',
        ]
        try:
            for step in online_steps:
                self._bootstrap_step(step)
        finally:
            self.go_offline()
        self._bootstrap_step(f"git add -A && git commit -q --allow-empty -m {commit_message}")

    def _bootstrap_step(self, step: str) -> None:
        res = self.exec(step, timeout=900)
        if res.exit_code != 0:
            raise RuntimeError(f"bootstrap step failed ({step.split()[0]}): {res.stdout[-2000:]}{res.stderr[-2000:]}")

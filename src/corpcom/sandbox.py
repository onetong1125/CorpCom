"""Run one Python program in the local Docker sandbox."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import uuid
from threading import Lock

_workspace_locks: dict[str, Lock] = {}
_workspace_locks_guard = Lock()

IMAGE = "corpcom-sandbox:local"


def run_python(
    code: str,
    *,
    runs_root: Path | None = None,
    task_dir: Path | None = None) -> dict:
    base_dir = Path.cwd() / "runs" if runs_root is None else runs_root
    run_dir = base_dir.resolve() / uuid.uuid4().hex
    run_dir.mkdir(parents=True)
    (run_dir / "analysis.py").write_text(code)

    work_dir = run_dir if task_dir is None else task_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    container_name = f"corpcom-{run_dir.name}"

    command = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--network", "bridge",
        "--memory", "1g",
        "--cpus", "1",
        "--pids-limit", "64",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,size=64m",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--mount", f"type=bind,src={run_dir},dst=/execution",
        "--mount", f"type=bind,src={work_dir},dst=/workspace",
        "--workdir", "/workspace",
        IMAGE,
        "python", "/execution/analysis.py",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=90, check=False
        )
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return {"status": "error", "error": "sandbox timed out after 90 seconds", "run_dir": str(run_dir)}

    if completed.returncode:
        return {
            "status": "error",
            "exit_code": completed.returncode,
            "error": completed.stderr[-4000:],
            "run_dir": str(run_dir),
        }

    result_file = run_dir / "result.json"
    if result_file.is_symlink():
        return {"status": "error", "error": "result.json must be a regular file", "run_dir": str(run_dir)}
    if not result_file.is_file():
        return {"status": "error", "error": "sandbox did not create result.json", "run_dir": str(run_dir)}
    if result_file.stat().st_size > 16384:
        return {"status": "error", "error": "result.json exceeds 16 KiB", "run_dir": str(run_dir)}

    try:
        result = json.loads(result_file.read_text())
    except json.JSONDecodeError as exc:
        return {"status": "error", "error": f"invalid result.json: {exc}", "run_dir": str(run_dir)}
    return {"status": "ok", "result": result, "run_dir": str(run_dir)}

def create_workspace_container(
    workspace_id: str,
    workspace_dir: Path,
) -> str:
    """Start a persistent container backed by the workspace directory."""
    container_name = f"corpcom-workspace-{workspace_id}"
    command = [
        "docker", "run", "-d",
        "--name", container_name,
        "--network", "bridge",
        "--memory", "1g",
        "--cpus", "1",
        "--pids-limit", "64",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,size=64m",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--mount", f"type=bind,src={workspace_dir.resolve()},dst=/workspace",
        "--workdir", "/workspace",
        IMAGE, "sleep", "infinity"
    ]

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return completed.stdout.strip()

def ensure_workspace_container(
    workspace_id: str,
    workspace_dir: Path,
) -> str:
    """Return a running container for the workspace, creating it if needed."""
    with _workspace_locks_guard:
        if workspace_id not in _workspace_locks:
            _workspace_locks[workspace_id] = Lock()
        workspace_lock = _workspace_locks[workspace_id]

    with workspace_lock:
        container_name = f"corpcom-workspace-{workspace_id}"

        completed = subprocess.run(
            [
                "docker", "container", "ls",
                "-a",
                "--no-trunc",
                "--filter", f"name=^/{container_name}$",
                "--format", "{{.ID}}\t{{.State}}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )

        output = completed.stdout.strip()
        if not output:
            return create_workspace_container(workspace_id, workspace_dir)

        container_id, state = output.split("\t")
        if state == "running":
            return container_id
        if state in ("created", "exited"):
            subprocess.run(
                ["docker", "start", container_id],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            return container_id
        raise RuntimeError(
            f"Cannot reuse container {container_id}: state={state}"
        )



def main() -> None:
    parser = argparse.ArgumentParser(description="Run a fixed Python script in the Docker sandbox")
    parser.add_argument("script", type=Path)
    args = parser.parse_args()
    print(json.dumps(run_python(args.script.read_text()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Run one Python program in the local Docker sandbox."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import uuid


IMAGE = "corpcom-sandbox:local"


def run_python(code: str, *, runs_root: Path | None = None) -> dict:
    base_dir = Path.cwd() / "runs" if runs_root is None else runs_root
    run_dir = base_dir.resolve() / uuid.uuid4().hex
    run_dir.mkdir(parents=True)
    (run_dir / "analysis.py").write_text(code)
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
        "--mount", f"type=bind,src={run_dir},dst=/workspace",
        IMAGE,
        "python", "/workspace/analysis.py",
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a fixed Python script in the Docker sandbox")
    parser.add_argument("script", type=Path)
    args = parser.parse_args()
    print(json.dumps(run_python(args.script.read_text()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

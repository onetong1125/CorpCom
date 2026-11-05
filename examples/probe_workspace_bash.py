import time
from tempfile import TemporaryDirectory
from pathlib import Path
import subprocess
from uuid import uuid4
import shlex
from concurrent.futures import ThreadPoolExecutor

from corpcom.sandbox import run_bash, ensure_workspace_container

# 测试用独立身份
workspace_id = f"probe-{uuid4().hex}"
container_id = None

with TemporaryDirectory(prefix="corpcom-rebuild-") as temporary:
  workspace_dir = Path(temporary).resolve()
  try:
    container_id = ensure_workspace_container(
        workspace_id, workspace_dir
    )
    # 1. pwd
    result = run_bash(
        "pwd",
        workspace_id=workspace_id,
        workspace_dir=workspace_dir,
    )
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "/workspace"

    # 2. 创建目录并写入文件
    result = run_bash(
        "mkdir -p tasks/demo && printf 'hello' > tasks/demo/note.txt",
        workspace_id=workspace_id,
        workspace_dir=workspace_dir,
    )
    assert result["exit_code"] == 0, result["stderr"]

    # 3. 读取文件
    result = run_bash(
        "cat tasks/demo/note.txt",
        workspace_id=workspace_id,
        workspace_dir=workspace_dir,
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert result["stdout"] == "hello"
    # 验证确实写入到了宿主机目录
    host_file = workspace_dir / "tasks/demo/note.txt"
    assert host_file.read_text(encoding="utf-8") == "hello"

    # 4.
    result = run_bash(
        "printf 'failed' >&2; exit 7",
        workspace_id=workspace_id,
        workspace_dir=workspace_dir,
    )
    assert result["exit_code"] == 7
    assert result["stderr"] == "failed"

    # 5. 同一 Workspace 的两条命令并行执行
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as executor:
      future_a = executor.submit(
          run_bash,
          "sleep 2; printf 'A'",
          workspace_id=workspace_id,
          workspace_dir=workspace_dir,
      )
      future_b = executor.submit(
          run_bash,
          "sleep 2; printf 'B'",
          workspace_id=workspace_id,
          workspace_dir=workspace_dir,
      )
      result_a = future_a.result()
      result_b = future_b.result()

    elapsed = time.monotonic() - started
    assert result_a["exit_code"] == 0, result_a["stderr"]
    assert result_a["stdout"] == "A"
    assert result_b["exit_code"] == 0, result_b["stderr"]
    assert result_b["stdout"] == "B"

    print(f"两条命令总耗时：{elapsed:.2f} 秒")

    # 6. 超时机制验证
    child_code = (
      "import os, signal, time; from pathlib import Path; "
      "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
      "Path('timeout-child.pid').write_text(str(os.getpid())); "
      "time.sleep(20)"
    )
    timeout_command = (
      "echo $$ > timeout-parent.pid; "
      f"python -c {shlex.quote(child_code)} & wait"
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
      future_a = executor.submit(
          run_bash,
          timeout_command,
          workspace_id=workspace_id,
          workspace_dir=workspace_dir,
          timeout_seconds=2,
      )
      future_b = executor.submit(
          run_bash,
          "sleep 5; printf survivor",
          workspace_id=workspace_id,
          workspace_dir=workspace_dir,
          timeout_seconds=10,
      )
      result_a = future_a.result()
      result_b = future_b.result()

    # A 超时终止，B 正常完成
    assert result_a["exit_code"] == 137, result_a
    assert result_b["exit_code"] == 0, result_b
    assert result_b["stdout"] == "survivor", result_b

    # 从挂载到宿主机的工作区读取父子进程 PID
    for filename in ("timeout-parent.pid", "timeout-child.pid"):
      pid = int((workspace_dir / filename).read_text().strip())

      result = run_bash(
          f"test ! -e /proc/{pid}",
          workspace_id=workspace_id,
          workspace_dir=workspace_dir,
      )
      assert result["exit_code"] == 0, f"进程 {pid} 尚未回收：{result}"

    # 仍然复用原来的容器
    current_id = ensure_workspace_container(workspace_id, workspace_dir)
    assert current_id == container_id

    print("超时隔离、父子进程回收和容器复用验证通过")

  finally:
    if container_id is not None:
      subprocess.run(
          ["docker", "rm", "-f", container_id],
          check=True,
          capture_output=True,
          text=True,
          timeout=10,
      )
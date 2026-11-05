from tempfile import TemporaryDirectory
from pathlib import Path
import subprocess
from uuid import uuid4

from corpcom.sandbox import ensure_workspace_container

# 测试用独立身份
workspace_id = f"probe-{uuid4().hex}"
container_id = None

with TemporaryDirectory(prefix="corpcom-rebuild-") as temporary:
  workspace_dir = Path(temporary).resolve()
  try:
    # 1. 创建容器A，挂载临时工作区
    container_id = ensure_workspace_container(
        workspace_id, workspace_dir
    )
    first_id = container_id
    print(f"容器 A：{first_id}")
    # 2. 在容器内写文件，文件实际保存在宿主机挂载目录。
    subprocess.run(
        [
          "docker", "exec", container_id,
          "bash", "-c",
          "mkdir -p /workspace/tasks/demo && "
          "printf 'hello' > /workspace/tasks/demo/note.txt",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    host_file = workspace_dir / "tasks/demo/note.txt"
    assert host_file.read_text(encoding="utf-8") == "hello"

    # 3. 删除容器 A，保留宿主机工作区。
    subprocess.run(
        ["docker", "rm", "-f", container_id],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    container_id = None

    assert host_file.read_text(encoding="utf-8") == "hello"

    # 4. 相同 Workspace 身份、相同目录，创建容器 B。
    container_id = ensure_workspace_container(
        workspace_id, workspace_dir
    )
    second_id = container_id
    print(f"容器 B：{second_id}")

    assert first_id != second_id, "应当创建一个新容器"

    # 5. 从新容器读取旧文件。
    completed = subprocess.run(
        [
          "docker", "exec", container_id,
          "cat", "/workspace/tasks/demo/note.txt",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.stdout == "hello", "重建后文件内容不一致"
    print("PASS：容器已重建，Workspace 文件仍然保留")

  finally:
    if container_id is not None:
      subprocess.run(
          ["docker", "rm", "-f", container_id],
          check=True,
          capture_output=True,
          text=True,
          timeout=10,
      )

# 退出 with 后，TemporaryDirectory 自动删除测试文件
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from langchain.tools import tool
from langchain_core.tools import BaseTool, ToolException

from corpcom.workspace import WorkspaceStore


_file_locks: dict[Path, Lock] = {}
_file_locks_guard = Lock()

@contextmanager
def _locked_workspace_file(workspace_dir: Path, file_path: str):
    """Resolve model-supplied paths and serialize writes within this process."""
    relative = Path(file_path)
    if not file_path or "\0" in file_path or relative.is_absolute():
        raise ToolException("file_path must be a non-empty workspace-relative path")
    root = workspace_dir.resolve()
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ToolException("file_path must stay within the current workspace")
    with _file_locks_guard:
        lock = _file_locks.setdefault(path, Lock())
    with lock:
        if path.is_dir():
            raise ToolException("file_path must refer to a file, not a directory")
        yield path


def create_file_tools(data_dir: Path, workspace_id: str) -> list[BaseTool]:
  @tool
  def write_file(file_path: str, content: str) -> dict:
    """Create or overwrite a UTF-8 file in the current workspace.

    Required for creating text files or replacing their entire contents,
    including code files. Do not use shell redirection, heredocs, tee,
    or temporary scripts instead. Use edit_file for targeted changes.
    file_path is relative to /workspace, such as tasks/demo/report.md.
    Create missing parent directories. Overwrite the entire existing file.
    Share a per-file lock with edit_file; Bash writes do not use this lock.
    Use register_artifact to register the finished file; writing alone does not
    create an artifact. Update research.json via update_research.
    """
    with WorkspaceStore(data_dir) as store:
      store.get_workspace(workspace_id)
    workspace_dir = data_dir / "workspaces" / workspace_id
    with _locked_workspace_file(workspace_dir, file_path) as path:
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(content, encoding="utf-8")
    return {"status": "ok",
            "file_path": path.relative_to(workspace_dir.resolve()).as_posix()}

  write_file.handle_tool_error = True

  @tool
  def edit_file(
      file_path: str, old_string: str, new_string: str,
      replace_all: bool = False,
  ) -> dict:
    """Replace exact text in an existing UTF-8 workspace file.

    Required for targeted text changes. Do not substitute sed -i, shell
    redirection, heredocs, or temporary scripts. If matching fails, read
    the latest file with execute_bash and correct the edit arguments.
    file_path is relative to /workspace. old_string must not be empty.
    By default it must occur exactly once; replace_all replaces every occurrence.
    Missing or ambiguous matches return an error without changing the file.
    Read, match, and write under the same per-file lock as write_file.
    Bash writes do not use this lock. Files are not automatically registered.
    Use register_artifact for an unregistered deliverable; already registered
    files keep their artifact ID and expose the updated contents.
    Update research.json via update_research.
    """
    with WorkspaceStore(data_dir) as store:
      store.get_workspace(workspace_id)
    if not old_string:
      raise ToolException("old_string must not be empty")
    workspace_dir = data_dir / "workspaces" / workspace_id
    with _locked_workspace_file(workspace_dir, file_path) as path:
      try:
        content = path.read_text(encoding="utf-8")
      except FileNotFoundError as exc:
        raise ToolException(
          "File not found; read the workspace directory before editing") from exc
      except UnicodeDecodeError as exc:
        raise ToolException("edit_file only supports UTF-8 text files") from exc
      count = content.count(old_string)
      if count == 0:
        raise ToolException(
          "old_string was not found; read the latest file before editing")
      if count > 1 and not replace_all:
        raise ToolException(
          "old_string matches multiple times; provide a unique string or set replace_all=True")
      path.write_text(content.replace(old_string, new_string), encoding="utf-8")
    return {
      "status": "ok",
      "file_path": path.relative_to(workspace_dir.resolve()).as_posix(),
      "replacements": count,
    }

  edit_file.handle_tool_error = True

  return [write_file, edit_file]
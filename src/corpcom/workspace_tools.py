"""Define research tools bound to one workspace and its data directory."""

from pathlib import Path
from contextlib import contextmanager
import inspect
from threading import Lock
from pydantic import BaseModel, ConfigDict, JsonValue

from langchain.tools import tool
from langchain_core.tools import BaseTool, ToolException

from corpcom.function_catalog import FUNCTIONS
from corpcom.workspace import WorkspaceStore
from corpcom.workspace_models import WorkspaceResearchInfo
from corpcom.sandbox import run_python, run_bash

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


class SandboxOutput(BaseModel):
    """One file explicitly selected for workspace registration."""

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: str


class SandboxResult(BaseModel):
    """The required structure of the sandbox's result.json."""

    model_config = ConfigDict(extra="forbid")

    summary: dict[str, JsonValue]
    outputs: list[SandboxOutput]

def create_workspace_tools(data_dir: Path, workspace_id: str) -> list[BaseTool]:
    """Bind workspace ownership without exposing it as a model-supplied argument."""
    # Tools run in worker threads, so each read owns a connection created in that worker.
    @tool
    def read_thread(thread_id: str) -> dict:
        """Read messages from an earlier conversation in this workspace."""
        with WorkspaceStore(data_dir) as reader:
            history = reader.get_thread(workspace_id, thread_id)
        return {
            "title": history.title,
            "messages": [
                {"role": item.role, "content": item.content}
                for item in history.messages
            ],
        }

    # 暂停按 Artifact ID 读取的 Agent 工具，工作区文件通过 execute_bash 读取。
    # @tool
    # def read_file(artifact_id: str) -> dict:
    #     """Read the UTF-8 text of a saved file in this workspace using its artifact ID."""
    #     with WorkspaceStore(data_dir) as reader:
    #         file = reader.get_file(workspace_id, artifact_id)
    #     return {"name": file.name, "content": file.path.read_text(encoding="utf-8")}

    @tool
    def update_research(changes: WorkspaceResearchInfo) -> dict:
        """Save research changes to this workspace's research.json.

        Use for explicit user changes or first-message initialization instructed
        by the system. During initialization, derive the goal, scope and a testable
        hypothesis from the user's research question; extract only stated periods
        and preferences. Otherwise, change only what the user requests.

        Supply only fields to initialize or update: goal, initial_hypothesis,
        scope, observation_period, preferences. Omitted fields are preserved.
        Empty strings clear text fields. Preferences replace the entire object;
        preserve existing keys when adding a preference, or use {} to clear all.
        Do not infer user preferences. Outside first-message initialization,
        do not change a hypothesis from analysis alone.
        Returns all saved research fields; use these values after an update.
        """
        with WorkspaceStore(data_dir) as store:
            saved = store.update_research(workspace_id, changes)
        return saved.info.model_dump()

    @tool
    def execute_python(code: str, archive_path: str) -> dict:
      """Run Python in Docker and archive selected outputs in this workspace.

        Do not use this tool as a workaround for creating or editing workspace
        text files. Use write_file or edit_file for those operations.

        Submit a complete Python program that can be executed directly.
        Each execution uses a fresh temporary working directory.
        The system saves the submitted code as analysis.py there.
        Do not overwrite it; include it in outputs to archive the program.

        archive_path selects a directory under this workspace's tasks directory.
        Use a meaningful task name and an explicit version, such as
        "moutai-wuliangye/v1". Consult saved relative_path values to choose a
        new version directory for each batch. Existing directories are rejected;
        saved files are never overwritten. Do not include the tasks prefix.
        This directory receives copies after execution; it is not mounted
        into the sandbox and does not provide access to earlier files.

        Save output files and result.json in the working directory.
        Printing output is not sufficient. result.json must be at most
        16 KiB and contain exactly these fields:
        - summary: an object containing findings and source information.
        - outputs: a list of objects containing path and kind strings.

        Each output path must be relative to the working directory.
        Only explicitly listed files are copied and indexed.
        Use an empty outputs list when no files need saving; no archive
        directory is created in that case.
        Example:
        {"summary": {"rows": 2},
         "outputs": [{"path": "financials.json", "kind": "data"}]}

        Execution is limited to 90 seconds.
        On success, return the summary in result and saved file references
        in artifacts. relative_path identifies each saved copy within the
        data directory. Use execute_bash to find and read files under /workspace.
        """
      with WorkspaceStore(data_dir) as store:
        store.get_workspace(workspace_id)

      execution = run_python(
          code,
          runs_root=data_dir / "workspaces" / workspace_id / ".runs",
      )

      if execution["status"] != "ok":
        return {
          key: value
          for key, value in execution.items()
          if key != "run_dir"
        }

      result = SandboxResult.model_validate(execution["result"])
      with WorkspaceStore(data_dir) as store:
        artifacts = store.archive_run_files(
            workspace_id,
            Path(execution["run_dir"]),
            [
              (Path(output.path), output.kind)
              for output in result.outputs
            ],
            archive_path=Path(archive_path),
        )

      return {
        "status": "ok",
        "result": result.summary,
        "artifacts": [
          {
            "artifact_id": artifact.id,
            "name": artifact.name,
            "kind": artifact.kind,
            "relative_path": artifact.relative_path,
          }
          for artifact in artifacts
        ],
      }

    @tool
    def execute_bash(command: str) -> dict:
      """Run Bash in this workspace's shared Docker container.

      Use for browsing, searching, reading files, and executing programs.
      To create or overwrite text files, use write_file. To change existing
      text, use edit_file. Do not substitute shell redirection (>, >>),
      heredocs, tee, sed -i, or temporary scripts for these file tools.
      Save code with write_file or edit_file before executing it here.
      The working directory is /workspace. Files persist across calls.
      Commands have a 90-second limit.
      Return exit_code, stdout, and stderr.
      Files created here are not automatically registered as artifacts.
      """
      with WorkspaceStore(data_dir) as store:
        store.get_workspace(workspace_id)

      return run_bash(
          command,
          workspace_id=workspace_id,
          workspace_dir=data_dir / "workspaces" / workspace_id,
      )

    @tool
    def write_file(file_path: str, content: str) -> dict:
        """Create or overwrite a UTF-8 file in the current workspace.

        Required for creating text files or replacing their entire contents,
        including code files. Do not use shell redirection, heredocs, tee,
        or temporary scripts instead. Use edit_file for targeted changes.
        file_path is relative to /workspace, such as tasks/demo/report.md.
        Create missing parent directories. Overwrite the entire existing file.
        Share a per-file lock with edit_file; Bash writes do not use this lock.
        This does not register an artifact. Update research.json via update_research.
        """
        with WorkspaceStore(data_dir) as store:
            store.get_workspace(workspace_id)
        workspace_dir = data_dir / "workspaces" / workspace_id
        with _locked_workspace_file(workspace_dir, file_path) as path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return {"status": "ok", "file_path": path.relative_to(workspace_dir.resolve()).as_posix()}

    write_file.handle_tool_error = True

    @tool
    def edit_file(
        file_path: str, old_string: str, new_string: str, replace_all: bool = False,
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
                raise ToolException("File not found; read the workspace directory before editing") from exc
            except UnicodeDecodeError as exc:
                raise ToolException("edit_file only supports UTF-8 text files") from exc
            count = content.count(old_string)
            if count == 0:
                raise ToolException("old_string was not found; read the latest file before editing")
            if count > 1 and not replace_all:
                raise ToolException("old_string matches multiple times; provide a unique string or set replace_all=True")
            path.write_text(content.replace(old_string, new_string), encoding="utf-8")
        return {
            "status": "ok",
            "file_path": path.relative_to(workspace_dir.resolve()).as_posix(),
            "replacements": count,
        }

    edit_file.handle_tool_error = True

    @tool
    def read_function_doc(function_id: str) -> dict:
        """Read the import statement, signature, and full documentation of one sandbox function selected for the current task.

        Choose the function using the summaries already provided in the system message.
        Call this tool only when you intend to use that function for the current request.
        Do not read unrelated function documentation or enumerate the catalog.
        Reuse documentation already present in the conversation.
        This tool reads metadata without executing the function.
        """
        if function_id not in FUNCTIONS:
            return {
                "status": "error",
                "error": f"Unknown sandbox function: {function_id}",
                "available_functions": list(FUNCTIONS),
            }

        function = FUNCTIONS[function_id]
        return {
            "function_id": function_id,
            "import": (
                f"from {function.__module__} import {function.__name__}"
            ),
            "signature": (
                f"{function.__name__}{inspect.signature(function)}"
            ),
            "documentation": inspect.getdoc(function),
        }


    return [read_thread, update_research, read_function_doc,
            execute_python, execute_bash, write_file, edit_file,
            # read_file,  # 暂停注册
            ]

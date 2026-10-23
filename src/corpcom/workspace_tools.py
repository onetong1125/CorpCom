"""Define research tools bound to one workspace and its data directory."""

from pathlib import Path
import inspect
from pydantic import BaseModel, ConfigDict, JsonValue

from langchain.tools import tool
from langchain_core.tools import BaseTool

from corpcom.function_catalog import FUNCTIONS
from corpcom.workspace import WorkspaceStore
from corpcom.sandbox import run_python

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

    @tool
    def read_file(artifact_id: str) -> dict:
        """Read the UTF-8 text of a saved file in this workspace using its artifact ID."""
        with WorkspaceStore(data_dir) as reader:
            file = reader.get_file(workspace_id, artifact_id)
        return {"name": file.name, "content": file.path.read_text(encoding="utf-8")}

    @tool
    def execute_python(code: str, archive_path: str) -> dict:
      """Run Python in Docker and archive selected outputs in this workspace.

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
        data directory. Use artifact_id with read_file to read saved text.
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


    return [read_thread, read_file, read_function_doc, execute_python]

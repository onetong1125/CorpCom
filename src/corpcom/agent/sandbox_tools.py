"""Agent tools for running Python and Bash in a workspace sandbox."""

from pathlib import Path

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, JsonValue

from corpcom.sandbox import run_bash, run_python
from corpcom.workspace import WorkspaceStore


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


def create_sandbox_tools(data_dir: Path, workspace_id: str) -> list[BaseTool]:
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
      Use register_artifact to register finished workspace files for delivery.
      """
      with WorkspaceStore(data_dir) as store:
        store.get_workspace(workspace_id)

      return run_bash(
          command,
          workspace_id=workspace_id,
          workspace_dir=data_dir / "workspaces" / workspace_id,
      )

    return [execute_python, execute_bash]

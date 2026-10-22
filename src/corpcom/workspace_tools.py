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
    def execute_python(code: str) -> dict:
        """Run Python in the Docker sandbox and register selected output files.

        Save output files in the working directory.
        Write result.json, at most 16 KiB, with exactly these fields:
        - summary: an object containing findings and source information.
        - outputs: a list of objects containing path and kind strings.

        Each path must be relative to the current run directory.
        Use an empty outputs list when no files need registration.
        Only explicitly listed files are registered, without copying them.
        Example:
        {"summary": {"rows": 2},
         "outputs": [{"path": "financials.json", "kind": "data"}]}

        Execution is limited to 90 seconds.
        On success, return the summary in result and registered file references
        in artifacts. Use their artifact_id values with read_file.
        """
        with WorkspaceStore(data_dir) as store:
            store.get_workspace(workspace_id)

        execution = run_python(
            code,
            runs_root=data_dir / "workspaces" / workspace_id / "runs",
        )

        if execution["status"] != "ok":
            return {
                key: value
                for key, value in execution.items()
                if key != "run_dir"
            }

        result = SandboxResult.model_validate(execution["result"])
        with WorkspaceStore(data_dir) as store:
            artifacts = store.register_run_files(
                workspace_id,
                Path(execution["run_dir"]),
                [
                    (Path(output.path), output.kind)
                    for output in result.outputs
                ],
            )

        return {
            "status": "ok",
            "result": result.summary,
            "artifacts": [
                {
                    "artifact_id": artifact.id,
                    "name": artifact.name,
                    "kind": artifact.kind,
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

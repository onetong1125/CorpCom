"""Define research tools bound to one workspace and its data directory."""

from pathlib import Path
import inspect

from langchain.tools import tool
from langchain_core.tools import BaseTool, ToolException

from corpcom.function_catalog import FUNCTIONS
from corpcom.errors import ArtifactRegistrationError
from corpcom.workspace import WorkspaceStore
from corpcom.workspace_models import WorkspaceResearchInfo
from corpcom.agent.file_tools import create_file_tools
from corpcom.agent.memory_tools import create_memory_tools
from corpcom.agent.sandbox_tools import create_sandbox_tools


def create_workspace_tools(
    data_dir: Path, workspace_id: str, *, user_memory_dir: Path | None = None,
) -> list[BaseTool]:
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
    def register_artifact(file_path: str, kind: str) -> dict:
        """Register an existing workspace file for the frontend file list and download.

        file_path is relative to /workspace, for example tasks/demo/report.md.
        kind is a non-empty category such as report, data, or code.
        Register finished files created with write_file or by programs run via Bash.
        This indexes the existing file without copying it. Later edits are visible
        through the same artifact ID; this is not an immutable snapshot.
        Repeated registration of the same resolved path returns the existing ID
        and kind. execute_python outputs and uploaded files are already registered.
        Returns artifact_id, name, kind, and relative_path (relative to data directory).
        """
        with WorkspaceStore(data_dir) as store:
            try:
                artifact = store.register_workspace_file(workspace_id, Path(file_path), kind)
            except ArtifactRegistrationError as exc:
                raise ToolException(str(exc)) from exc
        return {
            "artifact_id": artifact.id,
            "name": artifact.name,
            "kind": artifact.kind,
            "relative_path": artifact.relative_path,
        }

    register_artifact.handle_tool_error = True

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


    return [
      read_thread,
      *create_memory_tools(data_dir, workspace_id, user_memory_dir=user_memory_dir),
      update_research,
      read_function_doc,
      *create_sandbox_tools(data_dir, workspace_id),
      *create_file_tools(data_dir, workspace_id),
      register_artifact,
      # read_file,  # 暂停注册
    ]

"""Define read tools bound to one workspace and its data directory."""

from pathlib import Path

from langchain.tools import tool
from langchain_core.tools import BaseTool

from corpcom.workspace import WorkspaceStore


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

    return [read_thread, read_file]

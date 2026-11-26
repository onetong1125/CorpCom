from pathlib import Path

from langchain.tools import tool
from langchain_core.tools import BaseTool, ToolException

from corpcom.memory import MemoryScope, MemoryType
from corpcom.workspace import WorkspaceStore


def create_memory_tools(
    data_dir: Path, workspace_id: str, *, user_memory_dir: Path | None = None,
) -> list[BaseTool]:
    if user_memory_dir is None:
        user_memory_dir = data_dir / "user" / "memory"

    @tool
    def read_memory(scope: MemoryScope, key: str) -> str:
        """Read the full Markdown of a memory topic by its .md filename.

        Use scope "user" for shared memory or "workspace" for this workspace's memory.
        """
        with WorkspaceStore(data_dir) as store:
            return store.read_memory_topic(workspace_id, scope, user_memory_dir, key)

    @tool
    def search_memory(scope: MemoryScope, query: str) -> str:
        """Search the full memory index in the selected scope for a keyword.

        Use this when a relevant topic is missing from the index preview.
        Use a filename from the matching lines with read_memory to read the topic.
        """
        with WorkspaceStore(data_dir) as store:
            index = store.read_memory_index(workspace_id, scope, user_memory_dir)
        return "\n".join(line for line in index.splitlines() if query in line)

    @tool
    def write_memory(
        scope: MemoryScope, key: str, body: str,
        name: str | None = None, description: str | None = None,
        memory_type: MemoryType | None = None,
        expected_markdown: str | None = None,
    ) -> str:
        """Create a memory topic or revise its body in the selected scope.

        To create a topic, supply name, description, and memory_type.
        To revise a topic, first call read_memory with the same scope and key.
        Pass its complete Markdown verbatim as expected_markdown and the revised
        body without frontmatter as body. If the topic changed, read it again
        and merge the changes before retrying.
        """
        with WorkspaceStore(data_dir) as store:
            if expected_markdown is not None:
                store.revise_memory_topic(
                    workspace_id, scope, user_memory_dir, key,
                    expected_markdown=expected_markdown, body=body,
                )
            else:
                if name is None or description is None or memory_type is None:
                    raise ToolException("Creating a memory topic requires name, description, and memory_type")
                store.create_memory_topic(
                    workspace_id, scope, user_memory_dir, key,
                    name=name, description=description, memory_type=memory_type, body=body,
                )
        return f"已保存 {key}"

    @tool
    def forget_memory(
        scope: MemoryScope, key: str, expected_markdown: str,
    ) -> str:
        """Remove a memory topic and its index entry in the selected scope.

        First call read_memory with the same scope and key. Pass the complete
        Markdown it returned verbatim as expected_markdown. If the topic has
        changed, read it again before deciding whether to retry.
        """
        with WorkspaceStore(data_dir) as store:
            store.forget_memory_topic(
                workspace_id, scope, user_memory_dir, key, expected_markdown=expected_markdown,
            )
        return f"已移除 {key}"

    return [read_memory, search_memory, write_memory, forget_memory]

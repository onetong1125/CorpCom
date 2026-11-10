"""Persist missing results from the last tool exchange of an interrupted Thread."""

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from corpcom.workspace import WorkspaceStore
from corpcom.workspace_agent import create_workspace_agent


def recover_interrupted_checkpoint(
    store: WorkspaceStore, model: BaseChatModel,
    workspace_id: str, thread_id: str, checkpointer: BaseCheckpointSaver,
) -> None:
    """Called only after admission returns the previous status 'interrupted'."""
    agent = create_workspace_agent(store, model, workspace_id, thread_id, checkpointer)
    config = {"configurable": {"thread_id": f"{workspace_id}:{thread_id}"}}
    # A process may stop before the first checkpoint. Live state also includes
    # results already recorded by finished tasks in an incomplete graph step.
    state = agent.get_state(config)
    if not state.values:
        return

    answered = set()
    for message in reversed(state.values["messages"]):
        if isinstance(message, ToolMessage):
            answered.add(message.tool_call_id)
            continue
        if isinstance(message, AIMessage):
            missing = [ToolMessage(
                tool_call_id=call["id"], name=call["name"], status="error",
                content=("后端中断前未记录此工具的返回结果，执行结果未知。"
                         "工具可能已产生副作用；不要假定未执行或自动重试，"
                         "需要时先检查实际状态，再处理用户的新请求。"),
            ) for call in message.tool_calls if call["id"] not in answered]
            if message.tool_calls:
                # update_state does not carry live pending results into the new
                # checkpoint. Commit them with original IDs even when none are missing.
                agent.update_state(
                    config, {"messages": [*state.values["messages"], *missing]},
                    as_node="tools",
                )
        # Never repair earlier malformed exchanges from non-interruption failures.
        break

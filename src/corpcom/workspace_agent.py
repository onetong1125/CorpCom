"""Continue one research conversation using its persistent workspace context."""

import json

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain.agents.middleware import ModelCallLimitMiddleware, wrap_tool_call
from langchain_core.messages import ToolMessage

from corpcom.workspace import RecordNotFoundError, WorkspaceStore
from corpcom.workspace_tools import create_workspace_tools

@wrap_tool_call
def handle_workspace_tool_error(request, handler):
    """Return expected lookup failures as tool results the model can act on."""
    try:
        return handler(request)
    except RecordNotFoundError:
        return ToolMessage(
            content=(
                "当前 Workspace 中找不到指定资料。"
                "请检查资料索引中的 ID，再决定是否重新调用工具。"
            ),
            tool_call_id=request.tool_call["id"],
            name=request.tool_call["name"],
            status="error",
        )


def ask_workspace(
    store: WorkspaceStore, model: BaseChatModel,
    workspace_id: str, thread_id: str, question: str,
    checkpointer: BaseCheckpointSaver,
) -> str:
    """Answer in a thread, reading workspace material on demand and saving the exchange."""
    detail = store.get_workspace_detail(workspace_id)
    user_message = store.add_message(workspace_id, thread_id, "user", question)

    # Refresh workspace metadata and material indexes on every turn.
    context = {
        "title": detail.title,
        "goal": detail.goal,
        "scope": detail.scope,
        "observation_period": detail.observation_period,
        "initial_hypothesis": detail.initial_hypothesis,
        "preferences": detail.preferences,
        "other_threads": [
            {"id": item.id, "title": item.title}
            for item in detail.threads if item.id != thread_id
        ],
        "files": [
            {"id": item.id, "name": item.name, "kind": item.kind}
            for item in detail.artifacts
        ],
    }
    agent = create_agent(
        model,
        tools=create_workspace_tools(store.root, workspace_id),
        system_prompt=(
            "你是持续研究助手。以下 JSON 是当前 Workspace 的已保存研究状态。"
            "回答时遵循其目标与用户偏好，将初始假设视为待验证判断。"
            "当问题需要旧会话或文件内容时，按索引调用工具读取；不要凭索引猜测内容。"
            "区分已有证据、用户陈述与推断，不编造来源、数据或投资建议。"
            "工具只能读取当前 Workspace 的资料。\n"
            + json.dumps(context, ensure_ascii=False)
        ),
        middleware=[
            ModelCallLimitMiddleware(run_limit=6, exit_behavior="error"),
            handle_workspace_tool_error,
        ],
        checkpointer=checkpointer,
    )

    config = {
        "configurable": {
            "thread_id": f"{workspace_id}:{thread_id}",
        }
    }

    # Import existing conversation history only before its first checkpoint.
    if checkpointer.get_tuple(config) is None:
        messages = store.get_thread(workspace_id, thread_id).messages
    else:
        messages = [user_message]
    result = agent.invoke({
        "messages": [
            {
                "id": f"business:{item.id}",
                "role":item.role,
                "content":item.content,
            } for item in messages
        ],

    }, config,)
    answer = result["messages"][-1].text
    if not answer.strip():
        raise RuntimeError("model returned no answer")
    store.add_message(workspace_id, thread_id, "assistant", answer)
    return answer

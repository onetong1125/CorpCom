"""Continue one research conversation using its persistent workspace context."""

import json

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.language_models import BaseChatModel

from corpcom.workspace import WorkspaceStore
from corpcom.workspace_tools import create_workspace_tools


def ask_workspace(
    store: WorkspaceStore, model: BaseChatModel,
    workspace_id: str, thread_id: str, question: str,
) -> str:
    """Answer in a thread, reading workspace material on demand and saving the exchange."""
    detail = store.get_workspace_detail(workspace_id)
    store.add_message(workspace_id, thread_id, "user", question)
    thread = store.get_thread(workspace_id, thread_id)

    # Only the active conversation is loaded in full. Other material is indexed for tool reads.
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
        middleware=[ModelCallLimitMiddleware(run_limit=6, exit_behavior="error")],
    )
    # SQLite remains the conversation source; no framework checkpoint duplicates its history.
    result = agent.invoke({
        "messages": [{"role": item.role, "content": item.content} for item in thread.messages],
    })
    answer = result["messages"][-1].text
    if not answer.strip():
        raise RuntimeError("model returned no answer")
    store.add_message(workspace_id, thread_id, "assistant", answer)
    return answer

"""Continue one research conversation using its persistent workspace context."""

import logging

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain.agents.middleware import ModelCallLimitMiddleware, wrap_tool_call
from langchain_core.messages import ToolMessage
from langchain_core.callbacks import BaseCallbackHandler

from corpcom.workspace import RecordNotFoundError, WorkspaceStore
from corpcom.workspace_tools import create_workspace_tools
from corpcom.workspace_prompt import build_workspace_prompt

logger = logging.getLogger(__name__)


class ToolLogHandler(BaseCallbackHandler):
  def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
    logger.info(
        "工具开始 name=%s run_id=%s input=%s",
        serialized["name"],
        run_id,
        input_str,
    )

  def on_tool_end(self, output, *, run_id, **kwargs):
    logger.info(
        "工具返回 name=%s run_id=%s output=%s",
        kwargs["name"],
        run_id,
        output,
    )

  def on_tool_error(self, error, *, run_id, **kwargs):
    logger.error("工具异常 run_id=%s error=%s", run_id, error)

@wrap_tool_call
def handle_workspace_tool_error(request, handler):
    """Return expected lookup failures as tool results the model can act on."""
    try:
        return handler(request)
    except RecordNotFoundError as exc:
        return ToolMessage(
            content=str(exc),
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
    logger.info(
        "本轮开始 workspace=%s thread=%s",
        workspace_id,
        thread_id,
    )
    detail = store.get_workspace_detail(workspace_id)
    # Initialize once per workspace, including history in sibling conversations.
    initialize_research = (
        not any(
            value.strip() if isinstance(value, str) else value
            for value in detail.info.model_dump().values()
        )
        and not store.has_user_messages(workspace_id)
    )
    user_message = store.add_message(workspace_id, thread_id, "user", question)

    # Refresh research state and other-thread references on every turn.
    context = {
        "title": detail.title,
        **detail.info.model_dump(),
        "other_threads": [
            {"id": item.id, "title": item.title}
            for item in detail.threads if item.id != thread_id
        ],
    }
    agent = create_agent(
        model,
        tools=create_workspace_tools(store.root, workspace_id),
        system_prompt=build_workspace_prompt(
            context, initialize_research=initialize_research
        ),
        middleware=[
            ModelCallLimitMiddleware(run_limit=10, exit_behavior="error"),
            handle_workspace_tool_error,
        ],
        checkpointer=checkpointer,
    )

    config = {
        "configurable": {
            "thread_id": f"{workspace_id}:{thread_id}",
        },
      "callbacks": [ToolLogHandler()],
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
    logger.info(
        "本轮完成 workspace=%s thread=%s",
        workspace_id,
        thread_id,
    )
    return answer

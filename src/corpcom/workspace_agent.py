"""Continue one research conversation using its persistent workspace context."""

import logging
from collections.abc import Callable

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, wrap_tool_call
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from corpcom.workspace import RecordNotFoundError, WorkspaceStore
from corpcom.workspace_models import Message
from corpcom.workspace_prompt import build_workspace_prompt
from corpcom.workspace_tools import create_workspace_tools

logger = logging.getLogger(__name__)


class ToolLogHandler(BaseCallbackHandler):
  def __init__(
      self,
      on_event: Callable[[dict[str, object]], None] | None = None,
  ):
    self.on_event = on_event

  def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
    logger.info(
        "工具开始 name=%s run_id=%s input=%s",
        serialized["name"],
        run_id,
        input_str,
    )
    if self.on_event is not None:
      self.on_event({
        "type": "tool_start",
        "tool_run_id": str(run_id),
        "name": serialized["name"],
      })

  def on_tool_end(self, output, *, run_id, **kwargs):
    logger.info(
        "工具返回 name=%s run_id=%s output=%s",
        kwargs["name"],
        run_id,
        output,
    )
    if self.on_event is not None:
      self.on_event({
        "type": "tool_end",
        "tool_run_id": str(run_id),
      })

  def on_tool_error(self, error, *, run_id, **kwargs):
    logger.error("工具异常 run_id=%s error=%s", run_id, error)
    if self.on_event is not None:
      self.on_event({
        "type": "tool_error",
        "tool_run_id": str(run_id),
        "error": str(error),
      })

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


def create_workspace_agent(
    store: WorkspaceStore, model: BaseChatModel,
    workspace_id: str, thread_id: str, checkpointer: BaseCheckpointSaver,
    *, is_first_message: bool = False,
):
  """Build the same graph for research execution and checkpoint recovery."""
  detail = store.get_workspace_detail(workspace_id)

  initialize_research = is_first_message and not any(
      value.strip() if isinstance(value, str) else value
      for value in detail.info.model_dump().values()
  )

  # Refresh research state and other-thread references on every turn.
  context = {
    "title": detail.title,
    **detail.info.model_dump(),
    "other_threads": [
      {"id": item.id, "title": item.title}
      for item in detail.threads if item.id != thread_id
    ],
  }
  return create_agent(
      model,
      tools=create_workspace_tools(store.root, workspace_id),
      system_prompt=build_workspace_prompt(
          context, initialize_research=initialize_research
      ),
      middleware=[
        ModelCallLimitMiddleware(run_limit=100, exit_behavior="error"),
        handle_workspace_tool_error,
      ],
      checkpointer=checkpointer,
  )


def ask_workspace(
    store: WorkspaceStore, model: BaseChatModel,
    workspace_id: str, thread_id: str, question: str,
    checkpointer: BaseCheckpointSaver, *,
    on_event: Callable[[dict[str, object]], None] | None = None,
) -> AIMessage:
  """Save the user's question and generate the answer."""
  logger.info("本轮开始 workspace=%s thread=%s", workspace_id, thread_id)
  user_message, is_first = store.add_user_message(workspace_id, thread_id, question)
  logger.info("已保存用户消息 workspace=%s thread=%s question=%s...",
              workspace_id, thread_id, question[:20])
  agent = create_workspace_agent(
      store, model, workspace_id, thread_id, checkpointer, is_first_message=is_first,
  )
  config = {
    "configurable": {
      "thread_id": f"{workspace_id}:{thread_id}",
    },
    "callbacks": [ToolLogHandler(on_event)],
  }

  # Import existing conversation history only before its first checkpoint.
  if checkpointer.get_tuple(config) is None:
    messages = store.get_thread(workspace_id, thread_id).messages
  else:
    messages = [user_message]
  input_messages = [
    {
      "id": f"business:{item.id}",
      "role": item.role,
      "content": item.content,
    } for item in messages
  ]
  answer: AIMessage | None = None
  for mode, data in agent.stream(
      {"messages": input_messages}, config,
      stream_mode=["messages", "updates"]
  ):
    if mode == "updates":
      if "model" not in data:
        continue

      message = data["model"]["messages"][-1]
      if message.tool_calls:
        # Persist completed text before executing its tool calls.
        store.add_message(
            workspace_id,
            thread_id,
            "assistant",
            message.text,
            model_message_id=message.id,
            tool_calls=message.tool_calls,
        )
      else:
        # The coordinator saves the final answer with the success status.
        answer = message

    else:
      chunk, metadata = data
      if (
        metadata["langgraph_node"] == "model"
        and chunk.text
        and on_event is not None
      ):
        on_event({
          "type": "text_delta",
          "message_id": chunk.id,
          "text": chunk.text,
        })
  if answer is None or not answer.text.strip():
    raise RuntimeError("model returned no answer")
  logger.info(
      "回答生成完成 workspace=%s thread=%s",
      workspace_id,
      thread_id,
  )
  return answer

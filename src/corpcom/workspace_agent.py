"""Continue one research conversation using its persistent workspace context."""

import logging
import os
from collections.abc import Callable
from pathlib import Path

from langchain_core.messages.utils import count_tokens_approximately
from pydantic import env_settings
from redis.exceptions import RedisError

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, \
  wrap_tool_call, wrap_model_call
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphBubbleUp

from corpcom.event_types import ToolEvent, AgentEvent
from corpcom.workspace import RecordNotFoundError, WorkspaceStore
from corpcom.workspace_prompt import build_workspace_prompt
from corpcom.workspace_tools import create_workspace_tools

logger = logging.getLogger(__name__)


class ToolLogHandler(BaseCallbackHandler):
  # Propagate event persistence failures to the run coordinator.
  raise_error = True
  def __init__(
      self,
      on_event: Callable[[ToolEvent], None] | None = None,
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
        "tool_call_id": kwargs["tool_call_id"],
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
        "tool_call_id": output.tool_call_id,
        "content": output.content,
        "status": output.status,
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
  """Return tool failures as messages while preserving graph control flow."""
  try:
    return handler(request)
  except (GraphBubbleUp, RedisError):
    raise
  except Exception as exc:
    logger.exception(
        "工具执行失败 name=%s tool_call_id=%s",
        request.tool_call["name"],
        request.tool_call["id"],
    )
    return ToolMessage(
        content=f"{type(exc).__name__}: {exc}",
        tool_call_id=request.tool_call["id"],
        name=request.tool_call["name"],
        status="error",
    )

def compact_model_input_tokens(
    request,
    handler,
    working_note: str,
    render_prompt: Callable[[str], str]
):
  """Fit history, tool results and the working note to the model input budget."""
  system_message = request.system_message
  messages = request.messages
  count = count_tokens_approximately(
      [request.system_message, *messages],
      tools=request.tools,
  )
  raw_budget = os.getenv("CORPCOM_MODEL_INPUT_TOKEN_BUDGET")
  if raw_budget is not None:
    try:
      budget = int(raw_budget)
    except ValueError as exc:
      raise ValueError("CORPCOM_MODEL_INPUT_TOKEN_BUDGET 必须是正整数") from exc
    if budget <= 0:
      raise ValueError("CORPCOM_MODEL_INPUT_TOKEN_BUDGET 必须是正整数")

    while count > budget:
      next_turn = next(
          (
            index for index, message in enumerate(messages[1:], start=1)
            if isinstance(message, HumanMessage)
          ),
          None,
      )
      if next_turn is None:
        break

      messages = messages[next_turn:]
      count = count_tokens_approximately(
          [request.system_message, *messages],
          tools=request.tools,
      )

    if count > budget:
      messages = list(messages)
      marker = "\n[工具结果已截断]"
      for index, message in enumerate(messages):
        if count <= budget:
          break
        if not isinstance(message, ToolMessage) or message.status != "success":
          continue
        if not isinstance(message.content, str) or len(message.content) <= len(marker):
          continue

        original = message.content
        low, high = 0, len(original) - len(marker)
        while low < high:
          middle = (low + high + 1) // 2
          messages[index] = message.model_copy(
              update={"content": original[:middle] + marker}
          )
          candidate_count = count_tokens_approximately(
              [request.system_message, *messages], tools=request.tools,
          )
          if candidate_count <= budget:
            low = middle
          else:
            high = middle - 1
        messages[index] = message.model_copy(
            update={"content": original[:low] + marker}
        )
        count = count_tokens_approximately(
            [request.system_message, *messages], tools=request.tools,
        )
      if count > budget:
        marker = "\n［工作笔记已截断，可读取 agent.md 查看全文］"
        low, high = 0, len(working_note) - 1
        while low <= high:
          middle = (low + high) // 2
          candidate = request.system_message.model_copy(update={
              "content": render_prompt(working_note[:middle] + marker),
          })
          candidate_count = count_tokens_approximately(
              [candidate, *messages], tools=request.tools,
          )
          if candidate_count <= budget:
            system_message = candidate
            count = candidate_count
            low = middle + 1
          else:
            high = middle - 1
      if count > budget:
        raise RuntimeError("模型输入超过预算，无法缩短到预算范围内")

  logger.info("模型输入估算 tokens=%d", count)
  return handler(request.override(system_message=system_message, messages=messages))

def create_workspace_agent(
    store: WorkspaceStore, model: BaseChatModel,
    workspace_id: str, thread_id: str, checkpointer: BaseCheckpointSaver,
    *, is_first_message: bool = False, user_memory_dir: Path | None = None,
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
  working_note = store.read_working_note(workspace_id)
  if user_memory_dir is None:
    user_memory_dir = store.root / "user" / "memory"
  user_index = store.read_memory_index(workspace_id, "user", user_memory_dir)
  workspace_index = store.read_memory_index(workspace_id, "workspace", user_memory_dir)
  if user_index or workspace_index:
    context["memory_indexes"] = {"user": user_index, "workspace": workspace_index}

  def render_prompt(note: str) -> str:
    return build_workspace_prompt(
        {**context, "working_note": note}, initialize_research=initialize_research,
    )

  @wrap_model_call
  def apply_input_budget(request, handler):
    return compact_model_input_tokens(request, handler, working_note, render_prompt)

  return create_agent(
      model,
      tools=create_workspace_tools(store.root, workspace_id),
      system_prompt=render_prompt(working_note),
      middleware=[
        ModelCallLimitMiddleware(run_limit=100, exit_behavior="error"),
        handle_workspace_tool_error,
        apply_input_budget,
      ],
      checkpointer=checkpointer,
  )


def ask_workspace(
    store: WorkspaceStore, model: BaseChatModel,
    workspace_id: str, thread_id: str, question: str,
    checkpointer: BaseCheckpointSaver, *,
    on_event: Callable[[AgentEvent], None] | None = None,
    user_memory_dir: Path | None = None,
) -> AIMessage:
  """Save the user's question and generate the answer."""
  logger.info("本轮开始 workspace=%s thread=%s", workspace_id, thread_id)
  user_message, is_first = store.add_user_message(workspace_id, thread_id, question)
  logger.info("已保存用户消息 workspace=%s thread=%s question=%s...",
              workspace_id, thread_id, question[:20])
  agent = create_workspace_agent(
      store, model, workspace_id, thread_id, checkpointer, is_first_message=is_first,
      user_memory_dir=user_memory_dir,
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

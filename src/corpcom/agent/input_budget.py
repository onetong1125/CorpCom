"""Fit the workspace context and conversation to the model input budget."""

import logging
import os
from collections.abc import Callable

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

logger = logging.getLogger(__name__)


def compact_model_input_tokens(
    request,
    handler,
    working_note: str,
    memory_indexes: dict[str, str],
    render_prompt: Callable[[str, dict[str, str]], str],
):
  """Fit history, tool results, and research memory to the model input budget."""
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

    if count > budget and any(memory_indexes.values()):
      index_lines = {
          scope: index.splitlines(keepends=True)
          for scope, index in memory_indexes.items()
      }
      # Try the same line limit for each scope so both can contribute entries.
      low, high = 1, max(map(len, index_lines.values())) - 1
      memory_indexes = {}
      system_message = request.system_message.model_copy(update={
          "content": render_prompt(working_note, memory_indexes),
      })
      count = count_tokens_approximately([system_message, *messages], tools=request.tools)
      while count <= budget and low <= high:
        middle = (low + high) // 2
        candidate_indexes = {
            scope: "".join(lines[:middle]) for scope, lines in index_lines.items()
        }
        candidate = request.system_message.model_copy(update={
            "content": render_prompt(working_note, candidate_indexes),
        })
        candidate_count = count_tokens_approximately([candidate, *messages], tools=request.tools)
        if candidate_count <= budget:
          memory_indexes = candidate_indexes
          system_message = candidate
          count = candidate_count
          low = middle + 1
        else:
          high = middle - 1

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
              [system_message, *messages], tools=request.tools,
          )
          if candidate_count <= budget:
            low = middle
          else:
            high = middle - 1
        messages[index] = message.model_copy(
            update={"content": original[:low] + marker}
        )
        count = count_tokens_approximately(
            [system_message, *messages], tools=request.tools,
        )
      if count > budget:
        marker = "\n［工作笔记已截断，可读取 agent.md 查看全文］"
        low, high = 0, len(working_note) - 1
        while low <= high:
          middle = (low + high) // 2
          candidate = request.system_message.model_copy(update={
              "content": render_prompt(working_note[:middle] + marker, memory_indexes),
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

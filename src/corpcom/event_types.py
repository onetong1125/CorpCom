"""Contracts for research events."""
from collections.abc import Callable, Mapping
from typing import TypedDict, Literal, Any

from langchain_core.messages import ToolCall

# -------------- Run Progress Event --------------
class StartedEvent(TypedDict):
  """after admitting a run for the thread."""

  type: Literal["started"]
  workspace_id: str
  thread_id: str
  run_id: str

class TextDeltaEvent(TypedDict):
  """a text fragment from the model's streaming response."""

  type: Literal["text_delta"]
  message_id: str
  text: str

class ToolStartEvent(TypedDict):
  """A tool execution to the model's call."""

  type: Literal["tool_start"]
  tool_run_id: str
  tool_call_id: str
  name: str

class ToolEndEvent(TypedDict):
  """The returned tool message and its status."""

  type: Literal["tool_end"]
  tool_run_id: str
  tool_call_id: str
  content: str | list[str | dict[Any, Any]]
  status: Literal["success", "error"]

class ToolErrorEvent(TypedDict):
  """The exception for a tool execution."""

  type: Literal["tool_error"]
  tool_run_id: str
  error: str

ToolEvent = ToolStartEvent | ToolEndEvent | ToolErrorEvent
AgentEvent = ToolEvent | TextDeltaEvent

# The recorder writes it to Redis for replay. The coordinator archives it
# in SQLite when saving run success or failure.
RunProgressEvent = StartedEvent | AgentEvent

# -------------- Run Terminal Event --------------
# After committing the final run status to SQLite, the coordinator sends a live
# success or failure notification.
class RunFailedEvent(TypedDict):
  type: Literal["failed"]
  error: str

class RunSucceededEvent(TypedDict):
  type: Literal["succeeded"]
  message: dict[str, object]

# -------------- Replay Event --------------
class RunFinishedEvent(TypedDict):
  """replay finished event, for one-time"""
  type: Literal["run_finished"]
  run_id: str
  status: Literal["succeeded", "failed", "interrupted"]
  error: str | None

RunEventSink = Callable[[Mapping[str, object]], None]

# -------------- Checkpoint Event --------------
class AssistantMessageEvent(TypedDict):
  """The history reader builds this event for each requested assistant message.

  It uses a checkpoint message or combines recorded text deltas on read.
  The resulting event exists only in the history response.
  """

  type: Literal["assistant_message"]
  message_id: str | None
  text: str
  tool_calls: list[ToolCall]

class ToolResultEvent(TypedDict):
  """project_run_messages builds this history event from a checkpoint ToolMessage.

  The checkpoint retains the tool result. Each history read creates the event.
  """

  type: Literal["tool_result"]
  message_id: str | None
  tool_call_id: str
  content: str | list[str | dict[Any, Any]]
  status: Literal["success", "error"]

CheckpointEvent = AssistantMessageEvent | ToolResultEvent

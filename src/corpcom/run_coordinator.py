from uuid import uuid4
from threading import BoundedSemaphore
from collections.abc import Callable
from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain_core.language_models import BaseChatModel
from redis import Redis

from corpcom.workspace import WorkspaceStore
from corpcom.workspace_models import Message
from corpcom.workspace_agent import ask_workspace
from corpcom.workspace_recovery import recover_interrupted_checkpoint
from corpcom.run_events import RunEventStream


class ExecutionCapacityError(ValueError):
  """The backend has reached its concurrent execution limit."""


class RunCoordinator:
  def __init__(self,
      checkpointer: BaseCheckpointSaver,
      redis_client: Redis,
      *,
      max_concurrent_runs: int,
  ):
    self.checkpointer = checkpointer
    self._redis = redis_client
    self._slots = BoundedSemaphore(max_concurrent_runs)

  def run(self,
      store: WorkspaceStore,
      model: BaseChatModel,
      workspace_id: str,
      thread_id: str,
      question: str,
      *,
      on_event: Callable[[dict[str, object]], None] | None = None,
  ) -> Message:
    if not self._slots.acquire(blocking=False):
      raise ExecutionCapacityError("Too many concurrent executions")
    try:
      # 准入
      run_id = uuid4().hex
      previous = store.start_thread_execution(
          workspace_id, thread_id, run_id=run_id,
      )

      try:
        event_stream = RunEventStream(self._redis, run_id)

        def emit_event(event: dict[str, object]) -> None:
          """Persist each event before notifying the subscriber."""
          event_stream.append(event)
          if on_event is not None:
            on_event(event)

        started_event = {
          "type": "started",
          "workspace_id": workspace_id,
          "thread_id": thread_id,
          "run_id": run_id,
        }
        emit_event(started_event)

        # 恢复中断后的工具结果
        if previous.execution_status == "interrupted":
          recover_interrupted_checkpoint(
              store, model, workspace_id, thread_id, self.checkpointer
          )
        answer = ask_workspace(
            store, model, workspace_id, thread_id, question, self.checkpointer,
            on_event=emit_event
        )
        message = store.complete_thread_execution(
            workspace_id, thread_id, answer.text, model_message_id=answer.id)

      except Exception as e:
        store.finish_thread_execution(workspace_id, thread_id,
                                      status="failed", error=str(e))
        if on_event is not None:
          on_event({
            "type": "failed",
            "error": str(e)
          })
        raise
      else:
        if on_event is not None:
          on_event({
            "type": "succeeded",
            "message": message.model_dump(mode="json"),
          })
        return message
    finally:
      self._slots.release()

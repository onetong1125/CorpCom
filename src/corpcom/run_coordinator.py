from pathlib import Path
from uuid import uuid4
from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain_core.language_models import BaseChatModel
from redis import Redis
from threading import BoundedSemaphore

from corpcom.workspace import WorkspaceStore
from corpcom.workspace_models import Message
from corpcom.workspace_agent import ask_workspace
from corpcom.workspace_recovery import recover_interrupted_checkpoint
from corpcom.run_events import RunEventRecorder, RunEventStream
from corpcom.event_types import StartedEvent, RunFailedEvent, RunSucceededEvent, RunEventSink


class ExecutionCapacityError(ValueError):
  """The backend has reached its concurrent execution limit."""


class RunCoordinator:
  def __init__(self,
      checkpointer: BaseCheckpointSaver,
      redis_client: Redis,
      *,
      max_concurrent_runs: int,
      user_memory_dir: Path | None = None,
  ):
    self.checkpointer = checkpointer
    self._redis = redis_client
    self._user_memory_dir = user_memory_dir
    self._slots = BoundedSemaphore(max_concurrent_runs)

  def run(self,
      store: WorkspaceStore,
      model: BaseChatModel,
      workspace_id: str,
      thread_id: str,
      question: str,
      *,
      on_event: RunEventSink | None = None,
  ) -> Message:
    if not self._slots.acquire(blocking=False):
      raise ExecutionCapacityError("Too many concurrent executions")
    try:
      # 准入
      run_id = uuid4().hex
      event_stream = RunEventStream(self._redis, run_id)
      recorder = RunEventRecorder(event_stream, on_event)
      previous = store.start_thread_execution(
          workspace_id, thread_id, run_id=run_id,
      )

      try:
        started_event: StartedEvent = {
          "type": "started",
          "workspace_id": workspace_id,
          "thread_id": thread_id,
          "run_id": run_id,
        }
        recorder.emit(started_event)

        # 恢复中断后的工具结果
        if previous.execution_status == "interrupted":
          recover_interrupted_checkpoint(
              store, model, workspace_id, thread_id, self.checkpointer,
              user_memory_dir=self._user_memory_dir,
          )
        answer = ask_workspace(
            store, model, workspace_id, thread_id, question, self.checkpointer,
            on_event=recorder.emit, user_memory_dir=self._user_memory_dir,
        )
        message = store.complete_thread_execution(
            workspace_id, thread_id, answer.text, model_message_id=answer.id,
            sse_events=recorder.events,
        )

      except Exception as e:
        store.finish_thread_execution(
            workspace_id, thread_id, status="failed", error=str(e),
            sse_events=recorder.events,
        )
        if on_event is not None:
          failed_event: RunFailedEvent = {
            "type": "failed",
            "error": str(e)
          }
          on_event(failed_event)
        raise
      else:
        if on_event is not None:
          succeeded_event: RunSucceededEvent = {
            "type": "succeeded",
            "message": message.model_dump(mode="json"),
          }
          on_event(succeeded_event)
        return message
    finally:
      self._slots.release()

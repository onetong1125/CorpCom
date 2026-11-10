from threading import BoundedSemaphore
from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain_core.language_models import BaseChatModel

from corpcom.workspace import WorkspaceStore
from corpcom.workspace_models import Message
from corpcom.workspace_agent import ask_workspace
from corpcom.workspace_recovery import recover_interrupted_checkpoint


class ExecutionCapacityError(ValueError):
  """The backend has reached its concurrent execution limit."""


class RunCoordinator:
  def __init__(self,
    checkpointer: BaseCheckpointSaver,
    *,
    max_concurrent_runs: int,
  ):
    self.checkpointer = checkpointer
    self._slots = BoundedSemaphore(max_concurrent_runs)

  def run(self,
      store: WorkspaceStore,
      model: BaseChatModel,
      workspace_id: str,
      thread_id: str,
      question: str,
  ) -> Message:
    if not self._slots.acquire(blocking=False):
      raise ExecutionCapacityError("Too many concurrent executions")
    try:
      # 准入
      previous = store.start_thread_execution(workspace_id, thread_id)

      try:
        if previous.execution_status == "interrupted":
          recover_interrupted_checkpoint(
              store, model, workspace_id, thread_id, self.checkpointer)
        answer = ask_workspace(
            store, model, workspace_id, thread_id, question, self.checkpointer)
        return store.complete_thread_execution(workspace_id, thread_id, answer)

      except Exception as e:
        store.finish_thread_execution(workspace_id, thread_id,
                                      status="failed", error=str(e))
        raise
    finally:
      self._slots.release()

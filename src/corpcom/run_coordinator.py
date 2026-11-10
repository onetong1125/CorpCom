from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain_core.language_models import BaseChatModel

from corpcom.workspace import WorkspaceStore
from corpcom.workspace_models import Message
from corpcom.workspace_agent import ask_workspace


class RunCoordinator:
  def __init__(self, checkpointer: BaseCheckpointSaver):
    self.checkpointer = checkpointer

  def run(self,
      store: WorkspaceStore,
      model: BaseChatModel,
      workspace_id: str,
      thread_id: str,
      question: str,
  ) -> Message:
    # 准入
    store.start_thread_execution(workspace_id, thread_id)

    try:
      answer = ask_workspace(
          store, model, workspace_id, thread_id, question, self.checkpointer)
      return store.complete_thread_execution(workspace_id, thread_id, answer)

    except Exception as e:
      store.finish_thread_execution(workspace_id, thread_id,
                                    status="failed", error=str(e))
      raise

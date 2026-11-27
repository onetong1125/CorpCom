import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from contextlib import asynccontextmanager

from anyio import to_thread
import asyncio
from anyio.abc import TaskGroup
from langchain_core.language_models import BaseChatModel

from corpcom.event_types import RunEventSink
from corpcom.runs.coordinator import RunCoordinator, ExecutionCapacityError
from corpcom.workspace import WorkspaceStore, RecordNotFoundError, \
  ThreadBusyError
from corpcom.workspace_models import Message

logger = logging.getLogger(__name__)

def execute_research(
    root: Path,
    coordinator: RunCoordinator,
    workspace_id: str,
    thread_id: str,
    question: str,
    model: BaseChatModel,
    on_event: RunEventSink,
) -> Message:
  with WorkspaceStore(root) as store:
    store.get_thread(workspace_id, thread_id)
    return coordinator.run(
        store, model, workspace_id, thread_id, question,
        on_event=on_event,
    )


async def run_research(
    root: Path,
    coordinator: RunCoordinator,
    workspace_id: str,
    thread_id: str,
    question: str,
    model: BaseChatModel,
    publish: Callable[[Mapping[str, object] | Exception | None], None],
) -> None:
  try:
    await to_thread.run_sync(
        execute_research,
        root, coordinator, workspace_id, thread_id, question, model, publish,
    )
  except (RecordNotFoundError, ThreadBusyError, ExecutionCapacityError) as exc:
    publish(exc)
  except Exception as exc:
    logger.exception(
        "Background research failed workspace=%s thread=%s",
        workspace_id, thread_id,
    )
    publish(exc)
  finally:
    publish(None)

@asynccontextmanager
async def research_events(
    root: Path,
    coordinator: RunCoordinator,
    task_group: TaskGroup,
    workspace_id: str,
    thread_id: str,
    question: str,
    model: BaseChatModel,
):
  loop = asyncio.get_running_loop()
  queue: asyncio.Queue[Mapping[str, object] | Exception | None]  = asyncio.Queue()
  connected = True
  def enqueue(item):
    if connected:
      queue.put_nowait(item)

  def publish(item: Mapping[str, object] | Exception | None) -> None:
    loop.call_soon_threadsafe(enqueue, item)

  task_group.start_soon(
      run_research,
      root, coordinator, workspace_id, thread_id, question,
      model, publish
  )

  try:
    first = await queue.get()

    if isinstance(first, Exception):
      raise first

    async def events():
      yield first
      while True:
        item = await queue.get()
        if item is None:
          return
        if isinstance(item, Exception):
          raise item
        yield item
        if item["type"] in ("succeeded", "failed"):
          return

    yield events()
  finally:
    connected = False

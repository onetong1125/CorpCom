"""Read Redis events and execution state for stream recovery."""
from contextlib import asynccontextmanager, aclosing
from pathlib import Path

from anyio import to_thread
from redis import Redis

from corpcom.runs.events import RunEventStream
from corpcom.event_types import RunFinishedEvent
from corpcom.workspace import WorkspaceStore
from corpcom.workspace_models import ResearchRun

def read_replay_batch(
  root: Path,
  redis_client: Redis,
  workspace_id: str,
  thread_id: str,
  run_id: str,
  after: str,
) -> tuple[ResearchRun, list[tuple[str, dict[str, object]]]]:
  """Read state before events so terminal runs can be drained safely."""
  with WorkspaceStore(root) as store:
    run = store.get_research_run(
        workspace_id, thread_id, run_id
    )
  stream = RunEventStream(redis_client, run_id)
  events = stream.read_after(
      after,
      block_ms=1000 if run.status == "running" else None,
  )
  return run, events

@asynccontextmanager
async def replay_events(
  root: Path,
  redis_client: Redis,
  workspace_id: str,
  thread_id: str,
  run_id: str,
  after: str,
):
  initial_run, initial_batch = await to_thread.run_sync(
      read_replay_batch,
      root, redis_client, workspace_id, thread_id, run_id, after
  )

  async def events():
    run = initial_run
    batch = initial_batch
    cursor = after

    while True:
      for event_id, event in batch:
        yield {**event, "event_id": event_id}
        cursor = event_id

      # A terminal state alone is insufficient: drain Redis first.
      if not batch and run.status != "running":
        finished_event: RunFinishedEvent = {
          "type": "run_finished",
          "run_id": run.id,
          "status": run.status,
          "error": run.error,
        }
        yield finished_event
        return

      run, batch = await to_thread.run_sync(
          read_replay_batch,
          root, redis_client, workspace_id, thread_id, run_id, cursor
      )

  async with aclosing(events()) as stream:
    yield stream

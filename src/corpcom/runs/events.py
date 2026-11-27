"""Redis event storage for research replay."""

import json
from copy import deepcopy
from threading import Lock

from redis import Redis

from corpcom.event_types import RunProgressEvent, RunEventSink


class RunEventStream:
  """Store one run's events using a client with decode_responses=True."""

  def __init__(self, client: Redis, run_id: str):
    self._client = client
    self._key = f"corpcom:runs:{run_id}:events"

  def append(self, event: RunProgressEvent) -> str:
    """Append an event and return its Redis stream ID."""
    payload = json.dumps(
        event, ensure_ascii=False, allow_nan=False,
    )
    return self._client.xadd(self._key, {"event": payload})

  def read_after(
      self,
      event_id: str,
      *,
      block_ms: int | None = None,
  ) -> list[tuple[str, dict[str, object]]]:
    """Read up to 100 events strictly after the supplied ID."""
    batches = self._client.xread(
        {self._key: event_id},
        count=100,
        block=block_ms,
    )
    return [
      (entry_id, json.loads(fields["event"]))
      for _, entries in batches
      for entry_id, fields in entries
    ]


class RunEventRecorder:
  def __init__(
      self,
      stream: RunEventStream,
      on_event: RunEventSink | None
  ):
    self.stream = stream
    self.on_event = on_event
    self.events: list[RunProgressEvent] = []
    self._lock = Lock()

  def emit(self, event: RunProgressEvent) -> None:
    """Persist each event before notifying the subscriber."""
    with self._lock:
      snapshot = deepcopy(event)
      self.events.append(snapshot)
      event_id = self.stream.append(snapshot)
      if self.on_event is not None:
        self.on_event({**event, "event_id": event_id})

"""Redis event storage for research replay."""

import json

from redis import Redis


class RunEventStream:
  """Store one run's events using a client with decode_responses=True."""

  def __init__(self, client: Redis, run_id: str):
    self._client = client
    self._key = f"corpcom:runs:{run_id}:events"

  def append(self, event: dict[str, object]) -> str:
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
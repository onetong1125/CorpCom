"""Typed workspace records and the detail views returned by local storage."""

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue, PlainSerializer


# Keep datetime attributes in Python and preserve SQLite's UTC millisecond JSON format.
Timestamp = Annotated[
    datetime,
    PlainSerializer(
        lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        return_type=str,
        when_used="json",
    ),
]


class Workspace(BaseModel):
    """Research metadata shared across conversations, without loading their contents."""

    id: str
    title: str
    goal: str
    scope: str
    observation_period: str
    initial_hypothesis: str
    preferences: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: Timestamp
    updated_at: Timestamp


class Thread(BaseModel):
    """A conversation belonging to one workspace; messages are loaded separately."""

    id: str
    workspace_id: str
    title: str
    created_at: Timestamp


class Message(BaseModel):
    """One saved message; the thread determines its workspace ownership."""

    id: int
    thread_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: Timestamp


class Artifact(BaseModel):
    """File metadata with a path relative to the data directory."""

    id: str
    workspace_id: str
    name: str
    kind: str
    relative_path: str
    created_at: Timestamp


class WorkspaceSummary(BaseModel):
    """The fields shown by the workspace list command."""

    id: str
    title: str
    goal: str
    created_at: Timestamp
    updated_at: Timestamp


# Detail views extend records to preserve the existing flat CLI JSON structure.
class WorkspaceDetail(Workspace):
    """Workspace metadata plus indexes, without message history or file contents."""

    threads: list[Thread]
    artifacts: list[Artifact]


class ThreadDetail(Thread):
    """A conversation with its messages in insertion order."""

    messages: list[Message]


class ArtifactFile(Artifact):
    """File metadata plus the absolute path resolved by storage."""

    path: Path

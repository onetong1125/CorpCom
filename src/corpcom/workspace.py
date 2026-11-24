"""Store research in workspace JSON, with conversations and file indexes in SQLite."""

import json
import os
import sqlite3
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue

from corpcom.artifacts import ArtifactStore
from corpcom.errors import RecordNotFoundError
from corpcom.event_types import RunProgressEvent
from corpcom.workspace_models import (
  ArtifactFile, Message, Thread, ThreadDetail,
  Workspace, WorkspaceDetail, WorkspaceResearchInfo, WorkspaceSummary,
  ResearchRun, ResearchRunSummary
)

# Threads and files belong to a workspace; messages belong through their thread.
# Research fields live in research.json. Artifact rows index files on disk.
SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    execution_status TEXT CHECK (execution_status IN ('running', 'succeeded', 'failed', 'interrupted')),
    execution_started_at TEXT,
    execution_finished_at TEXT,
    execution_error TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    model_message_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    tool_calls TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_thread_model_message
ON messages (thread_id, model_message_id);
CREATE TABLE IF NOT EXISTS research_runs (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    run_index INTEGER NOT NULL CHECK (run_index >= 0),
    user_message_id INTEGER UNIQUE REFERENCES messages(id),
    status TEXT NOT NULL DEFAULT 'running' CHECK (
        status IN ('running', 'succeeded', 'failed', 'interrupted')
    ),
    started_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    finished_at TEXT,
    error TEXT,
    sse_events TEXT,
    UNIQUE (thread_id, run_index)
);
CREATE INDEX IF NOT EXISTS ix_research_runs_thread
ON research_runs (thread_id);
CREATE TABLE IF NOT EXISTS tool_executions (
    id TEXT PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    tool_call_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'returned', 'error', 'interrupted')
    ),
    output TEXT,
    error TEXT,
    started_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    finished_at TEXT,
    UNIQUE (message_id, tool_call_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


class ThreadBusyError(ValueError):
  """The thread already has an execution in progress."""

def _require_text(**fields: str) -> None:
  """Reject blank required input without changing valid text formatting."""
  for name, value in fields.items():
    if not value.strip():
      raise ValueError(f"{name} must not be empty")

def _message_from_row(row: sqlite3.Row) -> Message:
  """Decode stored tool calls before building a message."""
  data = dict(row)
  data["tool_calls"] = json.loads(data["tool_calls"])
  return Message.model_validate(data)


EMPTY_WORKING_NOTE = """# 工作笔记

## 当前进展与下一步

## 会话与关键发现

## 资料与产物
"""


class WorkspaceStore:
  """Manage one data directory; use with to close the connection on exit.

  Each mutation commits its own transaction; the outer with manages the connection.
  Thread and file operations require the owning workspace ID to prevent mixing research.
  Imported files are copies, so later changes to the source do not affect them.
  """

  def __init__(self, root: Path):
    self.root = root.resolve()
    self.root.mkdir(parents=True, exist_ok=True)
    self.db = sqlite3.connect(self.root / "corpcom.db")
    self.db.row_factory = sqlite3.Row
    # Enable foreign keys per connection to reject references to missing parents.
    self.db.execute("PRAGMA foreign_keys = ON")
    self.db.executescript(SCHEMA)
    self.artifacts = ArtifactStore(self.db, self.root, self._touch)

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc_value, traceback):
    self.db.close()

  def get_workspace(self, workspace_id: str) -> Workspace:
    """Combine workspace identity with research.json without loading related records."""
    row = self.db.execute(
        "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
    ).fetchone()
    if row is None:
      raise RecordNotFoundError(f"workspace not found: {workspace_id}")
    path = self.root / "workspaces" / workspace_id / "research.json"
    research = WorkspaceResearchInfo.model_validate_json(path.read_text(encoding="utf-8"))
    return Workspace(**dict(row), info=research)

  def _write_research(self, workspace_id: str, research: WorkspaceResearchInfo) -> None:
    """Replace a complete JSON file while the caller holds SQLite's write lock."""
    content = json.dumps(research.model_dump(), ensure_ascii=False, allow_nan=False, indent=2)
    path = self.root / "workspaces" / workspace_id / "research.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    try:
      temporary.write_text(content + "\n", encoding="utf-8")
      temporary.replace(path)
    finally:
      temporary.unlink(missing_ok=True)

  def _ensure_working_note(self, workspace_id: str) -> Path:
    path = self.root / "workspaces" / workspace_id / "agent.md"
    try:
      note = path.open("x", encoding="utf-8")
    except FileExistsError:
      return path
    with note:
      note.write(EMPTY_WORKING_NOTE)
    return path

  def read_working_note(self, workspace_id: str) -> str:
    """Read the current note, initializing it on first use."""
    self.get_workspace(workspace_id)
    path = self._ensure_working_note(workspace_id)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as note:
      return note.read()

  def _thread(self, workspace_id: str, thread_id: str) -> Thread:
    # Check ownership before reading or adding messages, not just the thread ID.
    row = self.db.execute(
        "SELECT * FROM threads WHERE workspace_id = ? AND id = ?",
        (workspace_id, thread_id),
    ).fetchone()
    if row is None:
      raise RecordNotFoundError(f"thread not found in workspace: {thread_id}")
    return Thread.model_validate(dict(row))

  def _touch(self, workspace_id: str) -> None:
    """Update the workspace timestamp inside the caller's transaction without committing."""
    self.db.execute(
        "UPDATE workspaces SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
        (workspace_id,),
    )

  def create_workspace(
      self, title: str, goal: str = "", scope: str = "", observation_period: str = "",
      initial_hypothesis: str = "", preferences: dict[str, JsonValue] | None = None,
  ) -> Workspace:
    """Create research with optional fields in the workspace's research.json."""
    _require_text(title=title)
    research = WorkspaceResearchInfo(
        goal=goal, scope=scope, observation_period=observation_period,
        initial_hypothesis=initial_hypothesis,
        preferences={} if preferences is None else preferences,
    )
    workspace_id = uuid4().hex
    with self.db:
      self.db.execute(
          "INSERT INTO workspaces (id, title) VALUES (?, ?)",
          (workspace_id, title),
      )
      self._write_research(workspace_id, research)
      self._ensure_working_note(workspace_id)
    return self.get_workspace(workspace_id)

  def list_workspaces(self) -> list[WorkspaceSummary]:
    rows = self.db.execute("SELECT * FROM workspaces ORDER BY rowid")
    return [
        WorkspaceSummary(**row, goal=self.get_workspace(row["id"]).info.goal)
        for row in rows
    ]

  def get_workspace_detail(self, workspace_id: str) -> WorkspaceDetail:
    """Return workspace details and indexes; messages and file contents are read separately."""
    workspace = self.get_workspace(workspace_id)
    threads = [Thread.model_validate(dict(row)) for row in self.db.execute(
        "SELECT * FROM threads WHERE workspace_id = ? ORDER BY rowid",
        (workspace_id,)
    )]
    artifacts = self.artifacts.list_for_workspace(workspace_id)
    return WorkspaceDetail(**workspace.model_dump(), threads=threads,
                           artifacts=artifacts)

  def update_research(self, workspace_id: str, changes: WorkspaceResearchInfo) -> Workspace:
    """Update supplied fields; serialize read-modify-write across API and tool workers."""
    with self.db:
      self.db.execute("BEGIN IMMEDIATE")
      workspace = self.get_workspace(workspace_id)
      values = workspace.info.model_dump()
      values.update(changes.model_dump(exclude_unset=True))
      self._write_research(workspace_id, WorkspaceResearchInfo(**values))
      self._touch(workspace_id)
    return self.get_workspace(workspace_id)

  def set_preferences(self, workspace_id: str,
      preferences: dict[str, JsonValue]) -> Workspace:
    """Replace preferences; an empty object clears them."""
    return self.update_research(workspace_id, WorkspaceResearchInfo(preferences=preferences))

  def create_thread(self, workspace_id: str, title: str) -> Thread:
    """Create an empty conversation sharing workspace details without copying old messages."""
    self.get_workspace(workspace_id)
    _require_text(title=title)
    thread_id = uuid4().hex
    with self.db:
      self.db.execute(
          "INSERT INTO threads (id, workspace_id, title) VALUES (?, ?, ?)",
          (thread_id, workspace_id, title),
      )
      self._touch(workspace_id)
    return self._thread(workspace_id, thread_id)

  def get_thread(self, workspace_id: str, thread_id: str) -> ThreadDetail:
    """Return a conversation with history messages."""
    thread = self._thread(workspace_id, thread_id)
    # Message IDs preserve insertion order even when timestamps share a millisecond.
    messages = [_message_from_row(row) for row in self.db.execute(
        "SELECT * FROM messages WHERE thread_id = ? ORDER BY id", (thread_id,)
    )]
    return ThreadDetail(**thread.model_dump(), messages=messages)

  def has_user_messages(self, workspace_id: str) -> bool:
    """Check user-message history across all conversations in this workspace."""
    return self.db.execute(
        """SELECT 1 FROM messages JOIN threads ON threads.id = messages.thread_id
           WHERE threads.workspace_id = ? AND messages.role = 'user' LIMIT 1""",
        (workspace_id,),
    ).fetchone() is not None

  def add_message(
      self,
      workspace_id: str,
      thread_id: str,
      role: str,
      content: str,
      *,
      model_message_id: str | None = None,
      tool_calls: list[dict[str, JsonValue]] | None = None,
  ) -> Message:
    """Save message text and model tool-call declarations."""
    self._thread(workspace_id, thread_id)
    calls = [] if tool_calls is None else tool_calls

    # Assistant tool calls form a message even without text.
    if role != "assistant" or not calls:
      _require_text(content=content)

    encoded_calls = json.dumps(calls, ensure_ascii=False, allow_nan=False,)
    with self.db:
      cursor = self.db.execute(
          "INSERT INTO messages "
          "(thread_id, role, content, model_message_id, tool_calls) "
          "VALUES (?, ?, ?, ?, ?)",
          (thread_id, role, content, model_message_id, encoded_calls),
      )
      self._touch(workspace_id)

    return _message_from_row(self.db.execute(
        "SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)
    ).fetchone())

  def add_user_message(
    self,
    workspace_id: str,
    thread_id: str,
    content: str,
) -> tuple[Message, bool]:
    """Save the user's message and return whether is's an initial message """
    self._thread(workspace_id, thread_id)
    _require_text(content=content)
    with self.db:
      # 先取得数据库写入资格
      self.db.execute("BEGIN IMMEDIATE")
      is_first = not self.has_user_messages(workspace_id)
      cursor = self.db.execute(
          "INSERT INTO messages (thread_id, role, content) VALUES (?, 'user', ?)",
          (thread_id, content),
      )
      # Save the question and its run association in one transaction.
      self.db.execute(
          """
          UPDATE research_runs
          SET user_message_id = ?
          WHERE thread_id = ?
            AND status = 'running'
          """,
          (cursor.lastrowid, thread_id),
      )
      self._touch(workspace_id)
      message = _message_from_row(self.db.execute(
        "SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)
      ).fetchone())

      return message, is_first



  def import_file(self, workspace_id: str, source: Path,
      kind: str) -> ArtifactFile:
    """Copy and index a file; separate imports preserve copies with the same name."""
    workspace = self.get_workspace(workspace_id)
    _require_text(kind=kind)
    return self.artifacts.import_file(workspace, source, kind)

  def register_run_file(
      self, workspace_id: str, run_dir: Path, output_path: Path, kind: str,
  ) -> ArtifactFile:
    """Index one existing run output without copying or moving it."""
    return self.register_run_files(
        workspace_id, run_dir, [(output_path, kind)],
    )[0]

  def register_run_files(
      self, workspace_id: str, run_dir: Path,
      outputs: list[tuple[Path, str]],
  ) -> list[ArtifactFile]:
    """Index a batch of run outputs in one database transaction."""
    workspace = self.get_workspace(workspace_id)
    return self.artifacts.register_run_files(
        workspace, run_dir, outputs,
    )

  def archive_run_files(
      self, workspace_id: str, run_dir: Path,
      outputs: list[tuple[Path, str]], *, archive_path: Path,
  ) -> list[ArtifactFile]:
    """Copy selected outputs into a new task directory and index one batch."""
    workspace = self.get_workspace(workspace_id)

    return self.artifacts.archive_run_files(
        workspace, run_dir, outputs, archive_path=archive_path)

  def register_workspace_file(
      self, workspace_id: str, file_path: Path, kind: str,
  ) -> ArtifactFile:
    """Index an existing workspace file in place, reusing its existing record."""
    workspace = self.get_workspace(workspace_id)
    return self.artifacts.register_workspace_file(workspace, file_path, kind)

  def get_file(self, workspace_id: str, artifact_id: str) -> ArtifactFile:
    return self.artifacts.get_file(workspace_id, artifact_id)

  def get_research_run(
      self,
      workspace_id: str,
      thread_id: str,
      run_id: str,
  ) -> ResearchRun:
    """Read a run within its owning workspace and thread."""
    self._thread(workspace_id, thread_id)
    row = self.db.execute(
        """
        SELECT * FROM research_runs
        WHERE id = ? AND thread_id = ?
        """,
        (run_id, thread_id),
    ).fetchone()

    if row is None:
      raise RecordNotFoundError(
          f"research run not found in thread: {run_id}"
      )

    data = dict(row)
    if data["sse_events"] is not None:
      data["sse_events"] = json.loads(data["sse_events"])

    return ResearchRun.model_validate(data)

  def list_research_runs(
      self,
      workspace_id: str,
      thread_id: str,
  ) -> list[ResearchRunSummary]:
    """List this thread's runs in insertion order."""
    self._thread(workspace_id, thread_id)
    rows = self.db.execute(
        """
        SELECT id, thread_id, run_index, user_message_id, status,
               started_at, finished_at, error
        FROM research_runs
        WHERE thread_id = ?
        ORDER BY run_index
        """,
        (thread_id,),
    ).fetchall()

    return [
        ResearchRunSummary.model_validate(dict(row))
        for row in rows
    ]

  def start_thread_execution(
      self, workspace_id: str, thread_id: str,
      *, run_id: str,
  ) -> Thread:
    """Claim execution and return the Thread snapshot from before the claim."""
    if run_id is None:
      raise ValueError("run_id must not be None")
    with self.db:
      # Read the old state under the same write lock as the admission update.
      self.db.execute("BEGIN IMMEDIATE")
      previous = self._thread(workspace_id, thread_id)
      cursor = self.db.execute(
          """
          UPDATE threads
          SET execution_status      = 'running',
              execution_started_at  = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
              execution_finished_at = NULL,
              execution_error       = NULL
          WHERE workspace_id = ?
            AND id = ?
            AND (
              execution_status IS NULL
                  OR execution_status <> 'running'
              )
          """,
          (workspace_id, thread_id),
      )

      if cursor.rowcount == 0:
        raise ThreadBusyError(f"Thread is already running: {thread_id}")

      # Allocate the thread-local index under the admission write lock.
      self.db.execute(
          """
          INSERT INTO research_runs (id, thread_id, run_index)
          SELECT ?, ?, COALESCE(MAX(run_index), -1) + 1
          FROM research_runs
          WHERE thread_id = ?
          """,
          (run_id, thread_id, thread_id),
      )

      return previous

  def finish_thread_execution(
      self,
      workspace_id: str,
      thread_id: str,
      *,
      status: Literal["succeeded", "failed"],
      error: str | None = None,
      sse_events: list[RunProgressEvent] | None = None,
  ) -> Thread:
    self._thread(workspace_id, thread_id)
    # serialize events
    encoded_events = (
        None
        if sse_events is None
        else json.dumps(sse_events, ensure_ascii=False, allow_nan=False)
    )
    with self.db:
      self.db.execute(
          """
          UPDATE threads SET execution_status = ?,
          execution_finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
          execution_error = ?
          WHERE workspace_id = ? AND id = ?
          """,
          (status, error, workspace_id, thread_id),
      )
      self.db.execute(
          """
          UPDATE research_runs
          SET status = ?,
              finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
              error = ?,
              sse_events = ?
          WHERE thread_id = ? AND status = 'running'
          """,
          (status, error, encoded_events, thread_id),
      )

      return self._thread(workspace_id, thread_id)


  def interrupt_running_executions(self) -> int:
    with self.db:
      cursor = self.db.execute(
          """
          UPDATE threads
          SET execution_status = 'interrupted',
              execution_finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
              execution_error = '后端重启，上次执行已中断'
          WHERE execution_status = 'running'
          """
      )
      self.db.execute(
          """
          UPDATE research_runs
          SET status = 'interrupted',
              finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
              error = '后端重启，上次执行已中断'
          WHERE status = 'running'
          """
      )
      return cursor.rowcount

  def complete_thread_execution(
      self,
      workspace_id: str,
      thread_id: str,
      answer: str,
      *,
      model_message_id: str | None = None,
      sse_events: list[RunProgressEvent] | None = None,
  ) -> Message:
    self._thread(workspace_id, thread_id)
    _require_text(content=answer)

    # serialize events
    encoded_events = (
      None
      if sse_events is None
      else json.dumps(sse_events, ensure_ascii=False, allow_nan=False)
    )
    with self.db:
      cursor = self.db.execute(
          """
          INSERT INTO messages (thread_id, role, content, model_message_id)
          VALUES (?, 'assistant', ?, ?);
          """,
          (thread_id, answer, model_message_id)
      )
      message_id = cursor.lastrowid

      self.db.execute(
          """
          UPDATE threads
          SET execution_status = 'succeeded',
              execution_finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
              execution_error = NULL
          WHERE workspace_id = ? AND id = ?;
          """,
          (workspace_id, thread_id),
      )
      self.db.execute(
          """
          UPDATE research_runs
          SET status = 'succeeded',
              finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
              error = NULL,
              sse_events = ?
          WHERE thread_id = ? AND status = 'running'
          """,
          (encoded_events, thread_id),
      )

      self._touch(workspace_id)

      return _message_from_row(self.db.execute(
          "SELECT * FROM messages WHERE id = ?", (message_id,)
      ).fetchone())

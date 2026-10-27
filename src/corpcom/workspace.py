"""Store research and conversations in SQLite, and imported file copies on disk."""

import json
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from pydantic import JsonValue

from corpcom.workspace_models import (
  Artifact, ArtifactFile, Message, Thread, ThreadDetail,
  Workspace, WorkspaceDetail, WorkspaceSummary,
)

# Threads and files belong to a workspace; messages belong through their thread.
# Preferences are JSON text. Artifact rows index files; their contents stay on disk.
SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    scope TEXT NOT NULL,
    observation_period TEXT NOT NULL,
    initial_hypothesis TEXT NOT NULL,
    preferences TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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


class RecordNotFoundError(ValueError):
  """A requested record does not exist within the supplied workspace."""


def _require_text(**fields: str) -> None:
  """Reject blank required input without changing valid text formatting."""
  for name, value in fields.items():
    if not value.strip():
      raise ValueError(f"{name} must not be empty")


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

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc_value, traceback):
    self.db.close()

  def get_workspace(self, workspace_id: str) -> Workspace:
    """Decode a stored row into typed metadata without loading related records."""
    row = self.db.execute(
        "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
    ).fetchone()
    if row is None:
      raise RecordNotFoundError(f"workspace not found: {workspace_id}")
    workspace = dict(row)
    workspace["preferences"] = json.loads(workspace["preferences"])
    return Workspace.model_validate(workspace)

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
      self, title: str, goal: str, scope: str, observation_period: str,
      initial_hypothesis: str, preferences: dict[str, JsonValue],
  ) -> Workspace:
    """Create research with free-text scope and period, without fixed companies or metrics."""
    _require_text(title=title)
    workspace_id = uuid4().hex
    # Commit on success or roll back on error; later processes can read committed rows.
    with self.db:
      self.db.execute(
          """INSERT INTO workspaces
             (id, title, goal, scope, observation_period, initial_hypothesis,
              preferences)
             VALUES (?, ?, ?, ?, ?, ?, ?)""",
          (workspace_id, title, goal, scope, observation_period,
           initial_hypothesis,
           json.dumps(preferences, ensure_ascii=False, allow_nan=False)),
      )
    return self.get_workspace(workspace_id)

  def list_workspaces(self) -> list[WorkspaceSummary]:
    rows = self.db.execute(
        "SELECT id, title, goal, created_at, updated_at FROM workspaces ORDER BY rowid"
    )
    return [WorkspaceSummary.model_validate(dict(row)) for row in rows]

  def get_workspace_detail(self, workspace_id: str) -> WorkspaceDetail:
    """Return workspace details and indexes; messages and file contents are read separately."""
    workspace = self.get_workspace(workspace_id)
    threads = [Thread.model_validate(dict(row)) for row in self.db.execute(
        "SELECT * FROM threads WHERE workspace_id = ? ORDER BY rowid",
        (workspace_id,)
    )]
    artifacts = [Artifact.model_validate(dict(row)) for row in self.db.execute(
        "SELECT * FROM artifacts WHERE workspace_id = ? ORDER BY rowid",
        (workspace_id,)
    )]
    return WorkspaceDetail(**workspace.model_dump(), threads=threads,
                           artifacts=artifacts)

  def set_preferences(self, workspace_id: str,
      preferences: dict[str, JsonValue]) -> Workspace:
    """Replace preferences with explicit user input; do not merge keys. An empty object clears them."""
    self.get_workspace(workspace_id)
    with self.db:
      self.db.execute(
          "UPDATE workspaces SET preferences = ? WHERE id = ?",
          (json.dumps(preferences, ensure_ascii=False, allow_nan=False),
           workspace_id),
      )
      self._touch(workspace_id)
    return self.get_workspace(workspace_id)

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
    messages = [Message.model_validate(dict(row)) for row in self.db.execute(
        "SELECT * FROM messages WHERE thread_id = ? ORDER BY id", (thread_id,)
    )]
    return ThreadDetail(**thread.model_dump(), messages=messages)

  def add_message(self, workspace_id: str, thread_id: str, role: str,
      content: str) -> Message:
    """Save supplied message text without calling a model or generating a reply."""
    self._thread(workspace_id, thread_id)
    _require_text(content=content)
    with self.db:
      cursor = self.db.execute(
          "INSERT INTO messages (thread_id, role, content) VALUES (?, ?, ?)",
          (thread_id, role, content),
      )
      self._touch(workspace_id)
    return Message.model_validate(dict(self.db.execute(
        "SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()))

  def import_file(self, workspace_id: str, source: Path,
      kind: str) -> ArtifactFile:
    """Copy and index a file; separate imports preserve copies with the same name."""
    self.get_workspace(workspace_id)
    _require_text(kind=kind)
    source = source.resolve(strict=True)
    if not source.is_file():
      raise ValueError(f"source must be a file: {source}")
    artifact_id = uuid4().hex
    # Workspace and artifact IDs select the directory; retain the familiar filename.
    directory = self.root / "workspaces" / workspace_id / "files" / artifact_id
    directory.mkdir(parents=True)
    destination = directory / source.name
    try:
      with self.db:
        shutil.copyfile(source, destination)
        self.db.execute(
            """INSERT INTO artifacts (id, workspace_id, name, kind, relative_path)
               VALUES (?, ?, ?, ?, ?)""",
            (artifact_id, workspace_id, source.name, kind,
             # Relative paths keep files locatable when the whole data directory moves.
             destination.relative_to(self.root).as_posix()),
        )
        self._touch(workspace_id)
    except (OSError, sqlite3.Error):
      # fixme File writes are outside SQLite transactions; clean up this failed copy separately.
      # This handles file/database errors, but does not guarantee atomicity on process kill.
      shutil.rmtree(directory)
      raise
    return self.get_file(workspace_id, artifact_id)

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
    self.get_workspace(workspace_id)

    run_dir = run_dir.resolve(strict=True)
    runs_root = self.root / "workspaces" / workspace_id / "runs"
    if run_dir.parent != runs_root:
      raise ValueError("run directory must belong to this workspace")

    artifacts = []
    with self.db:
      for output_path, kind in outputs:
        _require_text(kind=kind)
        if output_path.is_absolute():
          raise ValueError(
              "output path must be relative to the run directory"
          )

        source = (run_dir / output_path).resolve(strict=True)
        if not source.is_relative_to(run_dir):
          raise ValueError(
              "output path must stay within the run directory"
          )
        if not source.is_file():
          raise ValueError(f"output must be a file: {source}")

        artifact_id = uuid4().hex
        self.db.execute(
            """INSERT INTO artifacts
                   (id, workspace_id, name, kind, relative_path)
               VALUES (?, ?, ?, ?, ?)""",
            (
              artifact_id, workspace_id, source.name, kind,
              source.relative_to(self.root).as_posix(),
            ),
        )
        artifacts.append(
            self.get_file(workspace_id, artifact_id)
        )

      if artifacts:
        self._touch(workspace_id)

    return artifacts

  def archive_run_files(
      self, workspace_id: str, run_dir: Path,
      outputs: list[tuple[Path, str]], *, archive_path: Path,
  ) -> list[ArtifactFile]:
    """Copy selected outputs into a new task directory and index one batch."""
    self.get_workspace(workspace_id)
    workspace_root = self.root / "workspaces" / workspace_id
    run_dir = run_dir.resolve(strict=True)
    if run_dir.parent != workspace_root / ".runs":
      raise ValueError("run directory must belong to this workspace")

    if (
        archive_path.is_absolute()
        or not archive_path.parts
        or ".." in archive_path.parts
    ):
      raise ValueError("archive path must be a non-empty relative task path")

    tasks_root = workspace_root / "tasks"
    directory = (tasks_root / archive_path).resolve()
    if directory == tasks_root or not directory.is_relative_to(tasks_root):
      raise ValueError(
          "archive path must stay within the workspace tasks directory"
      )

    selected = []
    seen = set()
    for output_path, kind in outputs:
      _require_text(kind=kind)
      if output_path.is_absolute():
        raise ValueError("output path must be relative to the run directory")

      source = (run_dir / output_path).resolve(strict=True)
      if not source.is_relative_to(run_dir):
        raise ValueError("output path must stay within the run directory")
      if not source.is_file():
        raise ValueError(f"output must be a file: {source}")
      if source in seen:
        raise ValueError(f"duplicate output file: {output_path}")

      seen.add(source)
      selected.append((source, kind))

    if not selected:
      return []

    # Create exclusively, so cleanup can only remove this batch's directory.
    directory.mkdir(parents=True)
    artifacts = []
    try:
      with self.db:
        for source, kind in selected:
          destination = directory / source.relative_to(run_dir)
          destination.parent.mkdir(parents=True, exist_ok=True)
          shutil.copyfile(source, destination)

          artifact_id = uuid4().hex
          self.db.execute(
              """INSERT INTO artifacts
                     (id, workspace_id, name, kind, relative_path)
                 VALUES (?, ?, ?, ?, ?)""",
              (
                artifact_id, workspace_id, destination.name, kind,
                destination.relative_to(self.root).as_posix(),
              ),
          )
          artifacts.append(self.get_file(workspace_id, artifact_id))

        self._touch(workspace_id)
    except (OSError, sqlite3.Error):
      # SQLite rolls back rows; remove this batch's copies and propagate the error.
      shutil.rmtree(directory)
      raise

    return artifacts

  def get_file(self, workspace_id: str, artifact_id: str) -> ArtifactFile:
    """Check ownership and return file metadata and an absolute path, without reading contents."""
    row = self.db.execute(
        "SELECT * FROM artifacts WHERE workspace_id = ? AND id = ?",
        (workspace_id, artifact_id),
    ).fetchone()
    if row is None:
      raise RecordNotFoundError(f"file not found in workspace: {artifact_id}")
    artifact = dict(row)
    path = self.root / artifact["relative_path"]
    # An index row does not guarantee the file still exists after manual deletion.
    if not path.is_file():
      raise FileNotFoundError(f"stored file is missing: {path}")
    return ArtifactFile(**artifact, path=path)

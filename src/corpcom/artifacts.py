import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from corpcom.workspace_models import Artifact, ArtifactFile, Workspace
from corpcom.errors import RecordNotFoundError, ArtifactRegistrationError


class ArtifactStore:
  def __init__(self,
      db: sqlite3.Connection,
      root: Path,
      touch_workspace: Callable[[str], None],
  ):
    self.db = db
    self.root = root
    self._touch_workspace = touch_workspace

  def list_for_workspace(self, workspace_id: str) -> list[Artifact]:
    rows = self.db.execute(
        "SELECT * FROM artifacts WHERE workspace_id = ? ORDER BY rowid",
        (workspace_id,),
    )
    return [Artifact.model_validate(dict(row)) for row in rows]

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

  def import_file(self, workspace: Workspace, source: Path,
      kind: str) -> ArtifactFile:
    source = source.resolve(strict=True)
    if not source.is_file():
      raise ValueError(f"source must be a file: {source}")
    artifact_id = uuid4().hex
    # Workspace and artifact IDs select the directory; retain the familiar filename.
    directory = self.root / "workspaces" / workspace.id / "files" / artifact_id
    directory.mkdir(parents=True)
    destination = directory / source.name
    try:
      with self.db:
        shutil.copyfile(source, destination)
        self.db.execute(
            """INSERT INTO artifacts (id, workspace_id, name, kind, relative_path)
               VALUES (?, ?, ?, ?, ?)""",
            (artifact_id, workspace.id, source.name, kind,
             # Relative paths keep files locatable when the whole data directory moves.
             destination.relative_to(self.root).as_posix()),
        )
        self._touch_workspace(workspace.id)
    except (OSError, sqlite3.Error):
      # fixme File writes are outside SQLite transactions; clean up this failed copy separately.
      # This handles file/database errors, but does not guarantee atomicity on process kill.
      shutil.rmtree(directory)
      raise
    return self.get_file(workspace.id, artifact_id)

  def register_run_files(
      self, workspace: Workspace, run_dir: Path,
      outputs: list[tuple[Path, str]],
  ) -> list[ArtifactFile]:
    """Index a batch of run outputs in one database transaction."""

    run_dir = run_dir.resolve(strict=True)
    runs_root = self.root / "workspaces" / workspace.id / "runs"
    if run_dir.parent != runs_root:
      raise ValueError("run directory must belong to this workspace")

    artifacts = []
    with self.db:
      for output_path, kind in outputs:
        if not kind.strip():
          raise ValueError("kind must not be empty")
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
              artifact_id, workspace.id, source.name, kind,
              source.relative_to(self.root).as_posix(),
            ),
        )
        artifacts.append(
            self.get_file(workspace.id, artifact_id)
        )

      if artifacts:
        self._touch_workspace(workspace.id)

    return artifacts

  def archive_run_files(
      self, workspace: Workspace, run_dir: Path,
      outputs: list[tuple[Path, str]], *, archive_path: Path,
  ) -> list[ArtifactFile]:
    """Copy selected outputs into a new task directory and index one batch."""
    workspace_id = workspace.id
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
      if not kind.strip(): raise ValueError("kind must not be empty")
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

        self._touch_workspace(workspace_id)
    except (OSError, sqlite3.Error):
      # SQLite rolls back rows; remove this batch's copies and propagate the error.
      shutil.rmtree(directory)
      raise

    return artifacts

  def register_workspace_file(
      self, workspace: Workspace, file_path: Path, kind: str,
  ) -> ArtifactFile:
    """Index an existing workspace file in place, reusing its existing record."""
    workspace_id = workspace.id
    if not kind.strip():
      raise ArtifactRegistrationError("kind must not be empty")
    if file_path.is_absolute() or "\0" in str(file_path):
      raise ArtifactRegistrationError("file_path must be relative to /workspace")
    workspace_root = (self.root / "workspaces" / workspace_id).resolve()
    source = (workspace_root / file_path).resolve()
    if not source.is_relative_to(workspace_root):
      raise ArtifactRegistrationError("file_path must stay within the current workspace")
    if not source.is_file():
      raise ArtifactRegistrationError("file_path must refer to an existing regular file")
    relative_path = source.relative_to(self.root).as_posix()
    # Serialize lookup and insert across connections to avoid duplicate registrations.
    with self.db:
      self.db.execute("BEGIN IMMEDIATE")
      row = self.db.execute(
          "SELECT id FROM artifacts WHERE workspace_id = ? AND relative_path = ?",
          (workspace_id, relative_path),
      ).fetchone()
      if row is not None:
        artifact_id = row["id"]
      else:
        artifact_id = uuid4().hex
        self.db.execute(
            """INSERT INTO artifacts (id, workspace_id, name, kind, relative_path)
               VALUES (?, ?, ?, ?, ?)""",
            (artifact_id, workspace_id, source.name, kind, relative_path),
        )
        self._touch_workspace(workspace_id)
    return self.get_file(workspace_id, artifact_id)

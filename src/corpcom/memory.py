"""Store long-term memory topics and their Markdown index in one directory."""

import fcntl
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml

MemoryScope = Literal["user", "workspace"]
MemoryType = Literal["user", "feedback", "project", "reference"]


def read_index(directory: Path) -> str:
  """Read the scope's UTF-8 index, returning empty text if it is missing."""
  try:
    return _read_markdown(directory, "memory.md")
  except FileNotFoundError:
    return ""


def read_topic(
    directory: Path,
    key: str,
) -> str:
  """Read a topic's full UTF-8 Markdown from the selected scope."""
  try:
    return _read_markdown(directory, key)
  except FileNotFoundError as exc:
    raise FileNotFoundError(exc.errno, exc.strerror, key) from exc


def create_topic(
    directory: Path,
    key: str,
    *,
    name: str,
    description: str,
    memory_type: MemoryType,
    body: str,
) -> None:
  """Create or retry a topic and ensure it is indexed in the selected scope."""
  metadata = yaml.safe_dump(
      {"name": name, "description": description, "type": memory_type},
      allow_unicode=True, sort_keys=False,
  )
  markdown = f"---\n{metadata}---\n\n{body}".encode("utf-8")
  entry = f"- [{name}]({key}) — {description}\n".encode("utf-8")

  directory.mkdir(parents=True, exist_ok=True)
  with _locked_directory(directory) as directory_fd:
    temporary = f".{uuid4().hex}.tmp"
    temporary_fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        mode=0o600, dir_fd=directory_fd,
    )
    try:
      with os.fdopen(temporary_fd, "wb") as topic:
        topic.write(markdown)
      # Publish only after the temporary file has been written and closed.
      try:
        os.link(temporary, key, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
      except FileExistsError:
        if _read_memory_file(directory_fd, key) != markdown:
          raise
    finally:
      os.unlink(temporary, dir_fd=directory_fd)

    try:
      index_content = _read_memory_file(directory_fd, "memory.md")
    except FileNotFoundError:
      index_content = b""
    destination = b"(" + key.encode("utf-8") + b")"
    if any(_memory_index_entry_matches(line, destination) for line in index_content.splitlines()):
      return
    if index_content and not index_content.endswith(b"\n"):
      entry = b"\n" + entry
    _replace_memory_file(directory_fd, "memory.md", index_content + entry)


def revise_topic(
    directory: Path,
    key: str,
    *,
    expected_markdown: str,
    body: str,
) -> None:
  """Replace the body if the topic still matches the caller's last read."""
  with _locked_directory(directory) as directory_fd:
    current = _read_memory_file(directory_fd, key).decode("utf-8")
    if current != expected_markdown:
      raise ValueError("memory topic changed; read the latest Markdown and merge your changes")

    header = re.match(
        r"\A---[ \t]*\r?\n.*?^---[ \t]*(?:\r?\n|\Z)(?:[ \t]*\r?\n)*",
        current,
        re.MULTILINE | re.DOTALL,
    )
    if header is None:
      raise ValueError("memory topic is missing its YAML frontmatter")
    prefix = header.group()
    if not prefix.endswith("\n") and body:
      prefix += "\r\n" if "\r\n" in prefix else "\n"

    _replace_memory_file(directory_fd, key, (prefix + body).encode("utf-8"))


def forget_topic(
    directory: Path,
    key: str,
    *,
    expected_markdown: str,
) -> None:
  """Remove a topic and its index entries if the topic still matches the last read."""
  with _locked_directory(directory) as directory_fd:
    current = _read_memory_file(directory_fd, key).decode("utf-8")
    if current != expected_markdown:          # 要求全文精确匹配
      raise ValueError("memory topic changed; read the latest Markdown before forgetting it")

    try:
      index = _read_memory_file(directory_fd, "memory.md")
    except FileNotFoundError:
      index = b""                             # 缺少索引视为空索引
    destination = b"(" + key.encode("utf-8") + b")"
    remaining = b"".join(
        line for line in index.splitlines(keepends=True)
        if not _memory_index_entry_matches(line, destination)
    )
    if remaining != index:
      _replace_memory_file(directory_fd, "memory.md", remaining)
    # Keep the topic available for a retry until the index change is complete.
    os.unlink(key, dir_fd=directory_fd)


@contextmanager
def _locked_directory(directory: Path):
  """Serialize topic and index mutations within a memory scope."""
  directory_fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
  try:
    fcntl.flock(directory_fd, fcntl.LOCK_EX)
    yield directory_fd
  finally:
    os.close(directory_fd)


def _memory_index_entry_matches(line: bytes, destination: bytes) -> bool:
  item = re.match(rb"[ \t]*-[ \t]+\[([^\r\n]*)", line)
  if item is None:
    return False
  label_and_target = item.group(1)
  depth = 1
  for bracket in re.finditer(rb"\\.|[\[\]]", label_and_target):
    if bracket.group() == b"[":
      depth += 1
    elif bracket.group() == b"]":
      depth -= 1
      if depth == 0:
        return label_and_target.startswith(destination, bracket.end())
  return False


def validate_topic_key(key: str, *, writable: bool = False) -> None:
  filename = Path(key)
  if filename.name != key or filename.suffix != ".md" or "\0" in key:
    raise ValueError("memory topic key must be a single .md filename")
  if writable and key.casefold() == "memory.md":
    raise ValueError("memory.md is reserved for the memory index")


def _read_markdown(directory: Path, filename: str) -> str:
  directory_fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
  try:
    return _read_memory_file(directory_fd, filename).decode("utf-8")
  finally:
    os.close(directory_fd)


def _read_memory_file(directory_fd: int, filename: str) -> bytes:
  file_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
  with os.fdopen(file_fd, "rb") as memory_file:
    return memory_file.read()


def _replace_memory_file(directory_fd: int, filename: str, content: bytes) -> None:
  temporary = f".{uuid4().hex}.tmp"
  temporary_fd = os.open(
      temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
      mode=0o600, dir_fd=directory_fd,
  )
  try:
    with os.fdopen(temporary_fd, "wb") as memory_file:
      memory_file.write(content)
    os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
  except BaseException:
    os.unlink(temporary, dir_fd=directory_fd)
    raise

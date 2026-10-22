"""Verify durable storage and ownership without depending on a user interface."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from corpcom.workspace import RecordNotFoundError, WorkspaceStore


class WorkspaceStoreTests(unittest.TestCase):
  def setUp(self):
    # Isolate each test without touching user research.
    temporary = TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.root = Path(temporary.name).resolve()
    self.data = self.root / "data"
    self.store = self.enterContext(WorkspaceStore(self.data))

  def create(self, title="研究", scope="单家公司长期跟踪"):
    return self.store.create_workspace(
        title=title, goal="检验经营韧性", scope=scope,
        observation_period="未来三个月",
        initial_hypothesis="经营现金流能够持续覆盖经营投入",
        preferences={"报告语言": "中文", "关注指标": ["现金流"]},
    )

  def test_reopen_and_continue_in_a_new_thread(self):
    """A separate process restores prior material and creates an independent conversation."""
    workspace = self.create()
    wid = workspace.id
    first = self.store.create_thread(wid, "第一天")
    self.store.add_message(wid, first.id, "user", "重点检查现金流")
    self.store.add_message(wid, first.id, "assistant",
                           "待核对原始报表，这是手工保存的测试消息。")
    self.store.set_preferences(wid,
                               {"报告形式": "简短", "关注指标": ["现金流"]})
    plan = self.root / "研究计划.md"
    plan.write_text("# 研究计划\n核对现金流来源。\n", encoding="utf-8")
    artifact = self.store.import_file(wid, plan, "plan")
    plan.unlink()
    self.store.db.close()

    # The child has no access to the parent connection or its in-memory records.
    completed = subprocess.run(
        [sys.executable, "-c", """
import json
from pathlib import Path
import sys
from corpcom.workspace import WorkspaceStore

with WorkspaceStore(Path(sys.argv[1])) as store:
    wid, tid, aid = sys.argv[2:]
    second = store.create_thread(wid, "第二天")
    file = store.get_file(wid, aid)
    print(json.dumps({
        "workspace": store.get_workspace_detail(wid).model_dump(mode="json"),
        "first": store.get_thread(wid, tid).model_dump(mode="json"),
        "second": store.get_thread(wid, second.id).model_dump(mode="json"),
        "file": file.model_dump(mode="json"),
        "content": file.path.read_text(encoding="utf-8"),
    }, ensure_ascii=False))
""", str(self.data), wid, first.id, artifact.id],
        cwd=self.root, capture_output=True, text=True, timeout=10,
    )
    self.assertEqual(completed.returncode, 0, completed.stderr)
    result = json.loads(completed.stdout)
    restored = result["workspace"]
    for key in ("goal", "scope", "observation_period", "initial_hypothesis"):
      self.assertEqual(restored[key], getattr(workspace, key))
    self.assertEqual(restored["preferences"],
                     {"报告形式": "简短", "关注指标": ["现金流"]})
    self.assertEqual([t["id"] for t in restored["threads"]],
                     [first.id, result["second"]["id"]])
    history = result["first"]["messages"]
    self.assertEqual([m["role"] for m in history], ["user", "assistant"])
    self.assertEqual(history[0]["content"], "重点检查现金流")
    self.assertEqual(result["second"]["messages"], [])
    self.assertEqual(restored["artifacts"][0]["id"], artifact.id)
    self.assertEqual(result["content"], "# 研究计划\n核对现金流来源。\n")
    self.assertTrue(Path(result["file"]["path"]).is_relative_to(
      self.data / "workspaces" / wid))

  def test_workspace_isolation_on_reads_and_writes(self):
    """Pair real thread/file IDs with another workspace and reject reads and writes."""
    first = self.create("公司研究")
    second = self.create("事件研究", "某行业的政策变化")
    thread = self.store.create_thread(first.id, "私有讨论")
    self.store.add_message(first.id, thread.id, "user", "仅属于第一个研究")
    source = self.root / "evidence.txt"
    source.write_text("first workspace", encoding="utf-8")
    artifact = self.store.import_file(first.id, source, "file")
    with self.assertRaisesRegex(RecordNotFoundError, "thread not found"):
      self.store.get_thread(second.id, thread.id)
    with self.assertRaisesRegex(RecordNotFoundError, "thread not found"):
      self.store.add_message(second.id, thread.id, "user", "不得写入")
    with self.assertRaisesRegex(RecordNotFoundError, "file not found"):
      self.store.get_file(second.id, artifact.id)
    self.store.set_preferences(second.id, {"研究风格": "事件驱动"})
    self.assertEqual(self.store.get_workspace(first.id).preferences["报告语言"],
                     "中文")
    other = self.store.get_workspace_detail(second.id)
    self.assertEqual(other.threads, [])
    self.assertEqual(other.artifacts, [])
    self.assertEqual(len(self.store.get_thread(first.id, thread.id).messages),
                     1)

  def test_same_name_imports_keep_distinct_copies(self):
    """A report with the same name must not overwrite previously saved evidence."""
    wid = self.create().id
    source = self.root / "report.txt"
    source.write_text("version one", encoding="utf-8")
    first = self.store.import_file(wid, source, "report")
    source.write_text("version two", encoding="utf-8")
    second = self.store.import_file(wid, source, "report")
    self.assertNotEqual(first.id, second.id)
    self.assertEqual(first.path.read_text(encoding="utf-8"), "version one")
    self.assertEqual(second.path.read_text(encoding="utf-8"), "version two")
    self.assertEqual(len(self.store.get_workspace_detail(wid).artifacts), 2)

  def test_invalid_inputs_do_not_change_stored_state(self):
    """File and storage errors leave records unchanged; JSON shape is checked at the API boundary."""
    wid = self.create().id
    before = self.store.get_workspace_detail(wid)
    with self.assertRaises(ValueError):
      self.store.set_preferences(wid, {"x": float("nan")})
    with self.assertRaises(FileNotFoundError):
      self.store.import_file(wid, self.root / "missing.txt", "file")
    with self.assertRaisesRegex(ValueError, "source must be a file"):
      self.store.import_file(wid, self.root, "file")
    with self.assertRaisesRegex(ValueError, "title must not be empty"):
      self.store.create_thread(wid, "   ")
    self.assertEqual(self.store.get_workspace_detail(wid), before)
    source = self.root / "local.txt"
    source.write_text("evidence", encoding="utf-8")
    with self.assertRaisesRegex(RecordNotFoundError, "workspace not found"):
      self.store.import_file("../../outside", source, "file")
    self.assertFalse((self.data / "workspaces").exists())

  def test_missing_stored_file_is_an_explicit_error(self):
    """Manual deletion must not leave an apparently readable file in the index."""
    wid = self.create().id
    source = self.root / "file.txt"
    source.write_text("data", encoding="utf-8")
    artifact = self.store.import_file(wid, source, "file")
    artifact.path.unlink()
    with self.assertRaisesRegex(FileNotFoundError, "stored file is missing"):
      self.store.get_file(wid, artifact.id)

  def test_register_run_file_keeps_original_file(self):
    """Register a run output in place and expose it through the file index."""
    wid = self.create().id
    run_dir = self.data / "workspaces" / wid / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    source = run_dir / "financials.json"
    content = '{"revenue": 123}'
    source.write_text(content, encoding="utf-8")
    before = set(self.data.rglob("*"))

    artifact = self.store.register_run_file(
        wid, run_dir, Path("financials.json"), "data",
    )

    self.assertEqual(artifact.path, source)
    self.assertEqual(set(self.data.rglob("*")), before)
    self.assertEqual(
        artifact.relative_path,
        source.relative_to(self.data).as_posix(),
    )
    stored = self.store.get_file(wid, artifact.id)
    self.assertEqual(stored.path.read_text(encoding="utf-8"), content)
    detail = self.store.get_workspace_detail(wid)
    self.assertEqual([item.id for item in detail.artifacts], [artifact.id])

    self.store.db.close()

    with WorkspaceStore(self.data) as reopened:
      restored = reopened.get_file(wid, artifact.id)
      self.assertEqual(restored.path, source)
      self.assertEqual(
          restored.path.read_text(encoding="utf-8"), content,
      )
      detail = reopened.get_workspace_detail(wid)
      self.assertEqual(
          [item.id for item in detail.artifacts], [artifact.id],
      )

  def test_register_run_file_rejects_unsafe_output_paths(self):
    """Reject absolute paths and escapes without changing stored metadata."""
    wid = self.create().id
    run_dir = self.data / "workspaces" / wid / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    source = run_dir / "output.txt"
    source.write_text("output", encoding="utf-8")

    outside = run_dir.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (run_dir / "escape.txt").symlink_to(outside)

    before = self.store.get_workspace_detail(wid)
    cases = [
      (source, "output path must be relative"),
      (Path("../outside.txt"), "output path must stay within"),
      (Path("escape.txt"), "output path must stay within"),
    ]
    for output_path, message in cases:
      with self.subTest(output_path=output_path):
        with self.assertRaisesRegex(ValueError, message):
          self.store.register_run_file(
              wid, run_dir, output_path, "data",
          )
        self.assertEqual(
            self.store.get_workspace_detail(wid), before,
        )

  def test_register_run_file_rejects_another_workspace_run(self):
    """Reject another workspace's run without changing either workspace."""
    first = self.create("公司 A")
    second = self.create("公司 B")
    run_dir = self.data / "workspaces" / first.id / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    source = run_dir / "output.txt"
    source.write_text("first workspace", encoding="utf-8")

    before_first = self.store.get_workspace_detail(first.id)
    before_second = self.store.get_workspace_detail(second.id)

    with self.assertRaisesRegex(
        ValueError, "run directory must belong to this workspace",
    ):
      self.store.register_run_file(
          second.id, run_dir, Path("output.txt"), "data",
      )

    self.assertEqual(
        self.store.get_workspace_detail(first.id), before_first,
    )
    self.assertEqual(
        self.store.get_workspace_detail(second.id), before_second,
    )
    self.assertEqual(
        source.read_text(encoding="utf-8"), "first workspace",
    )

  def test_register_run_files_rolls_back_the_whole_batch(self):
    """Roll back earlier registrations when a later output is invalid."""
    wid = self.create().id
    run_dir = self.data / "workspaces" / wid / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    first = run_dir / "first.txt"
    first.write_text("first output", encoding="utf-8")
    outside = run_dir.parent / "outside.txt"
    outside.write_text("outside output", encoding="utf-8")
    before = self.store.get_workspace_detail(wid)

    with self.assertRaisesRegex(
        ValueError, "output path must stay within",
    ):
      self.store.register_run_files(
          wid, run_dir,
          [
            (Path("first.txt"), "data"),
            (Path("../outside.txt"), "data"),
          ],
      )

    self.assertEqual(self.store.get_workspace_detail(wid), before)
    with WorkspaceStore(self.data) as reopened:
      self.assertEqual(reopened.get_workspace_detail(wid), before)

    self.assertEqual(first.read_text(encoding="utf-8"), "first output")
    self.assertEqual(outside.read_text(encoding="utf-8"), "outside output")


if __name__ == "__main__":
  unittest.main()

"""Verify durable storage and ownership without depending on a user interface."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
from threading import Barrier
import unittest
import sqlite3
from uuid import uuid4

import yaml

from corpcom.workspace import RecordNotFoundError, ThreadBusyError, WorkspaceStore
from corpcom.workspace_models import WorkspaceResearchInfo


EMPTY_NOTE_HEADINGS = [
    "# 工作笔记",
    "## 当前进展与下一步",
    "## 会话与关键发现",
    "## 资料与产物",
]


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

  def test_new_workspace_has_empty_working_note(self):
    for title in ("研究 A", "研究 B"):
      workspace = self.create(title)
      path = self.data / "workspaces" / workspace.id / "agent.md"
      lines = path.read_text(encoding="utf-8").splitlines()
      self.assertEqual([line for line in lines if line.strip()], EMPTY_NOTE_HEADINGS)

  def test_first_working_note_read_creates_missing_file(self):
    workspace = self.create()
    path = self.data / "workspaces" / workspace.id / "agent.md"
    path.unlink(missing_ok=True)  # Model a workspace created before working notes.
    with WorkspaceStore(self.data) as reopened:
      reopened.get_workspace_detail(workspace.id)
      self.assertFalse(path.exists())
      note = reopened.read_working_note(workspace.id)
    self.assertEqual(path.read_text(encoding="utf-8"), note)
    self.assertEqual([line for line in note.splitlines() if line.strip()], EMPTY_NOTE_HEADINGS)

  def test_reopening_workspace_preserves_handwritten_working_note(self):
    workspace = self.create()
    path = self.data / "workspaces" / workspace.id / "agent.md"
    handwritten = "# 我的笔记\r\n\r\n下一步核对季报。  ".encode("utf-8")
    path.write_bytes(handwritten)
    with WorkspaceStore(self.data) as reopened:
      reopened.get_workspace_detail(workspace.id)
      note = reopened.read_working_note(workspace.id)
    self.assertEqual(path.read_bytes(), handwritten)
    self.assertEqual(note.splitlines(), handwritten.decode("utf-8").splitlines())

  def test_working_note_rejects_symlink(self):
    workspace = self.create()
    path = self.data / "workspaces" / workspace.id / "agent.md"
    outside = self.root / "outside.md"
    outside.write_text("私人内容", encoding="utf-8")
    path.unlink()
    path.symlink_to(outside)

    with self.assertRaises(OSError):
      self.store.read_working_note(workspace.id)
    self.assertEqual(outside.read_text(encoding="utf-8"), "私人内容")

  def test_missing_memory_index_is_empty_for_each_scope(self):
    wid = self.create().id
    user_dir = self.root / "custom-memory"
    for scope in ("user", "workspace"):
      with self.subTest(scope=scope):
        self.assertEqual(self.store.read_memory_index(wid, scope, user_dir), "")

  def test_memory_index_reads_shared_and_workspace_markdown_from_disk(self):
    a = self.create("公司 A").id
    b = self.create("公司 B").id
    user_dir = self.root / "custom-memory"
    shared = "- [报告单位](units.md) — 金额注明单位。\n"
    project_a = "- [回款](cashflow.md) — 按季核对回款。\n"
    project_b = "- [库存](inventory.md) — 跟踪周转天数。\n"
    for directory, content in (
        (user_dir, shared),
        (self.data / "workspaces" / a / "memory", project_a),
        (self.data / "workspaces" / b / "memory", project_b),
    ):
      directory.mkdir(parents=True, exist_ok=True)
      (directory / "memory.md").write_text(content, encoding="utf-8")

    for wid, expected in ((a, project_a), (b, project_b)):
      with self.subTest(workspace=wid):
        self.assertEqual(self.store.read_memory_index(wid, "user", user_dir), shared)
        self.assertEqual(self.store.read_memory_index(wid, "workspace", user_dir), expected)

    updated = "- [报告单位](units.md) — 金额与币种均须标注。\n"
    (user_dir / "memory.md").write_text(updated, encoding="utf-8")
    self.assertEqual(self.store.read_memory_index(b, "user", user_dir), updated)

  def test_memory_topic_reads_latest_full_markdown_from_selected_scope(self):
    wid = self.create().id
    user_dir = self.root / "custom-memory"
    project_dir = self.data / "workspaces" / wid / "memory"
    key = "report-style.md"
    header = "---\nname: 报告格式\ndescription: 金额口径\ntype: feedback\n---\n"
    shared = header + "所有报告注明币种。\n\n依据：用户明确要求。\n"
    project = header + "本项目另列原币金额。\n\n依据：项目约定。\n"
    for directory, body in ((user_dir, shared), (project_dir, project)):
      directory.mkdir(parents=True, exist_ok=True)
      (directory / "memory.md").write_text(f"- [报告格式]({key}) — 金额口径\n", encoding="utf-8")
      (directory / key).write_text(body, encoding="utf-8")

    self.assertEqual(self.store.read_memory_topic(wid, "user", user_dir, key), shared)
    self.assertEqual(self.store.read_memory_topic(wid, "workspace", user_dir, key), project)
    revised = project.replace("另列原币金额", "同时列出原币和折算金额")
    (project_dir / key).write_text(revised, encoding="utf-8")
    self.assertEqual(self.store.read_memory_topic(wid, "workspace", user_dir, key), revised)

  def test_missing_memory_topic_reports_its_filename(self):
    wid = self.create().id
    user_dir = self.root / "custom-memory"
    user_dir.mkdir()
    (user_dir / "memory.md").write_text("- [报告单位](units.md) — 单位约定\n", encoding="utf-8")
    with self.assertRaisesRegex(FileNotFoundError, r"units\.md"):
      self.store.read_memory_topic(wid, "user", user_dir, "units.md")

  def test_memory_topic_requires_a_markdown_filename_in_the_scope_directory(self):
    wid = self.create().id
    user_dir = self.root / "custom-memory"
    for key in ("notes.txt", "reports/summary.md", str(self.root / "report.md")):
      with self.subTest(key=key), self.assertRaises(ValueError):
        self.store.read_memory_topic(wid, "user", user_dir, key)

  def test_create_memory_topic_writes_markdown_and_indexes_it_in_selected_scope(self):
    wid = self.create().id
    user_dir = self.root / "custom" / "memory"
    project_dir = self.data / "workspaces" / wid / "memory"
    project_dir.mkdir(parents=True, exist_ok=True)
    existing = "# 项目索引\n\n- [库存](stock.md) — 跟踪周转天数"
    (project_dir / "memory.md").write_text(existing, encoding="utf-8")

    for scope, directory, name, description, kind, body in (
        ("user", user_dir, "报告格式", "金额注明币种", "feedback", "适用于所有报告。\n\n依据：用户明确要求。\n"),
        ("workspace", project_dir, "回款核查", "按季核对回款", "project", "仅适用于本项目。\n\n依据：一季报第 8 页。\n"),
    ):
      with self.subTest(scope=scope):
        self.store.create_memory_topic(
            wid, scope, user_dir, "research.md", name=name, description=description,
            memory_type=kind, body=body,
        )
        markdown = (directory / "research.md").read_text(encoding="utf-8")
        opening, metadata, saved_body = markdown.split("---\n", 2)
        self.assertEqual(opening, "")
        self.assertEqual(yaml.safe_load(metadata), {
            "name": name, "description": description, "type": kind,
        })
        self.assertEqual(saved_body.lstrip("\n"), body)
        index = (directory / "memory.md").read_text(encoding="utf-8")
        entry = f"- [{name}](research.md) — {description}"
        self.assertEqual(index.splitlines().count(entry), 1)
        if scope == "workspace":
          self.assertTrue(index.startswith(existing + "\n"))

  def test_create_memory_topic_rejects_reserved_index_filename(self):
    wid = self.create().id
    user_dir = self.root / "custom-memory"
    user_dir.mkdir()
    index = user_dir / "memory.md"
    index.write_text("# 手写索引\n", encoding="utf-8")
    with self.assertRaises(ValueError):
      self.store.create_memory_topic(
          wid, "user", user_dir, "memory.md", name="报告格式", description="金额注明币种",
          memory_type="feedback", body="依据：用户明确要求。\n",
      )
    self.assertEqual(index.read_text(encoding="utf-8"), "# 手写索引\n")

  def _create_memory_topic_with_file_limit(self, wid, directory, key, fields):
    # Limit only the child writer. The retry uses the same public API without the limit.
    writer = dedent("""\
        import errno, json, resource, signal, sys
        from pathlib import Path
        from corpcom.workspace import WorkspaceStore

        data, wid, directory, key, fields = sys.argv[1:]
        with WorkspaceStore(Path(data)) as store:
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            limits = resource.getrlimit(resource.RLIMIT_FSIZE)
            resource.setrlimit(resource.RLIMIT_FSIZE, (128, limits[1]))
            try:
                store.create_memory_topic(wid, "user", Path(directory), key, **json.loads(fields))
            except OSError as exc:
                if exc.errno != errno.EFBIG:
                    raise
            else:
                raise AssertionError("Expected a write to exceed 128 bytes")
            finally:
                resource.setrlimit(resource.RLIMIT_FSIZE, limits)
        """)
    subprocess.run([sys.executable, "-c", writer, str(self.data), wid, str(directory), key,
                    json.dumps(fields)], check=True)

  def test_retry_create_memory_topic_after_body_write_failure(self):
    wid = self.create().id
    directory = self.root / "custom-memory"
    key = "cashflow.md"
    fields = dict(name="回款", description="到账依据", memory_type="reference",
                  body="一季报第八页记录了到账日期。\n" * 100)
    self._create_memory_topic_with_file_limit(wid, directory, key, fields)
    self.assertFalse((directory / key).exists(), "失败的写入留下了不完整的正式主题")
    self.assertEqual(self.store.read_memory_index(wid, "user", directory), "")
    self.store.create_memory_topic(wid, "user", directory, key, **fields)
    self.assertTrue(self.store.read_memory_topic(wid, "user", directory, key).endswith(fields["body"]))
    self.assertEqual(self.store.read_memory_index(wid, "user", directory),
                     "- [回款](cashflow.md) — 到账依据\n")

  def test_retry_memory_topic_after_partial_index_write(self):
    wid = self.create().id
    directory = self.root / "custom-memory"
    directory.mkdir()
    key = "cashflow.md"
    fields = dict(name="回款", description="到账依据", memory_type="reference", body="见季报。\n")
    link = "- [回款](cashflow.md)".encode("utf-8")
    # Leave room for the link but not its description.
    original = "# 手写索引".encode("utf-8").ljust(128 - len(link) - 2, b" ") + b"\r\n"
    index = directory / "memory.md"
    index.write_bytes(original)
    self._create_memory_topic_with_file_limit(wid, directory, key, fields)
    topic = self.store.read_memory_topic(wid, "user", directory, key)
    self.assertTrue(topic.endswith(fields["body"]))
    with self.subTest(stage="after failure"):
      self.assertEqual(index.read_bytes(), original)
    self.store.create_memory_topic(wid, "user", directory, key, **fields)
    with self.subTest(stage="after retry"):
      self.assertEqual(index.read_bytes(), original + link + " — 到账依据\n".encode("utf-8"))
      self.assertEqual(self.store.read_memory_topic(wid, "user", directory, key), topic)

  def test_two_workspaces_concurrently_add_topics_to_the_shared_user_index(self):
    directory = self.root / "custom-memory"
    directory.mkdir()
    original = "# 手写索引\r\n\r\n- [原有约定](existing.md) — 用户备注\r\n"
    index = directory / "memory.md"
    index.write_bytes(original.encode())
    topics = [
        (self.create("公司 A").id, "units.md", "报告单位", "注明币种"),
        (self.create("公司 B").id, "sources.md", "引用来源", "注明页码"),
    ]
    ready = Barrier(2)

    def save(topic):
      wid, key, name, description = topic
      with WorkspaceStore(self.data) as store:
        ready.wait(timeout=5)
        store.create_memory_topic(wid, "user", directory, key, name=name,
                                  description=description, memory_type="feedback", body=description)

    with ThreadPoolExecutor(max_workers=2) as writers:
      list(writers.map(save, topics))

    saved = index.read_bytes()
    self.assertTrue(saved.startswith(original.encode()))
    self.assertCountEqual(saved[len(original.encode()):].decode().splitlines(),
                          [f"- [{name}]({key}) — {desc}" for _, key, name, desc in topics])
    for wid, key, _, description in topics:
      self.assertTrue(self.store.read_memory_topic(wid, "user", directory, key).endswith(description))

  def test_retry_memory_topic_repairs_missing_entry_only_once(self):
    wid = self.create().id
    directory = self.root / "custom-memory"
    args = (wid, "user", directory, "units.md")
    fields = dict(name="金额单位", description="金额标注币种", memory_type="feedback",
                  body="依据：用户要求注明币种。\n")
    self.store.create_memory_topic(*args, **fields)
    topic = directory / "units.md"
    original_topic = topic.read_bytes()
    index = directory / "memory.md"
    # Reconstruct the on-disk state after the topic write but before index registration.
    previous = "# 索引\n\n- [库存](stock.md) — 单位见[币种约定](units.md)\n"
    index.write_text(previous, encoding="utf-8")

    with WorkspaceStore(self.data) as reopened:
      reopened.create_memory_topic(*args, **fields)
      self.assertEqual(index.read_text(encoding="utf-8"),
                       previous + "- [金额单位](units.md) — 金额标注币种\n")
      # A user may relabel the link. The filename still identifies the registered topic.
      edited_index = previous + "- [我的币种约定 [2025]](units.md) — 手工整理\n"
      index.write_text(edited_index, encoding="utf-8")
      reopened.create_memory_topic(*args, **fields)
    self.assertEqual(index.read_text(encoding="utf-8"), edited_index)
    self.assertEqual(topic.read_bytes(), original_topic)

  def test_retry_memory_topic_preserves_a_conflicting_edit(self):
    wid = self.create().id
    directory = self.root / "custom-memory"
    args = (wid, "user", directory, "units.md")
    fields = dict(name="金额单位", description="金额标注币种", memory_type="feedback",
                  body="金额需注明币种。\n")
    self.store.create_memory_topic(*args, **fields)
    topic = directory / "units.md"
    revised = topic.read_bytes() + "用户补充：同时注明汇率日期。\n".encode("utf-8")
    topic.write_bytes(revised)
    index = directory / "memory.md"
    index.write_text("# 待整理\n", encoding="utf-8")

    with self.assertRaises(FileExistsError):
      self.store.create_memory_topic(*args, **fields)
    self.assertEqual(topic.read_bytes(), revised)
    self.assertEqual(index.read_text(encoding="utf-8"), "# 待整理\n")

  def test_revise_memory_topic_keeps_frontmatter_and_handwritten_index(self):
    wid = self.create().id
    args = (wid, "workspace", self.root / "user-memory", "cashflow.md")
    directory = self.data / "workspaces" / wid / "memory"
    directory.mkdir()
    header = "---\n# 手工整理\nname: 回款核查\ndescription: 跟踪回款\ntype: project\n---\n\n"
    topic = directory / "cashflow.md"
    topic.write_text(header + "年报显示回款改善，原因待核实。\n", encoding="utf-8")
    index = directory / "memory.md"
    entry = "- [回款核查 [2025]](cashflow.md) — 我的跟踪项\n"
    index.write_text(entry, encoding="utf-8")
    original = self.store.read_memory_topic(*args)
    revised_body = "回款提前主要源于结算时点。\n\n2025-04-30 修订：一季报第 8 页披露了到账日期。\n"

    self.store.revise_memory_topic(*args, expected_markdown=original, body=revised_body)

    self.assertEqual(self.store.read_memory_topic(*args), header + revised_body)
    self.assertEqual(index.read_bytes(), entry.encode("utf-8"))

  def test_revise_memory_topic_rejects_stale_read_after_editor_save(self):
    wid = self.create().id
    directory = self.root / "user-memory"
    args = (wid, "user", directory, "units.md")
    self.store.create_memory_topic(*args, name="报告单位", description="披露币种",
                                  memory_type="feedback", body="金额标注币种。\n")
    original = self.store.read_memory_topic(*args)
    topic = directory / "units.md"
    handwritten = (original + "用户补充：标注换算日期。\n").encode("utf-8")
    topic.write_bytes(handwritten)
    index = directory / "memory.md"
    original_index = index.read_bytes()

    with self.assertRaises(ValueError):
      self.store.revise_memory_topic(*args, expected_markdown=original, body="改用美元。\n")
    self.assertEqual(topic.read_bytes(), handwritten)
    self.assertEqual(index.read_bytes(), original_index)

  def test_forget_memory_topic_removes_only_its_file_and_index_entry(self):
    wid = self.create().id
    user_dir = self.root / "user-memory"
    key = "cashflow.md"
    project_args = (wid, "workspace", user_dir, key)
    user_args = (wid, "user", user_dir, key)
    self.store.create_memory_topic(*project_args, name="回款", description="季度核查",
                                  memory_type="project", body="回款时点待核实。\n")
    self.store.create_memory_topic(*user_args, name="回款资料", description="公共数据源",
                                  memory_type="reference", body="参考公开年报。\n")
    project_dir = self.data / "workspaces" / wid / "memory"
    index = project_dir / "memory.md"
    before = "# 项目索引\r\n\r\n"
    after = "- [库存](stock.md) — 另见[回款](cashflow.md)"
    index.write_bytes((before + "- [回款 [2025]](cashflow.md) — 季度核查\r\n" + after).encode())
    original = self.store.read_memory_topic(*project_args)
    shared = (user_dir / key).read_bytes()
    shared_index = (user_dir / "memory.md").read_bytes()

    self.store.forget_memory_topic(*project_args, expected_markdown=original)

    self.assertFalse((project_dir / key).exists())
    self.assertEqual(index.read_bytes(), (before + after).encode())
    self.assertEqual((user_dir / key).read_bytes(), shared)
    self.assertEqual((user_dir / "memory.md").read_bytes(), shared_index)

  def test_forget_memory_topic_rejects_stale_read_before_removing_index_entry(self):
    wid = self.create().id
    directory = self.root / "user-memory"
    args = (wid, "user", directory, "units.md")
    self.store.create_memory_topic(*args, name="报告单位", description="披露币种",
                                  memory_type="feedback", body="金额标注币种。\n")
    original = self.store.read_memory_topic(*args)
    edited = original + "用户补充：同时注明汇率。\n"
    topic = directory / "units.md"
    topic.write_bytes(edited.encode())
    index = directory / "memory.md"
    original_index = index.read_bytes()

    with self.assertRaises(ValueError):
      self.store.forget_memory_topic(*args, expected_markdown=original)
    self.assertEqual(topic.read_bytes(), edited.encode())
    self.assertEqual(index.read_bytes(), original_index)

  def test_forget_memory_topic_resumes_after_index_entry_was_removed(self):
    wid = self.create().id
    directory = self.root / "user-memory"
    args = (wid, "user", directory, "units.md")
    self.store.create_memory_topic(*args, name="报告单位", description="披露币种",
                                  memory_type="feedback", body="金额标注币种。\n")
    original = self.store.read_memory_topic(*args)
    # Disk state immediately after replacing the index, before unlinking the topic.
    index = directory / "memory.md"
    other_entry = b"- [Period](period.md)\r\n"
    index.write_bytes(other_entry)
    with WorkspaceStore(self.data) as reopened:
      reopened.forget_memory_topic(*args, expected_markdown=original)
    self.assertFalse((directory / "units.md").exists())
    self.assertEqual(index.read_bytes(), other_entry)

  def test_concurrent_user_messages_have_one_first_message_per_workspace(self):
    workspace = self.create()
    threads = [self.store.create_thread(workspace.id, str(i)) for i in range(2)]
    # Only prior user messages disqualify a new message from being the first.
    self.store.add_message(workspace.id, threads[0].id, "assistant", "已有说明")
    ready = Barrier(2)

    def save(thread):
      with WorkspaceStore(self.data) as store:
        ready.wait(timeout=5)
        return store.add_user_message(workspace.id, thread.id, thread.title)

    with ThreadPoolExecutor(max_workers=2) as executor:
      results = list(executor.map(save, threads))

    self.assertEqual(sorted(first for _, first in results), [False, True])
    first_message = next(message for message, first in results if first)
    self.assertEqual(first_message.id, min(message.id for message, _ in results))
    with WorkspaceStore(self.data) as reopened:
      for thread, (message, _) in zip(threads, results):
        saved = reopened.get_thread(workspace.id, thread.id)
        self.assertEqual([m for m in saved.messages if m.role == "user"], [message])
        self.assertEqual(message.content, thread.title)
      later = reopened.create_thread(workspace.id, "后续会话")
      self.assertFalse(reopened.add_user_message(workspace.id, later.id, "继续")[1])

      other = reopened.create_workspace("独立研究")
      other_thread = reopened.create_thread(other.id, "首轮")
      self.assertTrue(reopened.add_user_message(other.id, other_thread.id, "新问题")[1])

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
      self.assertEqual(restored["info"][key], getattr(workspace.info, key))
    self.assertEqual(restored["info"]["preferences"],
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
    self.assertEqual(self.store.get_workspace(first.id).info.preferences["报告语言"],
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
    self.assertEqual([p.name for p in (self.data / "workspaces").iterdir()], [wid])

  def test_research_file_is_the_source_for_details_and_listing(self):
    workspace = self.create()
    path = self.data / "workspaces" / workspace.id / "research.json"
    content = json.loads(path.read_text(encoding="utf-8"))
    self.assertEqual(set(content), set(WorkspaceResearchInfo.model_fields))
    self.assertEqual(content["goal"], workspace.info.goal)
    updated = self.store.update_research(workspace.id, WorkspaceResearchInfo(
        goal="核对现金流", scope="", observation_period="", preferences={},
    ))
    self.assertEqual(updated.info.initial_hypothesis, workspace.info.initial_hypothesis)
    self.assertEqual(updated.info.scope, "")
    self.assertEqual(updated.info.observation_period, "")
    self.assertEqual(updated.info.preferences, {})
    with WorkspaceStore(self.data) as reopened:
      self.assertEqual(reopened.get_workspace(workspace.id), updated)
      self.assertEqual(reopened.list_workspaces()[0].goal, "核对现金流")
    content = json.loads(path.read_text(encoding="utf-8"))
    content["goal"] = "文件中的目标"
    path.write_text(json.dumps(content), encoding="utf-8")
    self.assertEqual(self.store.get_workspace(workspace.id).info.goal, "文件中的目标")
    self.assertEqual(self.store.list_workspaces()[0].goal, "文件中的目标")
    path.unlink()
    with self.assertRaises(FileNotFoundError):
      self.store.get_workspace(workspace.id)

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

  def test_start_execution_returns_prior_state_and_serializes_competing_claims(self):
    workspace = self.create()
    thread = self.store.create_thread(workspace.id, "竞争恢复资格")
    self.assertEqual(
        self.store.start_thread_execution(workspace.id, thread.id, run_id=uuid4().hex),
        thread)
    self.store.interrupt_running_executions()
    previous = self.store.get_workspace_detail(workspace.id).threads[0]
    ready = Barrier(2)

    def claim():
      with WorkspaceStore(self.data) as store:
        ready.wait(timeout=5)
        try:
          return store.start_thread_execution(workspace.id, thread.id, run_id=uuid4().hex)
        except ThreadBusyError:
          return "busy"

    with ThreadPoolExecutor(max_workers=2) as executor:
      results = list(executor.map(lambda _: claim(), range(2)))
    self.assertEqual(results.count("busy"), 1)
    self.assertEqual([result for result in results if result != "busy"], [previous])
    current = self.store.get_thread(workspace.id, thread.id)
    self.assertEqual(current.execution_status, "running")
    self.assertIsNone(current.execution_finished_at)
    self.assertIsNone(current.execution_error)

  def test_complete_execution_rolls_back_answer_when_status_update_fails(self):
    workspace = self.create()
    thread = self.store.create_thread(workspace.id, "事务回滚测试")

    self.store.start_thread_execution(workspace.id, thread.id, run_id=uuid4().hex)
    self.store.add_message(
        workspace.id, thread.id, "user", "测试问题",
    )

    before_thread = self.store.get_thread(workspace.id, thread.id)
    before_workspace = self.store.get_workspace(workspace.id)

    # 允许回答 INSERT 执行，再让后面的状态 UPDATE 失败。
    with self.store.db:
        self.store.db.execute("""
            CREATE TRIGGER reject_execution_success
            BEFORE UPDATE OF execution_status ON threads
            WHEN NEW.execution_status = 'succeeded'
            BEGIN
                SELECT RAISE(ABORT, 'injected status update failure');
            END
        """)

    with self.assertRaisesRegex(
        sqlite3.IntegrityError,
        "injected status update failure",
    ):
        self.store.complete_thread_execution(
            workspace.id, thread.id, "这条回答不应保存",
        )

    # 整体回滚：没有新增回答，Thread 和 Workspace 均保持原状。
    self.assertEqual(
        self.store.get_thread(workspace.id, thread.id),
        before_thread,
    )
    self.assertEqual(
        self.store.get_workspace(workspace.id),
        before_workspace,
    )

    # 使用新连接确认数据库中也没有留下部分结果。
    with WorkspaceStore(self.data) as reopened:
        self.assertEqual(
            reopened.get_thread(workspace.id, thread.id),
            before_thread,
        )
        self.assertEqual(
            reopened.get_workspace(workspace.id),
            before_workspace,
        )

  def test_start_execution_rejects_none_run_id_without_changes(self):
    workspace = self.create()
    thread = self.store.create_thread(workspace.id, "无效运行 ID")

    with self.assertRaisesRegex(ValueError, "run_id must not be None"):
      self.store.start_thread_execution(
          workspace.id, thread.id, run_id=None,
      )

    self.assertEqual(
        self.store.get_thread(workspace.id, thread.id).execution_status,
        None,
    )
    self.assertFalse(self.store.db.in_transaction)
    self.assertEqual(
        self.store.db.execute(
            "SELECT COUNT(*) FROM research_runs"
        ).fetchone()[0],
        0,
    )

  def test_each_run_keeps_its_own_user_message(self):
    workspace = self.create()
    thread = self.store.create_thread(workspace.id, "连续研究")

    self.store.start_thread_execution(
        workspace.id, thread.id, run_id="first",
    )
    first, _ = self.store.add_user_message(
        workspace.id, thread.id, "第一轮问题",
    )
    self.store.complete_thread_execution(
        workspace.id, thread.id, "第一轮回答",
    )

    self.store.start_thread_execution(
        workspace.id, thread.id, run_id="second",
    )
    second, _ = self.store.add_user_message(
        workspace.id, thread.id, "第二轮问题",
    )

    # Verify committed associations using a new connection.
    with WorkspaceStore(self.data) as reopened:
      rows = reopened.db.execute(
          "SELECT id, user_message_id, status FROM research_runs"
      ).fetchall()
      self.assertEqual(
          {row["id"]: (row["user_message_id"], row["status"])
           for row in rows},
          {
              "first": (first.id, "succeeded"),
              "second": (second.id, "running"),
          },
      )

  def test_run_indexes_are_per_thread_and_rejected_claims_use_no_index(self):
    workspace = self.create()
    first = self.store.create_thread(workspace.id, "第一条 Thread")
    second = self.store.create_thread(workspace.id, "第二条 Thread")

    self.store.start_thread_execution(
        workspace.id, first.id, run_id="first-0",
    )
    with self.assertRaises(ThreadBusyError):
      self.store.start_thread_execution(
          workspace.id, first.id, run_id="rejected",
      )

    self.store.finish_thread_execution(
        workspace.id, first.id,
        status="failed", error="测试失败",
    )
    self.store.start_thread_execution(
        workspace.id, first.id, run_id="first-1",
    )
    self.store.start_thread_execution(
        workspace.id, second.id, run_id="second-0",
    )

    with WorkspaceStore(self.data) as reopened:
      first_runs = reopened.list_research_runs(workspace.id, first.id)
      second_runs = reopened.list_research_runs(workspace.id, second.id)

      self.assertEqual(
          [(run.id, run.run_index, run.status) for run in first_runs],
          [
              ("first-0", 0, "failed"),
              ("first-1", 1, "running"),
          ],
      )
      self.assertEqual(
          [(run.id, run.run_index) for run in second_runs],
          [("second-0", 0)],
      )

if __name__ == "__main__":
  unittest.main()

"""Verify persistence and ownership through separate processes, not shared in-memory state."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class WorkspaceCliTests(unittest.TestCase):
    def setUp(self):
        # Isolate each test in a temporary directory without touching user research.
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "data"

    def command(self, *args, success=True):
        """Start a real CLI process for each call and check its exit status and JSON output."""
        completed = subprocess.run(
            [sys.executable, "-m", "corpcom.workspace_cli", "--data-dir", str(self.data), *args],
            cwd=self.root, capture_output=True, text=True, timeout=10,
        )
        if success:
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        return completed.stderr

    def create(self, title="研究", scope="单家公司长期跟踪"):
        return self.command(
            "create", "--title", title, "--goal", "检验经营韧性",
            "--scope", scope, "--observation-period", "未来三个月",
            "--hypothesis", "经营现金流能够持续覆盖经营投入",
            "--preferences", '{"报告语言":"中文","关注指标":["现金流"]}',
        )

    def test_reopen_and_continue_in_a_new_thread(self):
        """Simulate returning to research: restore prior material while keeping new messages separate."""
        workspace = self.create()
        wid = workspace["id"]
        first = self.command("thread-create", wid, "--title", "第一天")
        self.command("message-add", wid, first["id"], "--content", "重点检查现金流")
        self.command(
            "message-add", wid, first["id"], "--role", "assistant",
            "--content", "待核对原始报表，这是手工保存的测试消息。",
        )
        self.command("set-preferences", wid, "--preferences", '{"报告形式":"简短","关注指标":["现金流"]}')
        plan = self.root / "研究计划.md"
        plan.write_text("# 研究计划\n核对现金流来源。\n", encoding="utf-8")
        artifact = self.command("file-add", wid, str(plan), "--kind", "plan")
        # Removing the source proves the stored artifact is a copy, not a path reference.
        plan.unlink()

        # Each command is a new process; the second thread cannot rely on earlier process state.
        second = self.command("thread-create", wid, "--title", "第二天")
        restored = self.command("show", wid)
        for key in ("goal", "scope", "observation_period", "initial_hypothesis"):
            self.assertEqual(restored[key], workspace[key])
        self.assertEqual(restored["preferences"], {"报告形式": "简短", "关注指标": ["现金流"]})
        self.assertEqual([t["id"] for t in restored["threads"]], [first["id"], second["id"]])
        history = self.command("thread-show", wid, first["id"])["messages"]
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])
        self.assertEqual(history[0]["content"], "重点检查现金流")
        self.assertEqual(self.command("thread-show", wid, second["id"])["messages"], [])
        self.assertEqual(restored["artifacts"][0]["id"], artifact["id"])
        file = self.command("file-show", wid, artifact["id"])
        self.assertEqual(Path(file["path"]).read_text(), "# 研究计划\n核对现金流来源。\n")
        self.assertTrue(Path(file["path"]).is_relative_to(self.data / "workspaces" / wid))

    def test_workspace_isolation_on_reads_and_writes(self):
        """Pair real thread/file IDs with another workspace and verify reads and writes fail."""
        first = self.create("公司研究")
        second = self.create("事件研究", "某行业的政策变化")
        thread = self.command("thread-create", first["id"], "--title", "私有讨论")
        self.command("message-add", first["id"], thread["id"], "--content", "仅属于第一个研究")
        source = self.root / "evidence.txt"
        source.write_text("first workspace")
        artifact = self.command("file-add", first["id"], str(source))
        self.assertIn("thread not found", self.command("thread-show", second["id"], thread["id"], success=False))
        self.assertIn("thread not found", self.command(
            "message-add", second["id"], thread["id"], "--content", "不得写入", success=False,
        ))
        self.assertIn("file not found", self.command("file-show", second["id"], artifact["id"], success=False))
        self.command("set-preferences", second["id"], "--preferences", '{"研究风格":"事件驱动"}')
        self.assertEqual(self.command("show", first["id"])["preferences"]["报告语言"], "中文")
        other = self.command("show", second["id"])
        self.assertEqual(other["threads"], [])
        self.assertEqual(other["artifacts"], [])
        self.assertEqual(len(self.command("thread-show", first["id"], thread["id"])["messages"]), 1)

    def test_same_name_imports_keep_distinct_copies(self):
        """A new report with the same name must not overwrite previously saved evidence."""
        wid = self.create()["id"]
        source = self.root / "report.txt"
        source.write_text("version one")
        first = self.command("file-add", wid, str(source), "--kind", "report")
        source.write_text("version two")
        second = self.command("file-add", wid, str(source), "--kind", "report")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(Path(first["path"]).read_text(), "version one")
        self.assertEqual(Path(second["path"]).read_text(), "version two")
        self.assertEqual(len(self.command("show", wid)["artifacts"]), 2)

    def test_invalid_inputs_do_not_change_stored_state(self):
        """Invalid input must fail without adding records or creating directories outside its workspace."""
        wid = self.create()["id"]
        before = self.command("show", wid)
        self.command("set-preferences", wid, "--preferences", '[]', success=False)
        self.command("set-preferences", wid, "--preferences", '{"x":NaN}', success=False)
        self.command("file-add", wid, str(self.root / "missing.txt"), success=False)
        self.command("file-add", wid, str(self.root), success=False)
        self.command("thread-create", wid, "--title", "   ", success=False)
        self.assertEqual(self.command("show", wid), before)
        source = self.root / "local.txt"
        source.write_text("evidence")
        self.command("file-add", "../../outside", str(source), success=False)
        self.assertFalse((self.data / "workspaces").exists())

    def test_missing_stored_file_is_an_explicit_error(self):
        """Simulate manual deletion and ensure the index does not hide the missing file."""
        wid = self.create()["id"]
        source = self.root / "file.txt"
        source.write_text("data")
        artifact = self.command("file-add", wid, str(source))
        Path(artifact["path"]).unlink()
        self.assertIn("stored file is missing", self.command("file-show", wid, artifact["id"], success=False))


if __name__ == "__main__":
    unittest.main()

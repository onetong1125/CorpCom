"""Verify sandbox function discovery without executing data functions."""

from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory

from corpcom.workspace import WorkspaceStore
from corpcom.function_catalog import FUNCTIONS, list_function_summaries
from corpcom.workspace_tools import create_workspace_tools


def sample_function(symbol: str) -> dict:
  """Return sample data.

  The symbol identifies the requested company.
  """
  raise AssertionError("Reading documentation must not execute the function")


class WorkspaceToolTests(unittest.TestCase):
  def test_read_function_doc_returns_registered_contract(self):
    with patch.dict(FUNCTIONS, {"sample.lookup": sample_function}, clear=True):
      tools = {
        tool.name: tool
        for tool in create_workspace_tools(Path("unused"), "unused")
      }

      result = tools["read_function_doc"].invoke({
        "function_id": "sample.lookup",
      })

    self.assertEqual(result, {
      "function_id": "sample.lookup",
      "import": f"from {__name__} import sample_function",
      "signature": "sample_function(symbol: str) -> dict",
      "documentation": (
        "Return sample data.\n\n"
        "The symbol identifies the requested company."
      ),
    })

  def test_read_function_doc_returns_error_for_unknown_id(self):
    with patch.dict(FUNCTIONS, {"sample.lookup": sample_function}, clear=True):
      tools = {
        tool.name: tool
        for tool in create_workspace_tools(Path("unused"), "unused")
      }

      result = tools["read_function_doc"].invoke({
        "function_id": "sample.missing",
      })

    self.assertEqual(result, {
      "status": "error",
      "error": "Unknown sandbox function: sample.missing",
      "available_functions": ["sample.lookup"],
    })

  def test_function_summaries_include_only_opening_paragraph(self):
    with patch.dict(FUNCTIONS, {"sample.lookup": sample_function}, clear=True):
      result = list_function_summaries()

    self.assertEqual(result, [{
      "function_id": "sample.lookup",
      "summary": "Return sample data.",
    }])

  def test_execute_python_archives_outputs_and_returns_readable_ids(self):
    """Read selected archived copies after deleting the temporary run."""
    temporary = self.enterContext(TemporaryDirectory())
    data = Path(temporary).resolve() / "data"
    with WorkspaceStore(data) as store:
      wid = store.create_workspace(
          title="研究", goal="验证产物登记", scope="测试",
          observation_period="", initial_hypothesis="产物可以跨连接读取",
          preferences={},
      ).id

    run_dir = data / "workspaces" / wid / ".runs" / "test-run"
    run_dir.mkdir(parents=True)
    source = run_dir / "financials.json"
    content = '{"revenue": 123}'
    source.write_text(content, encoding="utf-8")
    (run_dir / "debug.txt").write_text("debug", encoding="utf-8")

    tools = {
      tool.name: tool
      for tool in create_workspace_tools(data, wid)
    }
    execution = {
      "status": "ok",
      "run_dir": str(run_dir),
      "result": {
        "summary": {"rows": 1},
        "outputs": [{"path": "financials.json", "kind": "data"}],
      },
    }
    with patch(
        "corpcom.workspace_tools.run_python", return_value=execution,
    ) as run_python:
      result = tools["execute_python"].invoke({
        "code": "# test",
        "archive_path": "financials/v1",
      })
      run_python.assert_called_once_with(
          "# test", runs_root=data / "workspaces" / wid / ".runs",
      )

    self.assertEqual(result["status"], "ok")
    self.assertEqual(result["result"], {"rows": 1})
    self.assertEqual(len(result["artifacts"]), 1)
    reference = result["artifacts"][0]
    self.assertEqual(reference["name"], "financials.json")
    self.assertEqual(reference["kind"], "data")

    archive_dir = data / "workspaces" / wid / "tasks" / "financials" / "v1"
    self.assertEqual(
        reference["relative_path"],
        (archive_dir / "financials.json").relative_to(data).as_posix(),
    )
    self.assertEqual(
        {path.name for path in archive_dir.iterdir()}, {"financials.json"},
    )
    source.write_text("changed", encoding="utf-8")
    self.assertEqual(
        (archive_dir / "financials.json").read_text(encoding="utf-8"), content,
    )
    shutil.rmtree(run_dir)

    read = tools["read_file"].invoke({
      "artifact_id": reference["artifact_id"],
    })
    self.assertEqual(read, {"name": "financials.json", "content": content})

    with WorkspaceStore(data) as store:
      saved = store.get_file(wid, reference["artifact_id"])
      self.assertEqual(saved.path, archive_dir / "financials.json")
      self.assertEqual(
          [item.id for item in store.get_workspace_detail(wid).artifacts],
          [reference["artifact_id"]],
      )


  def test_execute_python_rolls_back_invalid_output_batch(self):
    """Propagate registration failure and leave no partial artifact index."""
    temporary = self.enterContext(TemporaryDirectory())
    data = Path(temporary).resolve() / "data"
    with WorkspaceStore(data) as store:
      wid = store.create_workspace(
          title="研究", goal="验证批量回滚", scope="测试",
          observation_period="", initial_hypothesis="失败不会留下部分索引",
          preferences={},
      ).id
      before = store.get_workspace_detail(wid)

    run_dir = data / "workspaces" / wid / ".runs" / "test-run"
    run_dir.mkdir(parents=True)
    (run_dir / "first.txt").write_text("first", encoding="utf-8")
    (run_dir.parent / "outside.txt").write_text("outside", encoding="utf-8")

    tools = {
      tool.name: tool
      for tool in create_workspace_tools(data, wid)
    }
    execution = {
      "status": "ok",
      "run_dir": str(run_dir),
      "result": {
        "summary": {},
        "outputs": [
          {"path": "first.txt", "kind": "data"},
          {"path": "../outside.txt", "kind": "data"},
        ],
      },
    }
    with patch(
        "corpcom.workspace_tools.run_python", return_value=execution,
    ):
      with self.assertRaisesRegex(
          ValueError, "output path must stay within",
      ):
        tools["execute_python"].invoke({
          "code": "# test",
          "archive_path": "financials/v1",
        })

    with WorkspaceStore(data) as store:
      self.assertEqual(store.get_workspace_detail(wid), before)
    self.assertFalse(
        (data / "workspaces" / wid / "tasks" / "financials" / "v1").exists()
    )


if __name__ == "__main__":
  unittest.main()
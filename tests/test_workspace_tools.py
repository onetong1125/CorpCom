"""Verify sandbox function discovery without executing data functions."""

from pathlib import Path
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
  unittest.main()
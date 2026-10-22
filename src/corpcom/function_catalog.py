"""Catalog sandbox functions exposed to the research agent."""

import inspect

from corpcom.fundamentals import get_annual_financials
from corpcom.market import get_price_history

FUNCTIONS = {
  "fundamentals.get_annual_financials": get_annual_financials,
  "market.get_price_history": get_price_history,
}

def list_function_summaries() -> list[dict[str, str]]:
  """Return registered function IDs and their opening docstring paragraphs."""
  return [
    {
      "function_id": function_id,
      "summary": inspect.getdoc(function).split("\n\n", 1)[0],
    }
    for function_id, function in FUNCTIONS.items()
  ]
import json
from pathlib import Path
import yaml

from corpcom.function_catalog import list_function_summaries
from jinja2 import Environment, PackageLoader

def build_workspace_prompt(
    context: dict,
    *,
    initialize_research: bool,
) -> str:
  """Build a workspace prompt."""
  config_path = Path(__file__).parent / "template" / "prompts.yaml"
  config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
  env = Environment(loader=PackageLoader("corpcom", "template"))
  return env.get_template("system.md.j2").render(
      initialize_research=initialize_research,
      function_summaries_json=json.dumps(list_function_summaries(), ensure_ascii=False),
      workspace_context_json=json.dumps(context, ensure_ascii=False),
      agent_role=config["agent_role"],
  )

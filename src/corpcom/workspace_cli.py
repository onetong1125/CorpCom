"""Parse input, call local storage, and print JSON without invoking a model or sandbox."""

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from corpcom.workspace import WorkspaceStore


def parse_preferences(value: str) -> dict:
    """Validate CLI preferences as a JSON object suitable for persistence."""
    try:
        preferences = json.loads(value)
        if not isinstance(preferences, dict):
            raise ValueError("preferences must be a JSON object")
        # Python's parser accepts NaN/Infinity; reject these nonstandard JSON numbers.
        json.dumps(preferences, allow_nan=False)
        return preferences
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage persistent research workspaces")
    # The default is relative to cwd; pass the original data path when running elsewhere.
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="data directory (default: ./data)")
    commands = parser.add_subparsers(dest="command", required=True)

    # Workspace commands create, list, inspect, or explicitly replace preferences.
    create = commands.add_parser("create", help="create a research workspace")
    create.add_argument("--title", required=True)
    create.add_argument("--goal", required=True)
    create.add_argument("--scope", default="")
    create.add_argument("--observation-period", default="")
    create.add_argument("--hypothesis", required=True)
    create.add_argument("--preferences", type=parse_preferences, default={})

    commands.add_parser("list", help="list workspaces")

    show = commands.add_parser("show", help="show goals, preferences, threads and files")
    show.add_argument("workspace_id")

    preferences = commands.add_parser("set-preferences", help="replace the full preferences object")
    preferences.add_argument("workspace_id")
    preferences.add_argument("--preferences", type=parse_preferences, required=True)

    # Conversation commands carry a workspace ID so storage can check ownership.
    thread_create = commands.add_parser("thread-create", help="create a conversation in a workspace")
    thread_create.add_argument("workspace_id")
    thread_create.add_argument("--title", required=True)

    thread_show = commands.add_parser("thread-show", help="show a conversation and its messages")
    thread_show.add_argument("workspace_id")
    thread_show.add_argument("thread_id")

    message = commands.add_parser("message-add", help="save a message; does not call the model")
    message.add_argument("workspace_id")
    message.add_argument("thread_id")
    message.add_argument("--role", choices=("user", "assistant"), default="user")
    message.add_argument("--content", required=True)

    ask = commands.add_parser("ask", help="ask the model in a saved conversation")
    ask.add_argument("workspace_id")
    ask.add_argument("thread_id")
    ask.add_argument("--question", required=True)

    # Imports create independent copies; file-show returns a path for opening in an editor.
    file_add = commands.add_parser("file-add", help="copy a local file into a workspace")
    file_add.add_argument("workspace_id")
    file_add.add_argument("source", type=Path)
    file_add.add_argument("--kind", default="file", help="label such as plan, data, or report")

    file_show = commands.add_parser("file-show", help="show metadata and the saved file's absolute path")
    file_show.add_argument("workspace_id")
    file_show.add_argument("artifact_id")

    args = parser.parse_args()

    try:
        # Keep persistence and ownership checks in WorkspaceStore rather than dispatch code.
        with WorkspaceStore(args.data_dir) as store:
            match args.command:
                case "create":
                    workspace = store.create_workspace(
                        args.title, args.goal, args.scope, args.observation_period,
                        args.hypothesis, args.preferences,
                    )
                    result = store.get_workspace_detail(workspace.id)
                case "list":
                    result = store.list_workspaces()
                case "show":
                    result = store.get_workspace_detail(args.workspace_id)
                case "set-preferences":
                    result = store.set_preferences(args.workspace_id, args.preferences)
                case "thread-create":
                    result = store.create_thread(args.workspace_id, args.title)
                case "thread-show":
                    result = store.get_thread(args.workspace_id, args.thread_id)
                case "message-add":
                    result = store.add_message(args.workspace_id, args.thread_id, args.role, args.content)
                case "ask":
                    from corpcom.workspace_agent import ask_workspace

                    load_dotenv()
                    ask_workspace(
                        store, OpenAI(), os.environ["CORPCOM_MODEL"],
                        args.workspace_id, args.thread_id, args.question,
                    )
                    result = store.get_thread(args.workspace_id, args.thread_id).messages[-1]
                case "file-add":
                    result = store.import_file(args.workspace_id, args.source, args.kind)
                case "file-show":
                    result = store.get_file(args.workspace_id, args.artifact_id)
    except (ValueError, OSError) as exc:
        # Report input/file errors on stderr and exit nonzero, without a success payload.
        parser.exit(1, f"error: {exc}\n")

    # Convert typed records only at the output boundary, including datetime and Path fields.
    payload = (
        [item.model_dump(mode="json") for item in result]
        if isinstance(result, list) else result.model_dump(mode="json")
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

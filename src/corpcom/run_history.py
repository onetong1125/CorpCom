"""Select and project checkpoint messages for research history."""

from collections.abc import Sequence
from pathlib import Path

from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, ToolMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.message import add_messages
from redis import Redis

from corpcom.run_events import RunEventStream
from corpcom.workspace import WorkspaceStore


def read_thread_messages(
    checkpointer: BaseCheckpointSaver,
    workspace_id: str,
    thread_id: str,
) -> list[BaseMessage]:
    """Read thread messages, including pending message writes."""
    saved = checkpointer.get_tuple({
        "configurable": {
            "thread_id": f"{workspace_id}:{thread_id}",
        }
    })
    if saved is None:
        return []
    messages = saved.checkpoint["channel_values"].get("messages", [])
    for _, channel, value in saved.pending_writes or []:
        if channel == "messages":
            messages = add_messages(messages, value)
    return list(messages)

def read_thread_history(
    root: Path,
    checkpointer: BaseCheckpointSaver,
    workspace_id: str,
    thread_id: str,
) -> dict[str, object]:
    """Group checkpoint history by saved questions without requiring runs."""
    with WorkspaceStore(root) as store:
        thread = store.get_thread(workspace_id, thread_id)

    messages = read_thread_messages(checkpointer, workspace_id, thread_id)

    return {
        "thread_id": thread_id,
        "turns": [
            {
                "user_message_id": question.id,
                "events": project_run_messages(select_run_messages(
                    messages, question.id
                ))
            }
            for question in thread.messages
            if question.role == "user"
        ]
    }


def select_run_messages(
    messages: Sequence[BaseMessage],
    user_message_id: int | None,
) -> list[BaseMessage]:
    """Select responses after this run's question, before the next question."""
    if user_message_id is None:
        return []

    target_id = f"business:{user_message_id}"
    collecting = False
    selected = []

    for message in messages:
        if isinstance(message, HumanMessage):
            if collecting:
                break
            collecting = message.id == target_id
            continue

        if collecting:
            selected.append(message)

    return selected

def read_run_messages(
    checkpointer: BaseCheckpointSaver,
    workspace_id: str,
    thread_id: str,
    user_message_id: int | None,
) -> list[BaseMessage]:
    """Read this run's checkpoint messages, including pending message writes."""
    if user_message_id is None:
        return []

    messages = read_thread_messages(checkpointer, workspace_id, thread_id)

    return select_run_messages(messages, user_message_id)

def project_run_messages(
    messages: Sequence[BaseMessage],
) -> list[dict[str, object]]:
    """Project complete messages"""
    events = []

    for message in messages:
        if isinstance(message, AIMessage):
            events.append({
                "type": "assistant_message",
                "message_id": message.id,
                "text": message.text,
                "tool_calls": message.tool_calls,
            })
        elif isinstance(message, ToolMessage):
            events.append({
                "type": "tool_result",
                "message_id": message.id,
                "tool_call_id": message.tool_call_id,
                "content": message.content,
                "status": message.status,
            })

    return events

def project_archived_events(
    events: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Collect archived text fragments and preserve tool events."""
    projected = []
    assistant_messages = {}

    for event in events:
        if event["type"] == "text_delta":
            message_id = event["message_id"]

            if message_id not in assistant_messages:
                message = {
                    "type": "assistant_message",
                    "message_id": message_id,
                    "text": "",
                    "tool_calls": [],
                }
                assistant_messages[message_id] = message
                projected.append(message)

            assistant_messages[message_id]["text"] += event["text"]

        elif event["type"] in ("tool_start", "tool_end", "tool_error"):
            projected.append(dict(event))

    return projected

def merge_run_history(
    checkpoint_events: Sequence[dict[str, object]],
    archived_events: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Prefer checkpoint messages and append uncheckpointed events."""
    merged = list(checkpoint_events)

    assistant_ids = {
        event["message_id"]
        for event in checkpoint_events
        if event["type"] == "assistant_message"
    }
    declared_call_ids = {
        call["id"]
        for event in checkpoint_events
        if event["type"] == "assistant_message"
        for call in event["tool_calls"]
    }
    completed_call_ids = {
        event["tool_call_id"]
        for event in checkpoint_events
        if event["type"] == "tool_result"
    }
    tool_call_ids = {
        event["tool_run_id"]:event["tool_call_id"]
        for event in archived_events
        if event["type"] == "tool_start"
    }

    for event in archived_events:
        if event["type"] == "assistant_message":
            if event["message_id"] not in assistant_ids:
                merged.append(dict(event))

        elif event["type"] == "tool_start":
            if event["tool_call_id"] not in declared_call_ids:
                merged.append(dict(event))

        elif event["type"] in ("tool_end", "tool_error"):
            tool_call_id = tool_call_ids[event["tool_run_id"]]
            if tool_call_id not in completed_call_ids:
                merged.append({
                    **event,
                    "tool_call_id": tool_call_id,
                })

    return merged

def read_run_history(
    root: Path,
    checkpointer: BaseCheckpointSaver,
    redis_client: Redis,
    workspace_id: str,
    thread_id: str,
    run_id: str,
) -> dict[str, object]:
    """Read checkpoint history and supplement it with recorded events."""
    with WorkspaceStore(root) as store:
        run = store.get_research_run(
            workspace_id, thread_id, run_id
        )

    recorded_events = run.sse_events
    if recorded_events is None:
        recorded_events = []
        stream = RunEventStream(redis_client, run_id)
        cursor = "0-0"
        while True:
            batch = stream.read_after(cursor)
            if not batch:
                break
            recorded_events.extend(event for _, event in batch)
            cursor = batch[-1][0]

    messages = read_run_messages(
        checkpointer, workspace_id, thread_id, run.user_message_id
    )

    events = merge_run_history(
        project_run_messages(messages),
        project_archived_events(recorded_events),
    )

    return {
        "run": run.model_dump(
            mode="json",
            exclude={"sse_events"},
        ),
        "events": events,
    }

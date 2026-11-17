"""Verify checkpoint history selection and reconstruction."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver, empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver
from redis import Redis

from corpcom.run_history import (
    project_run_messages,
    read_thread_messages,
    select_run_messages, project_archived_events,
    merge_run_history, read_run_history, read_thread_history
)
from corpcom.workspace import RecordNotFoundError
from corpcom.workspace import WorkspaceStore


class RunHistoryTests(unittest.TestCase):
  def test_selects_one_question_with_its_tool_history(self):
    plan = AIMessage(
        id="plan",
        content="",
        tool_calls=[{
          "id": "call-1",
          "name": "lookup",
          "args": {"query": "现金流"},
        }],
    )
    result = ToolMessage(
        id="result",
        tool_call_id="call-1",
        content="查询失败",
        status="error",
    )
    answer = AIMessage(id="answer", content="暂时无法确认。")

    messages = [
      HumanMessage(id="business:10", content="上一轮问题"),
      AIMessage(id="previous-answer", content="上一轮回答"),
      HumanMessage(id="business:20", content="本轮问题"),
      plan,
      result,
      answer,
      HumanMessage(id="business:30", content="下一轮问题"),
      AIMessage(id="next-answer", content="下一轮回答"),
    ]

    selected = select_run_messages(messages, user_message_id=20)

    self.assertEqual(selected, [plan, result, answer])

  def test_returns_empty_when_question_is_missing(self):
    messages = [
      HumanMessage(id="business:10", content="其他问题"),
      AIMessage(id="other-answer", content="其他问题的回答"),
    ]

    for user_message_id in (None, 20):
      with self.subTest(user_message_id=user_message_id):
        selected = select_run_messages(
            messages,
            user_message_id=user_message_id,
        )

        self.assertEqual(selected, [])

  def test_keeps_unfinished_last_turn(self):
    plan = AIMessage(
        id="unfinished-plan",
        content="开始查询。",
        tool_calls=[{
          "id": "pending-call",
          "name": "lookup",
          "args": {},
        }],
    )
    messages = [
      HumanMessage(id="business:20", content="查询现金流"),
      plan,
    ]

    selected = select_run_messages(messages, user_message_id=20)

    self.assertEqual(selected, [plan])

  def test_reads_pending_tool_result_after_reopening_checkpoint(self):
    question = HumanMessage(id="business:20", content="查询现金流")
    plan = AIMessage(
        id="plan",
        content="",
        tool_calls=[{
          "id": "call-1",
          "name": "lookup",
          "args": {},
        }],
    )
    result = ToolMessage(
        id="result",
        tool_call_id="call-1",
        content="查询失败",
        status="error",
    )

    with TemporaryDirectory() as directory:
      path = str(Path(directory) / "checkpoints.sqlite")

      with SqliteSaver.from_conn_string(path) as saver:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"]["messages"] = [question, plan]
        config = saver.put(
            {"configurable": {
              "thread_id": "w1:t1",
              "checkpoint_ns": "",
            }},
            checkpoint,
            {"source": "input", "step": 0, "parents": {}},
            {},
        )
        saver.put_writes(
            config,
            [("messages", [result])],
            "tool-task",
        )

      with SqliteSaver.from_conn_string(path) as saver:
        before = saver.get_tuple(config)
        self.assertEqual(
            before.checkpoint["channel_values"]["messages"],
            [question, plan],
        )

        messages = read_thread_messages(saver, "w1", "t1")

        self.assertEqual(messages, [question, plan, result])
        self.assertEqual(saver.get_tuple(config), before)

  def test_projects_tool_declaration_and_structured_error(self):
    plan = AIMessage(
        id="plan",
        content="",
        tool_calls=[{
          "id": "call-1",
          "name": "lookup",
          "args": {"query": "现金流"},
        }],
    )
    content = [{"type": "text", "text": "数据源不可用"}]
    result = ToolMessage(
        id="result",
        tool_call_id="call-1",
        content=content,
        status="error",
    )

    events = project_run_messages([plan, result])

    self.assertEqual(events, [
      {
        "type": "assistant_message",
        "message_id": "plan",
        "text": "",
        "tool_calls": plan.tool_calls,
      },
      {
        "type": "tool_result",
        "message_id": "result",
        "tool_call_id": "call-1",
        "content": content,
        "status": "error",
      },
    ])

  def test_projects_archived_text_around_tool_events(self):
    tool_start = {
      "type": "tool_start",
      "tool_run_id": "tool-run-1",
      "tool_call_id": "call-1",
      "name": "lookup",
    }
    tool_end = {
      "type": "tool_end",
      "tool_run_id": "tool-run-1",
      "tool_call_id": "call-1",
      "content": "查询结果",
      "status": "success",
    }
    events = [
      {"type": "text_delta", "message_id": "plan", "text": "先查询"},
      {"type": "text_delta", "message_id": "plan", "text": "现金流。"},
      tool_start,
      tool_end,
      {"type": "text_delta", "message_id": "answer", "text": "查询"},
      {"type": "text_delta", "message_id": "answer", "text": "完成。"},
    ]

    projected = project_archived_events(events)

    self.assertEqual(projected, [
      {
        "type": "assistant_message",
        "message_id": "plan",
        "text": "先查询现金流。",
        "tool_calls": [],
      },
      tool_start,
      tool_end,
      {
        "type": "assistant_message",
        "message_id": "answer",
        "text": "查询完成。",
        "tool_calls": [],
      },
    ])

  def test_merge_prefers_checkpoint_and_keeps_partial_answer(self):
    complete = {
      "type": "assistant_message",
      "message_id": "plan",
      "text": "完整操作计划。",
      "tool_calls": [],
    }
    archived_plan = {
      "type": "assistant_message",
      "message_id": "plan",
      "text": "完整操作",
      "tool_calls": [],
    }
    partial_answer = {
      "type": "assistant_message",
      "message_id": "answer",
      "text": "目前发现",
      "tool_calls": [],
    }

    merged = merge_run_history(
        [complete],
        [archived_plan, partial_answer],
    )

    self.assertEqual(merged, [complete, partial_answer])

  def test_merge_keeps_missing_parallel_tool_error(self):
    checkpoint_events = project_run_messages([
      AIMessage(
          id="plan",
          content="",
          tool_calls=[
            {"id": "call-a", "name": "lookup", "args": {"query": "A"}},
            {"id": "call-b", "name": "lookup", "args": {"query": "B"}},
          ],
      ),
      ToolMessage(
          id="result-a",
          tool_call_id="call-a",
          content="A 的结果",
      ),
    ])
    archived_error = {
      "type": "tool_error",
      "tool_run_id": "run-b",
      "error": "B 查询失败",
    }
    archived_events = [
      {
        "type": "tool_start",
        "tool_run_id": "run-a",
        "tool_call_id": "call-a",
        "name": "lookup",
      },
      {
        "type": "tool_start",
        "tool_run_id": "run-b",
        "tool_call_id": "call-b",
        "name": "lookup",
      },
      {
        "type": "tool_end",
        "tool_run_id": "run-a",
        "tool_call_id": "call-a",
        "content": "A 的结果",
        "status": "success",
      },
      archived_error,
    ]

    merged = merge_run_history(checkpoint_events, archived_events)

    self.assertEqual(merged, [
      *checkpoint_events,
      {**archived_error, "tool_call_id": "call-b"},
    ])

  def test_empty_archive_does_not_read_redis(self):
    redis = Mock(spec=Redis)

    with TemporaryDirectory() as directory:
      root = Path(directory)

      with WorkspaceStore(root) as store:
        workspace = store.create_workspace("测试研究")
        thread = store.create_thread(workspace.id, "测试会话")
        store.start_thread_execution(
            workspace.id, thread.id, run_id="run-1",
        )
        store.add_user_message(workspace.id, thread.id, "查询现金流")
        store.finish_thread_execution(
            workspace.id,
            thread.id,
            status="failed",
            error="模型连接失败",
            sse_events=[],
        )

      with SqliteSaver.from_conn_string(
          str(root / "checkpoints.sqlite")
      ) as saver:
        history = read_run_history(
            root, saver, redis,
            workspace.id, thread.id, "run-1",
        )

      self.assertEqual(history["events"], [])
      self.assertEqual(history["run"]["status"], "failed")
      self.assertEqual(history["run"]["error"], "模型连接失败")
      redis.xread.assert_not_called()

  def test_unarchived_run_reads_all_redis_batches(self):
    redis = Mock(spec=Redis)
    key = "corpcom:runs:run-1:events"
    entries = [
      (
        f"{index}-0",
        {"event": json.dumps({
          "type": "text_delta",
          "message_id": "partial",
          "text": "x",
        })},
      )
      for index in range(1, 104)
    ]
    redis.xread.side_effect = [
      [(key, entries[:100])],
      [(key, entries[100:])],
      [],
    ]

    with TemporaryDirectory() as directory:
      root = Path(directory)

      with WorkspaceStore(root) as store:
        workspace = store.create_workspace("测试研究")
        thread = store.create_thread(workspace.id, "测试会话")
        store.start_thread_execution(
            workspace.id, thread.id, run_id="run-1",
        )
        store.add_user_message(workspace.id, thread.id, "查询现金流")
        store.interrupt_running_executions()

      with SqliteSaver.from_conn_string(
          str(root / "checkpoints.sqlite")
      ) as saver:
        history = read_run_history(
            root, saver, redis,
            workspace.id, thread.id, "run-1",
        )

      self.assertEqual(history["run"]["status"], "interrupted")
      self.assertEqual(history["events"], [{
        "type": "assistant_message",
        "message_id": "partial",
        "text": "x" * 103,
        "tool_calls": [],
      }])
      self.assertEqual(redis.xread.call_args_list, [
        call({key: "0-0"}, count=100, block=None),
        call({key: "100-0"}, count=100, block=None),
        call({key: "103-0"}, count=100, block=None),
      ])

  def test_rejects_wrong_run_ownership_before_reading_history(self):
    saver = Mock(spec=BaseCheckpointSaver)
    redis = Mock(spec=Redis)

    with TemporaryDirectory() as directory:
      root = Path(directory)

      with WorkspaceStore(root) as store:
        workspace = store.create_workspace("测试研究")
        other_workspace = store.create_workspace("其他研究")
        thread = store.create_thread(workspace.id, "测试会话")
        other_thread = store.create_thread(workspace.id, "其他会话")
        store.start_thread_execution(
            workspace.id, thread.id, run_id="run-1",
        )

      cases = [
        (other_workspace.id, thread.id, "run-1"),
        (workspace.id, other_thread.id, "run-1"),
        (workspace.id, thread.id, "missing-run"),
      ]
      for workspace_id, thread_id, run_id in cases:
        with self.subTest(
            workspace_id=workspace_id,
            thread_id=thread_id,
            run_id=run_id,
        ):
          with self.assertRaises(RecordNotFoundError):
            read_run_history(
                root, saver, redis,
                workspace_id, thread_id, run_id,
            )

      saver.get_tuple.assert_not_called()
      redis.xread.assert_not_called()

  def test_thread_history_restores_tools_without_run_records(self):
    plan = AIMessage(
        id="old-plan",
        content="",
        tool_calls=[{
          "id": "old-call",
          "name": "lookup",
          "args": {},
        }],
    )
    result = ToolMessage(
        id="old-result",
        tool_call_id="old-call",
        content="旧工具结果",
    )

    with TemporaryDirectory() as directory:
      root = Path(directory)

      with WorkspaceStore(root) as store:
        workspace = store.create_workspace("旧研究")
        thread = store.create_thread(workspace.id, "旧会话")
        question = store.add_message(
            workspace.id, thread.id, "user", "查询现金流",
        )

      path = str(root / "checkpoints.sqlite")
      with SqliteSaver.from_conn_string(path) as saver:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"]["messages"] = [
          HumanMessage(
              id=f"business:{question.id}",
              content=question.content,
          ),
          plan,
          result,
        ]
        saver.put(
            {"configurable": {
              "thread_id": f"{workspace.id}:{thread.id}",
              "checkpoint_ns": "",
            }},
            checkpoint,
            {"source": "input", "step": 0, "parents": {}},
            {},
        )

      with SqliteSaver.from_conn_string(path) as saver:
        history = read_thread_history(
            root, saver, workspace.id, thread.id,
        )

      self.assertEqual(history, {
        "thread_id": thread.id,
        "turns": [{
          "user_message_id": question.id,
          "events": project_run_messages([plan, result]),
        }],
      })
      with WorkspaceStore(root) as store:
        self.assertEqual(
            store.list_research_runs(workspace.id, thread.id),
            [],
        )

  def test_restores_partial_text_from_sqlite_archive(self):
    redis = Mock(spec=Redis)

    with TemporaryDirectory() as directory:
      root = Path(directory)

      with WorkspaceStore(root) as store:
        workspace = store.create_workspace("测试研究")
        thread = store.create_thread(workspace.id, "测试会话")
        store.start_thread_execution(
            workspace.id, thread.id, run_id="run-1",
        )
        store.add_user_message(workspace.id, thread.id, "查询现金流")
        store.finish_thread_execution(
            workspace.id,
            thread.id,
            status="failed",
            error="模型连接中断",
            sse_events=[
              {
                "type": "text_delta",
                "message_id": "partial",
                "text": "目前",
              },
              {
                "type": "text_delta",
                "message_id": "partial",
                "text": "发现",
              },
            ],
        )

      with SqliteSaver.from_conn_string(
          str(root / "checkpoints.sqlite")
      ) as saver:
        history = read_run_history(
            root, saver, redis,
            workspace.id, thread.id, "run-1",
        )

      self.assertEqual(history["events"], [{
        "type": "assistant_message",
        "message_id": "partial",
        "text": "目前发现",
        "tool_calls": [],
      }])
      redis.xread.assert_not_called()
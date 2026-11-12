"""Exercise the real LangChain loop with a scripted model and temporary workspace storage."""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

from langchain.agents.middleware.model_call_limit import \
  ModelCallLimitExceededError
from langchain_core.language_models.fake_chat_models import \
  FakeMessagesListChatModel, FakeListChatModel, FakeListChatModelError
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import Field
from langchain_core.tools import tool

from corpcom.run_coordinator import RunCoordinator
from corpcom.workspace import WorkspaceStore
from corpcom.workspace_agent import ask_workspace
from corpcom.workspace_models import WorkspaceResearchInfo


class ScriptedModel(FakeMessagesListChatModel):
  """Replace only the model; framework routing, tool workers and SQLite remain real."""

  requests: list[list[BaseMessage]] = Field(default_factory=list)

  def bind_tools(self, tools, **kwargs):
    return self

  def _generate(self, messages, stop=None, run_manager=None, **kwargs):
    # Keep the input before the framework adds later replies and tool results.
    self.requests.append(deepcopy(messages))
    return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

class StreamingModel(FakeListChatModel):
  def bind_tools(self, tools, **kwargs):
    return self

class WorkspaceAgentTests(unittest.TestCase):
  def setUp(self):
    self.temporary = TemporaryDirectory()
    self.addCleanup(self.temporary.cleanup)
    self.root = Path(self.temporary.name)
    self.data = self.root / "data"
    with WorkspaceStore(self.data) as store:
      self.workspace = store.create_workspace(
          "公司研究", "检验经营韧性", "单家公司", "三个月",
          "现金流稳定", {"报告语言": "中文"},
      )
      self.thread = store.create_thread(self.workspace.id, "连续讨论")
    # Give each test an independent checkpoint store.
    self.checkpointer = InMemorySaver()

  def test_concurrent_first_turns_only_instruct_one_research_initialization(self):
    with WorkspaceStore(self.data) as store:
      empty = store.create_workspace("并发首次研究")
      threads = [store.create_thread(empty.id, str(i)) for i in range(2)]
    models = [
      ScriptedModel(responses=[AIMessage(content=f"回答 {i}")])
      for i in range(2)
    ]
    ready = Barrier(2)
    saved = Barrier(2)
    original_add = WorkspaceStore.add_user_message

    def save_before_prompt(store, workspace_id, thread_id, content):
      result = original_add(store, workspace_id, thread_id, content)
      # Both messages exist before either Agent builds its prompt; the
      # first-message decision must survive beyond the insertion transaction.
      saved.wait(timeout=5)
      return result

    checkpoint_path = str(self.data / "concurrent-checkpoints.sqlite")
    with SqliteSaver.from_conn_string(checkpoint_path) as checkpointer:
      coordinator = RunCoordinator(checkpointer, max_concurrent_runs=2)

      def run(index):
        with WorkspaceStore(self.data) as store:
          ready.wait(timeout=5)
          return coordinator.run(
              store, models[index], empty.id, threads[index].id,
              f"研究问题 {index}",
          )

      with patch.object(WorkspaceStore, "add_user_message", new=save_before_prompt):
        with ThreadPoolExecutor(max_workers=2) as executor:
          answers = list(executor.map(run, range(2)))

    initialize = [
      "这是整个 Workspace 的第一条用户消息" in model.requests[0][0].content
      for model in models
    ]
    self.assertEqual(sorted(initialize), [False, True])
    with WorkspaceStore(self.data) as store:
      details = [store.get_thread(empty.id, thread.id) for thread in threads]
      first_index = min(range(2), key=lambda i: details[i].messages[0].id)
      self.assertTrue(initialize[first_index])
      for index, detail in enumerate(details):
        self.assertEqual(detail.execution_status, "succeeded")
        self.assertEqual(
            [(m.role, m.content) for m in detail.messages],
            [("user", f"研究问题 {index}"), ("assistant", f"回答 {index}")],
        )
        self.assertEqual(detail.messages[-1], answers[index])

  def test_first_workspace_message_initializes_empty_research_and_persists_it(self):
    generated = {
      "goal": "比较茅台与五粮液的经营现金流质量",
      "scope": "贵州茅台、五粮液",
      "initial_hypothesis": "待验证假设：两家公司的现金流质量存在差异",
      "observation_period": "2023—2025 年度",
      "preferences": {"报告形式": "简短"},
    }
    model = ScriptedModel(responses=[
      AIMessage(content="", tool_calls=[{
        "id": "initialize-research", "name": "update_research",
        "args": {"changes": generated},
      }]),
      AIMessage(content="已保存研究信息，接下来核对两家公司年报。"),
    ])
    with WorkspaceStore(self.data) as store:
      # History in another workspace and empty sibling threads do not block initialization.
      store.add_message(self.workspace.id, self.thread.id, "user", "其他研究的历史")
      empty = store.create_workspace("新研究", goal=" \n", scope="\t")
      store.create_thread(empty.id, "尚未开始的会话")
      thread = store.create_thread(empty.id, "首次研究")
      ask_workspace(
          store, model, empty.id, thread.id,
          "比较茅台与五粮液2023—2025年度的经营现金流质量，报告简短些。",
          self.checkpointer,
      )
    self.assertIn("这是整个 Workspace 的第一条用户消息", model.requests[0][0].content)
    result = next(message for message in model.requests[1] if message.type == "tool")
    self.assertEqual(json.loads(result.content), generated)
    with WorkspaceStore(self.data) as store:
      self.assertEqual(store.get_workspace(empty.id).info.model_dump(), generated)
      path = self.data / "workspaces" / empty.id / "research.json"
      self.assertEqual(json.loads(path.read_text(encoding="utf-8")), generated)
      next_thread = store.create_thread(empty.id, "继续研究")
      next_model = ScriptedModel(responses=[AIMessage(content="继续核对年报。")])
      ask_workspace(store, next_model, empty.id, next_thread.id, "继续", self.checkpointer)
    prompt = next_model.requests[0][0].content
    self.assertNotIn("这是整个 Workspace 的第一条用户消息", prompt)
    context = json.loads(prompt.split("当前 Workspace 的已保存研究状态：\n", 1)[1])
    for key, value in generated.items():
      self.assertEqual(context[key], value)

  def test_first_message_does_not_initialize_partially_filled_research(self):
    for values in (
        {"goal": "已有目标"}, {"scope": "白酒行业"},
        {"initial_hypothesis": "已有假设"}, {"observation_period": "未来一年"},
        {"preferences": {"报告语言": "中文"}},
    ):
      with self.subTest(values=values), WorkspaceStore(self.data) as store:
        workspace = store.create_workspace("已填写研究", **values)
        thread = store.create_thread(workspace.id, "首次会话")
        model = ScriptedModel(responses=[AIMessage(content="继续现有研究。")])
        ask_workspace(store, model, workspace.id, thread.id,
                      "分析现金流", self.checkpointer)
        self.assertNotIn("这是整个 Workspace 的第一条用户消息", model.requests[0][0].content)
        self.assertEqual(store.get_workspace(workspace.id).info, workspace.info)

  def test_empty_info_is_not_initialized_after_a_prior_workspace_message(self):
    with WorkspaceStore(self.data) as store:
      workspace = store.create_workspace("空研究")
      first = store.create_thread(workspace.id, "旧会话")
      # A saved user message counts even if its model call did not finish.
      store.add_message(workspace.id, first.id, "user", "之前的问题")
      second = store.create_thread(workspace.id, "新会话")
      for thread in (first, second):
        with self.subTest(thread=thread.id):
          model = ScriptedModel(responses=[AIMessage(content="继续讨论。")])
          ask_workspace(store, model, workspace.id, thread.id,
                        "比较两家公司现金流", self.checkpointer)
          self.assertNotIn("这是整个 Workspace 的第一条用户消息", model.requests[0][0].content)
          self.assertEqual(store.get_workspace(workspace.id).info, WorkspaceResearchInfo())

  def test_agent_updates_research_and_new_thread_reads_saved_context(self):
    events = []
    model = ScriptedModel(responses=[
      AIMessage(content="", tool_calls=[{
        "id": "update-research", "name": "update_research",
        "args": {"changes": {
          "goal": "核对现金流质量", "scope": "",
          "observation_period": "未来一年",
          "preferences": {"报告语言": "中文", "报告形式": "简短"},
        }},
      }]),
      AIMessage(content="已保存研究信息。"),
    ])
    with WorkspaceStore(self.data) as store:
      other = store.create_workspace("另一个研究")
      ask_workspace(store, model, self.workspace.id, self.thread.id,
                    "目标改为核对现金流质量，清空范围，观察一年，报告简短且保留中文。",
                    self.checkpointer, on_event=events.append)
      # tool events
      tool_events = [
        event for event in events
        if event["type"].startswith("tool_")
      ]
      self.assertEqual(
          [event["type"] for event in tool_events],
          ["tool_start", "tool_end"],
      )
      self.assertEqual(tool_events[0]["name"], "update_research")
      self.assertEqual(
          tool_events[0]["tool_run_id"],
          tool_events[1]["tool_run_id"],
      )

      saved = store.get_workspace(self.workspace.id)
      self.assertEqual(saved.info.goal, "核对现金流质量")
      self.assertEqual(saved.info.scope, "")
      self.assertEqual(saved.info.observation_period, "未来一年")
      self.assertEqual(saved.info.initial_hypothesis, self.workspace.info.initial_hypothesis)
      self.assertEqual(saved.info.preferences, {"报告语言": "中文", "报告形式": "简短"})
      self.assertEqual(store.get_workspace(other.id), other)
    tool_result = next(m for m in model.requests[1] if m.type == "tool")
    self.assertEqual(json.loads(tool_result.content)["goal"], saved.info.goal)
    with WorkspaceStore(self.data) as store:
      second = store.create_thread(self.workspace.id, "新会话")
      next_model = ScriptedModel(responses=[AIMessage(content="继续核对现金流质量。")])
      ask_workspace(store, next_model, self.workspace.id, second.id,
                    "当前研究目标是什么？", self.checkpointer)
    prompt = next_model.requests[0][0].content
    context = json.loads(prompt.split("当前 Workspace 的已保存研究状态：\n", 1)[1])
    self.assertEqual(context["goal"], saved.info.goal)
    self.assertEqual(context["scope"], "")
    self.assertEqual(context["initial_hypothesis"], saved.info.initial_hypothesis)
    self.assertEqual(context["preferences"], saved.info.preferences)

  def test_same_thread_retains_both_turns_in_context_and_storage(self):
    first_question = "先分析什么指标？"
    first_answer = "先分析经营现金流，再核对净利润。"
    second_question = "按刚才的顺序，第二个指标是什么？"
    second_answer = "第二个指标是净利润。"
    model = ScriptedModel(responses=[
      AIMessage(content=first_answer), AIMessage(content=second_answer),
    ])

    checkpoint_path = str(self.data / "checkpoint.sqlite")
    with (
      WorkspaceStore(self.data) as store,
      SqliteSaver.from_conn_string(checkpoint_path) as checkpointer
    ):
      self.assertEqual(
          RunCoordinator(checkpointer, max_concurrent_runs=2).run(
              store, model, self.workspace.id, self.thread.id, first_question
          ).content,
          first_answer,
      )
    # Reopen both databases; no checkpoint connection is reused.
    with (
      WorkspaceStore(self.data) as store,
      SqliteSaver.from_conn_string(checkpoint_path) as checkpointer
    ):
      # Existing checkpoints must supply history without reading business messages.
      with patch.object(
          store,
          "get_thread",
          side_effect=AssertionError("第二轮不应从业务库重新加载历史"),
      ):
        self.assertEqual(
            RunCoordinator(checkpointer, max_concurrent_runs=2).run(
                store, model, self.workspace.id, self.thread.id,
                second_question
            ).content,
            second_answer,
        )

      config = {
        "configurable": {
          "thread_id": f"{self.workspace.id}:{self.thread.id}",
        }
      }
      saved = checkpointer.get_tuple(config)
      self.assertIsNotNone(saved)
      saved_messages = saved.checkpoint["channel_values"]["messages"]
      self.assertEqual(
          [(item.type, item.content) for item in saved_messages],
          [
            ("human", first_question),
            ("ai", first_answer),
            ("human", second_question),
            ("ai", second_answer),
          ],
      )

    self.assertEqual(len(model.requests), 2)
    self.assertEqual(model.requests[0][0].type, "system")
    self.assertEqual(model.requests[1][0].type, "system")
    self.assertEqual(
        [(item.type, item.content) for item in model.requests[0][1:]],
        [("human", first_question)],
    )
    self.assertEqual(
        [(item.type, item.content) for item in model.requests[1][1:]],
        [("human", first_question), ("ai", first_answer), ("human", second_question)],
    )
    with WorkspaceStore(self.data) as store:
      self.assertEqual(
          [(item.role, item.content) for item in store.get_thread(self.workspace.id, self.thread.id).messages],
          [("user", first_question), ("assistant", first_answer),
           ("user", second_question), ("assistant", second_answer)],
      )

  def test_new_thread_reads_prior_material_and_saves_answer(self):
    plan = self.root / "plan.md"
    plan.write_text("# 原始计划\n检查现金流。\n", encoding="utf-8")
    with WorkspaceStore(self.data) as store:
      store.add_message(self.workspace.id, self.thread.id, "user", "先核对现金流")
      file = store.import_file(self.workspace.id, plan, "plan")
      second = store.create_thread(self.workspace.id, "第二天")

    model = ScriptedModel(responses=[
      AIMessage(content="", tool_calls=[
        {"id": "old_thread", "name": "read_thread", "args": {"thread_id": self.thread.id}},
        # read_file 工具暂时停用。
        # {"id": "plan_file", "name": "read_file", "args": {"artifact_id": file.id}},
      ]),
      AIMessage(content="已承接旧会话，下一步核对现金流。"),
    ])

    checkpoint_path = str(self.data / "checkpoint.sqlite")
    with (
      WorkspaceStore(self.data) as store,
      SqliteSaver.from_conn_string(checkpoint_path) as checkpointer,
    ):
      answer = RunCoordinator(checkpointer, max_concurrent_runs=2).run(
          store, model, self.workspace.id, second.id,
          "继续昨天的研究"
      ).content
      self.assertIn("已承接旧会话", answer)
      self.assertEqual(len(model.requests), 2)
      context = model.requests[0][0].content
      self.assertIn("检验经营韧性", context)
      self.assertIn("现金流稳定", context)
      self.assertIn('"报告语言": "中文"', context)
      for request in model.requests:
        prompt = request[0].content
        state = json.loads(prompt.split("当前 Workspace 的已保存研究状态：\n", 1)[1])
        self.assertNotIn("files", state)
        self.assertNotIn(file.id, prompt)
        self.assertNotIn(file.name, prompt)
        self.assertNotIn(str(file.path), prompt)
        self.assertIn("/workspace/files/", prompt)
        self.assertIn("/workspace/tasks/", prompt)
      self.assertEqual(store.get_file(self.workspace.id, file.id), file)
      self.assertEqual(model.requests[0][-1].content, "继续昨天的研究")
      # Read through a real framework worker, exercising SQLite thread safety.
      results = {
        item.tool_call_id: json.loads(item.content)
        for item in model.requests[1] if item.type == "tool"
      }
      self.assertEqual(results["old_thread"]["messages"], [{"role": "user", "content": "先核对现金流"}])
      # self.assertEqual(results["plan_file"]["content"], "# 原始计划\n检查现金流。\n")
      self.assertEqual(
          [(item.role, item.content) for item in store.get_thread(self.workspace.id, second.id).messages],
          [("user", "继续昨天的研究"), ("assistant", answer)],
      )
      self.assertEqual(len(store.get_thread(self.workspace.id, self.thread.id).messages), 1)

    # Read from a fresh connection to verify tool messages reached disk.
    with SqliteSaver.from_conn_string(checkpoint_path) as checkpointer:
      config = {
        "configurable": {
          "thread_id": f"{self.workspace.id}:{second.id}",
        }
      }
      saved = checkpointer.get_tuple(config)
      self.assertIsNotNone(saved)
      saved_messages = saved.checkpoint["channel_values"]["messages"]

      self.assertEqual(
          [item.type for item in saved_messages],
          ["human", "ai", "tool", "ai"],
      )
      self.assertEqual(
          {
            call["id"]: call["name"]
            for call in saved_messages[1].tool_calls
          },
          {
            "old_thread": "read_thread",
            # "plan_file": "read_file",
          },
      )

      saved_results = {
        item.tool_call_id: json.loads(item.content)
        for item in saved_messages
        if item.type == "tool"
      }
      self.assertEqual(saved_results, results)
      self.assertEqual(saved_messages[-1].content, answer)


  def test_tools_reject_material_from_another_workspace(self):
    source = self.root / "private.txt"
    source.write_text("属于另一个研究", encoding="utf-8")
    with WorkspaceStore(self.data) as store:
      other = store.create_workspace("其他研究", "其他目标", "", "", "其他假设", {})
      private_thread = store.create_thread(other.id, "私有会话")
      store.add_message(other.id, private_thread.id, "user", "私有消息")
      private_file = store.import_file(other.id, source, "plan")
      for name, arguments in (
          ("read_thread", {"thread_id": private_thread.id}),
          # ("read_file", {"artifact_id": private_file.id}),
      ):
        with self.subTest(tool=name):
          thread = store.create_thread(self.workspace.id, name)
          model = ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[{
              "id": "foreign_read",
              "name": name,
              "args": arguments},]),
            AIMessage(content="当前 Workspace 中找不到指定资料。"),
          ])
          answer = RunCoordinator(self.checkpointer, max_concurrent_runs=2).run(
              store, model, self.workspace.id, thread.id,"读取指定资料"
          ).content
          self.assertEqual(
              answer, "当前 Workspace 中找不到指定资料。",
          )
          self.assertEqual(len(model.requests), 2)

          results = [
            m for m in model.requests[1] if m.type == "tool"
          ]
          self.assertEqual(len(results), 1)
          error = results[0]
          self.assertEqual(error.tool_call_id, "foreign_read")
          self.assertEqual(error.name, name)
          self.assertEqual(error.status, "error")
          self.assertEqual(
              error.content,
              f"thread not found in workspace: {private_thread.id}",
          )

          # Neither context nor tool results may expose foreign material.
          for request in model.requests:
            context = "\n".join(str(m.content) for m in request)
            for private_value in (
                "私有会话", "私有消息",
                "private.txt", "属于另一个研究",
                str(self.data),
            ):
              self.assertNotIn(private_value, context)
          self.assertEqual(
              [
                item.role
                for item in store.get_thread(
                  self.workspace.id, thread.id,
              ).messages
              ],
              ["user", "assistant"],
          )

      self.assertEqual(len(store.get_thread(other.id, private_thread.id).messages), 1)

  def test_model_call_limit_raises_without_saving_a_success_answer(self):
    # The fake repeats the same read indefinitely; the real middleware must stop it.
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[
      {"id": "repeat", "name": "read_thread", "args": {"thread_id": self.thread.id}},
    ])])
    with WorkspaceStore(self.data) as store:
      with self.assertRaises(ModelCallLimitExceededError):
        ask_workspace(store, model, self.workspace.id, self.thread.id,
                      "反复读取", self.checkpointer)
      self.assertEqual(len(model.requests), 100)
      self.assertEqual(
          [item.role for item in store.get_thread(self.workspace.id, self.thread.id).messages],
          ["user"],
      )

  def test_empty_model_answer_is_not_saved(self):
    model = ScriptedModel(responses=[AIMessage(content="")])
    with WorkspaceStore(self.data) as store:
      with self.assertRaisesRegex(RuntimeError, "model returned no answer"):
        ask_workspace(store, model, self.workspace.id, self.thread.id,
                      "继续研究", self.checkpointer)
      self.assertEqual(
          [item.role for item in store.get_thread(self.workspace.id, self.thread.id).messages],
          ["user"],
      )

  def test_existing_history_is_imported_only_once(self):
    model = ScriptedModel(responses=[
      AIMessage(content="继续核对现金流。", tool_calls=[]),
      AIMessage(content="下一步核对净利润。", tool_calls=[])
    ])

    with WorkspaceStore(self.data) as store:
      # Simulate a conversation created before checkpointing was enabled.
      store.add_message(
          self.workspace.id, self.thread.id,
          "user", "之前研究什么？",
      )
      store.add_message(
          self.workspace.id, self.thread.id,
          "assistant", "之前研究现金流。",
      )

      with patch.object(
          store, "get_thread", wraps=store.get_thread,
      ) as read_history:
        ask_workspace(
            store, model, self.workspace.id, self.thread.id,
            "继续研究", self.checkpointer,
        )
        ask_workspace(
            store, model, self.workspace.id, self.thread.id,
            "下一步呢？", self.checkpointer,
        )
        self.assertEqual(read_history.call_count, 1)

    expected = [
      ("human", "之前研究什么？"),
      ("ai", "之前研究现金流。"),
      ("human", "继续研究"),
      ("ai", "继续核对现金流。"),
      ("human", "下一步呢？"),
      ("ai", "下一步核对净利润。"),
    ]

    self.assertEqual(
        [(m.type, m.content) for m in model.requests[0][1:]],
        expected[:3],
    )
    self.assertEqual(
        [(m.type, m.content) for m in model.requests[1][1:]],
        expected[:5],
    )

    config = {
      "configurable": {
        "thread_id": f"{self.workspace.id}:{self.thread.id}",
      },
    }
    saved = self.checkpointer.get_tuple(config)
    self.assertIsNotNone(saved)
    self.assertEqual(
        [
          (m.type, m.content)
          for m in saved.checkpoint["channel_values"]["messages"]
        ],
        expected,
    )

  def test_checkpoint_isolates_threads(self):
    model = ScriptedModel(responses=[
      AIMessage(content="A 会话关注现金流。"),
      AIMessage(content="B 会话关注营收。"),
      AIMessage(content="A 会话之前讨论现金流。"),
    ])

    with WorkspaceStore(self.data) as store:
      second = store.create_thread(self.workspace.id, "独立讨论")

      ask_workspace(
          store, model, self.workspace.id, self.thread.id,
          "A：分析现金流", self.checkpointer,
      )
      ask_workspace(
          store, model, self.workspace.id, second.id,
          "B：分析营收", self.checkpointer,
      )
      ask_workspace(
          store, model, self.workspace.id, self.thread.id,
          "A：刚才讨论什么？", self.checkpointer,
      )

    # Thread B starts without thread A's conversation messages.
    self.assertEqual(
        [(m.type, m.content) for m in model.requests[1][1:]],
        [("human", "B：分析营收")],
    )
    # Returning to A restores only A's earlier exchange.
    self.assertEqual(
        [(m.type, m.content) for m in model.requests[2][1:]],
        [
          ("human", "A：分析现金流"),
          ("ai", "A 会话关注现金流。"),
          ("human", "A：刚才讨论什么？"),
        ],
    )

  def test_file_tools_allow_correction_after_failed_edit(self):
    model = ScriptedModel(responses=[
      AIMessage(content="", tool_calls=[{
        "id": "write", "name": "write_file",
        "args": {"file_path": "tasks/demo/note.txt", "content": "原始内容"},
      }]),
      AIMessage(content="", tool_calls=[{
        "id": "bad-edit", "name": "edit_file",
        "args": {"file_path": "tasks/demo/note.txt", "old_string": "不存在", "new_string": "更新"},
      }]),
      AIMessage(content="", tool_calls=[{
        "id": "edit", "name": "edit_file",
        "args": {"file_path": "tasks/demo/note.txt", "old_string": "原始内容", "new_string": "更新"},
      }]),
      AIMessage(content="文件已更新。"),
    ])
    with WorkspaceStore(self.data) as store:
      answer = ask_workspace(store, model, self.workspace.id, self.thread.id,
                             "写入并编辑笔记", self.checkpointer)
      self.assertEqual(answer.text, "文件已更新。")
      self.assertEqual(store.get_workspace_detail(self.workspace.id).artifacts, [])
    path = self.data / "workspaces" / self.workspace.id / "tasks/demo/note.txt"
    self.assertEqual(path.read_text(encoding="utf-8"), "更新")
    results = {m.tool_call_id: m for m in model.requests[-1] if m.type == "tool"}
    self.assertEqual(results["write"].status, "success")
    self.assertEqual(results["bad-edit"].status, "error")
    self.assertIn("not found", results["bad-edit"].content)
    self.assertEqual(results["edit"].status, "success")
    self.assertEqual(json.loads(results["edit"].content)["replacements"], 1)

  def test_missing_record_error_persists_and_allows_next_turn(self):
    model = ScriptedModel(responses=[
      AIMessage(content="", tool_calls=[
        {
          "id": "missing_read",
          "name": "read_thread",
          "args": {"thread_id": "missing"},
        },
      ]),
      AIMessage(content="找不到指定资料，请核对 ID。"),
    ])
    checkpoint_path = str(self.data / "checkpoint.sqlite")
    config = {
      "configurable": {
        "thread_id": f"{self.workspace.id}:{self.thread.id}",
      },
    }

    with (
      WorkspaceStore(self.data) as store,
      SqliteSaver.from_conn_string(checkpoint_path) as checkpointer,
    ):
      answer = ask_workspace(
          store, model, self.workspace.id, self.thread.id,
          "读取不存在的会话", checkpointer,
      )
      self.assertEqual(answer.text, "找不到指定资料，请核对 ID。")
      self.assertEqual(len(model.requests), 2)

      # The model must receive a result matching its failed tool call.
      results = [
        m for m in model.requests[1] if m.type == "tool"
      ]
      self.assertEqual(len(results), 1)
      error = results[0]
      self.assertEqual(error.tool_call_id, "missing_read")
      self.assertEqual(error.name, "read_thread")
      self.assertEqual(error.status, "error")
      self.assertEqual(error.content, "thread not found in workspace: missing")
      self.assertNotIn(str(self.data), error.content)

    # Reopen SQLite to verify persistence and continuation.
    with (
      WorkspaceStore(self.data) as store,
      SqliteSaver.from_conn_string(checkpoint_path) as checkpointer,
    ):
      saved = checkpointer.get_tuple(config)
      self.assertIsNotNone(saved)
      messages = saved.checkpoint["channel_values"]["messages"]
      self.assertEqual(
          [m.type for m in messages],
          ["human", "ai", "tool", "ai"],
      )
      self.assertEqual(messages[2], error)

      next_model = ScriptedModel(responses=[
        AIMessage(content="可以继续研究。"),
      ])
      answer = ask_workspace(
          store, next_model, self.workspace.id, self.thread.id,
          "继续研究", checkpointer,
      )
      self.assertEqual(answer.text, "可以继续研究。")
      self.assertEqual(
          next_model.requests[0][1:-1],
          messages,
      )

  def test_unexpected_tool_error_propagates(self):
    model = ScriptedModel(responses=[
      AIMessage(content="", tool_calls=[
        {
          "id": "broken_read",
          "name": "update_research",
          "args": {"changes": {"goal": "更新目标"}},
        },
      ]),
    ])

    with WorkspaceStore(self.data) as store:
      # Patch the class because tool workers create their own store instances.
      with patch.object(
          WorkspaceStore,
          "update_research",
          side_effect=RuntimeError("unexpected storage failure"),
      ):
        with self.assertRaisesRegex(
            RuntimeError, "unexpected storage failure",
        ):
          ask_workspace(
              store, model, self.workspace.id, self.thread.id,
              "读取资料", self.checkpointer,
          )

      self.assertEqual(len(model.requests), 1)
      self.assertEqual(
          [
            item.role
            for item in store.get_thread(
              self.workspace.id, self.thread.id,
          ).messages
          ],
          ["user"],
      )

  def test_streaming_success_events_and_saved_answer(self):
    events = []
    model = StreamingModel(responses=["你好"])
    coordinator = RunCoordinator(
        self.checkpointer, max_concurrent_runs=1
    )
    with WorkspaceStore(self.data) as store:
      message = coordinator.run(
          store, model, self.workspace.id, self.thread.id, "测试流式传输",
          on_event=events.append,
      )

      detail = store.get_thread(self.workspace.id, self.thread.id)

      self.assertEqual(events[0]["workspace_id"], self.workspace.id)
      self.assertEqual(events[0]["thread_id"], self.thread.id)
      self.assertEqual(
          [event["type"] for event in events],
          ["started", "text_delta", "text_delta", "succeeded"],
      )
      self.assertEqual([event["text"] for event in events[1:3]], ["你", "好"])
      self.assertEqual(events[1]["message_id"], events[2]["message_id"])
      self.assertIsNotNone(events[1]["message_id"])

      self.assertEqual(detail.execution_status, "succeeded")

      assistant_messages = [
        message for message in detail.messages
        if message.role == "assistant"
      ]
      self.assertEqual(len(assistant_messages), 1)
      self.assertEqual(assistant_messages[0].content, "你好")

      self.assertEqual(message.content, "你好")
      self.assertEqual(
          events[-1]["message"],
          message.model_dump(mode="json"),
      )
      self.assertEqual(assistant_messages[0], message)

  def test_streaming_failure_marks_failed_and_releases_capacity(self):
    model = StreamingModel(
        responses=["你好"],
        error_on_chunk_number=1,
    )
    events = []
    coordinator = RunCoordinator(
        self.checkpointer, max_concurrent_runs=1
    )
    with WorkspaceStore(self.data) as store:
      with self.assertRaises(FakeListChatModelError):
        coordinator.run(
            store, model, self.workspace.id, self.thread.id, "测试失败",
            on_event=events.append,
        )
      detail = store.get_thread(self.workspace.id, self.thread.id)
      self.assertEqual(
          [event["type"] for event in events],
          ["started", "text_delta", "failed"],
      )
      self.assertEqual(events[1]["text"], "你")
      self.assertEqual(detail.execution_status, "failed")
      self.assertEqual(detail.execution_error, events[-1]["error"])
      self.assertEqual([msg.role for msg in detail.messages], ["user"])

      model = StreamingModel(
          responses=["恢复正常"],
      )
      message = coordinator.run(
          store, model, self.workspace.id, self.thread.id, "测试失败后恢复",
      )
      self.assertEqual(message.content, "恢复正常")
      self.assertEqual(
          store.get_thread(self.workspace.id, self.thread.id).execution_status,
          "succeeded",
      )

  def test_plan_is_committed_before_tool_and_retained_after_completion(self):
    observed = []

    @tool
    def inspect_record() -> str:
      """Inspect a research record."""
      with WorkspaceStore(self.data) as reader:
        observed.append(
            reader.get_thread(self.workspace.id, self.thread.id)
        )
      return "已核对"

    model = ScriptedModel(responses=[
      AIMessage(
          id="plan-1",
          content="先核对记录。",
          tool_calls=[{
            "id": "call-1",
            "name": "inspect_record",
            "args": {},
          }],
      ),
      AIMessage(id="answer-1", content="核对完成。"),
    ])
    coordinator = RunCoordinator(
        self.checkpointer, max_concurrent_runs=1,
    )

    with (
      WorkspaceStore(self.data) as store,
      patch(
          "corpcom.workspace_agent.create_workspace_tools",
          return_value=[inspect_record],
      ),
    ):
      answer = coordinator.run(
          store, model, self.workspace.id, self.thread.id, "开始",
      )

    # A separate connection saw the committed plan during tool execution.
    self.assertEqual(len(observed), 1)
    during_tool = observed[0]
    self.assertEqual(during_tool.execution_status, "running")
    self.assertEqual(
        [m.content for m in during_tool.messages],
        ["开始", "先核对记录。"],
    )
    self.assertEqual(
        during_tool.messages[-1].model_message_id, "plan-1",
    )

    with WorkspaceStore(self.data) as reader:
      detail = reader.get_thread(self.workspace.id, self.thread.id)

    self.assertEqual(detail.execution_status, "succeeded")
    self.assertEqual(
        [(m.role, m.content, m.model_message_id) for m in detail.messages],
        [
          ("user", "开始", None),
          ("assistant", "先核对记录。", "plan-1"),
          ("assistant", "核对完成。", "answer-1"),
        ],
    )
    self.assertEqual(detail.messages[-1], answer)

  def test_completed_plan_survives_tool_failure(self):
    @tool
    def inspect_record() -> str:
      """Inspect a research record."""
      raise RuntimeError("record lookup failed")

    model = ScriptedModel(responses=[
      AIMessage(
          id="plan-1",
          content="先核对记录。",
          tool_calls=[{
            "id": "call-1",
            "name": "inspect_record",
            "args": {},
          }],
      ),
    ])
    coordinator = RunCoordinator(
        self.checkpointer, max_concurrent_runs=1,
    )

    with (
      WorkspaceStore(self.data) as store,
      patch(
          "corpcom.workspace_agent.create_workspace_tools",
          return_value=[inspect_record],
      ),
    ):
      with self.assertRaisesRegex(RuntimeError, "record lookup failed"):
        coordinator.run(
            store, model, self.workspace.id, self.thread.id, "开始",
        )

    # Completed messages remain readable after a later execution failure.
    with WorkspaceStore(self.data) as reader:
      detail = reader.get_thread(self.workspace.id, self.thread.id)

    self.assertEqual(detail.execution_status, "failed")
    self.assertEqual(detail.execution_error, "record lookup failed")
    self.assertEqual(
        [(m.role, m.content, m.model_message_id) for m in detail.messages],
        [
          ("user", "开始", None),
          ("assistant", "先核对记录。", "plan-1"),
        ],
    )
    self.assertEqual(len(model.requests), 1)

if __name__ == "__main__":
  unittest.main()

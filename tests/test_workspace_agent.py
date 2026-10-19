"""Exercise the real LangChain loop with a scripted model and temporary workspace storage."""

from copy import deepcopy
import json
from logging import setLoggerClass
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from corpcom import workspace
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.memory import InMemorySaver
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import Field

from corpcom.workspace import WorkspaceStore
from corpcom.workspace_agent import ask_workspace


class ScriptedModel(FakeMessagesListChatModel):
    """Replace only the model; framework routing, tool workers and SQLite remain real."""

    requests: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        # Keep the input before the framework adds later replies and tool results.
        self.requests.append(deepcopy(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


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
                ask_workspace(store, model, self.workspace.id, self.thread.id,
                              first_question, checkpointer),
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
                    ask_workspace(store, model, self.workspace.id, self.thread.id,
                                  second_question, checkpointer),
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
                {"id": "plan_file", "name": "read_file", "args": {"artifact_id": file.id}},
            ]),
            AIMessage(content="已承接旧会话和研究计划，下一步核对现金流。"),
        ])

        checkpoint_path = str(self.data / "checkpoint.sqlite")
        with (
            WorkspaceStore(self.data) as store,
            SqliteSaver.from_conn_string(checkpoint_path) as checkpointer,
        ):
            answer = ask_workspace(
                store, model, self.workspace.id, second.id,
                "继续昨天的研究", checkpointer,
            )
            self.assertIn("旧会话和研究计划", answer)
            self.assertEqual(len(model.requests), 2)
            context = model.requests[0][0].content
            self.assertIn("检验经营韧性", context)
            self.assertIn("现金流稳定", context)
            self.assertIn('"报告语言": "中文"', context)
            self.assertEqual(model.requests[0][-1].content, "继续昨天的研究")
            # Both reads execute through real framework workers, exercising SQLite thread safety.
            results = {
                item.tool_call_id: json.loads(item.content)
                for item in model.requests[1] if item.type == "tool"
            }
            self.assertEqual(results["old_thread"]["messages"], [{"role": "user", "content": "先核对现金流"}])
            self.assertEqual(results["plan_file"]["content"], "# 原始计划\n检查现金流。\n")
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
                ["human", "ai", "tool", "tool", "ai"],
            )
            self.assertEqual(
                {
                    call["id"]: call["name"]
                    for call in saved_messages[1].tool_calls
                },
                {
                    "old_thread": "read_thread",
                    "plan_file": "read_file",
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
                ("read_file", {"artifact_id": private_file.id}),
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
                    answer = ask_workspace(store, model, self.workspace.id, thread.id,
                                           "读取指定资料", self.checkpointer)
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
                    self.assertIn("找不到指定资料", error.content)

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
            self.assertEqual(len(model.requests), 6)
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

    def test_missing_record_error_persists_and_allows_next_turn(self):
        model = ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[
                {
                    "id": "missing_read",
                    "name": "read_file",
                    "args": {"artifact_id": "missing"},
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
                "读取不存在的文件", checkpointer,
            )
            self.assertEqual(answer, "找不到指定资料，请核对 ID。")
            self.assertEqual(len(model.requests), 2)

            # The model must receive a result matching its failed tool call.
            results = [
                m for m in model.requests[1] if m.type == "tool"
            ]
            self.assertEqual(len(results), 1)
            error = results[0]
            self.assertEqual(error.tool_call_id, "missing_read")
            self.assertEqual(error.name, "read_file")
            self.assertEqual(error.status, "error")
            self.assertIn("找不到指定资料", error.content)
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
            self.assertEqual(answer, "可以继续研究。")
            self.assertEqual(
                next_model.requests[0][1:-1],
                messages,
            )

    def test_unexpected_tool_error_propagates(self):
        model = ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[
                {
                    "id": "broken_read",
                    "name": "read_file",
                    "args": {"artifact_id": "any-file"},
                },
            ]),
        ])

        with WorkspaceStore(self.data) as store:
            # Patch the class because tool workers create their own store instances.
            with patch.object(
                WorkspaceStore,
                "get_file",
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

if __name__ == "__main__":
    unittest.main()

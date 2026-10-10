"""Exercise the real LangChain loop with a scripted model and temporary workspace storage."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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

    def test_same_thread_retains_both_turns_in_context_and_storage(self):
        first_question = "先分析什么指标？"
        first_answer = "先分析经营现金流，再核对净利润。"
        second_question = "按刚才的顺序，第二个指标是什么？"
        second_answer = "第二个指标是净利润。"
        model = ScriptedModel(responses=[
            AIMessage(content=first_answer), AIMessage(content=second_answer),
        ])

        with WorkspaceStore(self.data) as store:
            self.assertEqual(
                ask_workspace(store, model, self.workspace.id, self.thread.id, first_question),
                first_answer,
            )
        # Reopen between turns: the next invocation must use the committed conversation.
        with WorkspaceStore(self.data) as store:
            self.assertEqual(
                ask_workspace(store, model, self.workspace.id, self.thread.id, second_question),
                second_answer,
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
        with WorkspaceStore(self.data) as store:
            answer = ask_workspace(store, model, self.workspace.id, second.id, "继续昨天的研究")
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

    def test_tools_reject_material_from_another_workspace(self):
        source = self.root / "private.txt"
        source.write_text("属于另一个研究", encoding="utf-8")
        with WorkspaceStore(self.data) as store:
            other = store.create_workspace("其他研究", "其他目标", "", "", "其他假设", {})
            private_thread = store.create_thread(other.id, "私有会话")
            store.add_message(other.id, private_thread.id, "user", "私有消息")
            private_file = store.import_file(other.id, source, "plan")
            for name, arguments, error in (
                ("read_thread", {"thread_id": private_thread.id}, "thread not found in workspace"),
                ("read_file", {"artifact_id": private_file.id}, "file not found in workspace"),
            ):
                with self.subTest(tool=name):
                    thread = store.create_thread(self.workspace.id, name)
                    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[
                        {"id": "foreign_read", "name": name, "args": arguments},
                    ])])
                    with self.assertRaisesRegex(ValueError, error):
                        ask_workspace(store, model, self.workspace.id, thread.id, "读取指定资料")
                    self.assertEqual(len(model.requests), 1)
                    self.assertEqual(
                        [item.role for item in store.get_thread(self.workspace.id, thread.id).messages],
                        ["user"],
                    )
            self.assertEqual(len(store.get_thread(other.id, private_thread.id).messages), 1)

    def test_model_call_limit_raises_without_saving_a_success_answer(self):
        # The fake repeats the same read indefinitely; the real middleware must stop it.
        model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[
            {"id": "repeat", "name": "read_thread", "args": {"thread_id": self.thread.id}},
        ])])
        with WorkspaceStore(self.data) as store:
            with self.assertRaises(ModelCallLimitExceededError):
                ask_workspace(store, model, self.workspace.id, self.thread.id, "反复读取")
            self.assertEqual(len(model.requests), 6)
            self.assertEqual(
                [item.role for item in store.get_thread(self.workspace.id, self.thread.id).messages],
                ["user"],
            )

    def test_empty_model_answer_is_not_saved(self):
        model = ScriptedModel(responses=[AIMessage(content="")])
        with WorkspaceStore(self.data) as store:
            with self.assertRaisesRegex(RuntimeError, "model returned no answer"):
                ask_workspace(store, model, self.workspace.id, self.thread.id, "继续研究")
            self.assertEqual(
                [item.role for item in store.get_thread(self.workspace.id, self.thread.id).messages],
                ["user"],
            )


if __name__ == "__main__":
    unittest.main()

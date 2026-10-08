"""Verify research continuity across threads and within a multi-turn conversation."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from corpcom.workspace import WorkspaceStore
from corpcom.workspace_agent import ask_workspace


class FakeReply:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none=True):
        if self.tool_calls:
            return {
                "role": "assistant",
                "tool_calls": [
                    {"id": call.id, "type": "function", "function": {
                        "name": call.function.name, "arguments": call.function.arguments,
                    }} for call in self.tool_calls
                ],
            }
        return {"role": "assistant", "content": self.content}


def tool_call(name, key, value):
    return SimpleNamespace(
        id=f"call_{name}",
        function=SimpleNamespace(name=name, arguments=json.dumps({key: value})),
    )


class FakeClient:
    def __init__(self, first_thread_id, file_id):
        self.first_thread_id = first_thread_id
        self.file_id = file_id
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if len(self.calls) == 1:
            reply = FakeReply(tool_calls=[
                tool_call("read_thread", "thread_id", self.first_thread_id),
                tool_call("read_file", "artifact_id", self.file_id),
            ])
        else:
            results = [json.loads(item["content"]) for item in kwargs["messages"] if item["role"] == "tool"]
            assert results[0]["messages"][0]["content"] == "先核对现金流"
            assert results[1]["content"] == "# 原始计划\n检查现金流。\n"
            reply = FakeReply(content="已承接旧会话和研究计划，下一步核对现金流。")
        return SimpleNamespace(choices=[SimpleNamespace(message=reply)])


class WorkspaceAgentTests(unittest.TestCase):
    def test_same_thread_retains_both_turns_in_context_and_storage(self):
        requests = []
        first_question = "先分析什么指标？"
        first_answer = "先分析经营现金流，再核对净利润。"
        second_question = "按刚才的顺序，第二个指标是什么？"
        second_answer = "第二个指标是净利润。"
        replies = iter([first_answer, second_answer])

        def respond(**kwargs):
            # Capture each request before the agent appends the model's reply.
            requests.append(deepcopy(kwargs["messages"]))
            return SimpleNamespace(choices=[SimpleNamespace(message=FakeReply(content=next(replies)))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=respond)))
        with TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            with WorkspaceStore(data) as store:
                workspace = store.create_workspace(
                    "公司研究", "检验经营韧性", "单家公司", "三个月",
                    "现金流稳定", {"报告语言": "中文"},
                )
                thread = store.create_thread(workspace.id, "连续讨论")
                self.assertEqual(
                    ask_workspace(store, client, "test-model", workspace.id, thread.id, first_question),
                    first_answer,
                )
                self.assertEqual(
                    ask_workspace(store, client, "test-model", workspace.id, thread.id, second_question),
                    second_answer,
                )

            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0][0]["role"], "system")
            self.assertEqual(requests[1][0]["role"], "system")
            self.assertEqual(requests[0][1:], [{"role": "user", "content": first_question}])
            self.assertEqual(requests[1][1:], [
                {"role": "user", "content": first_question},
                {"role": "assistant", "content": first_answer},
                {"role": "user", "content": second_question},
            ])
            # Reopen storage to verify both exchanges were committed in order, without duplicates.
            with WorkspaceStore(data) as store:
                self.assertEqual(
                    [(item.role, item.content) for item in store.get_thread(workspace.id, thread.id).messages],
                    [("user", first_question), ("assistant", first_answer),
                     ("user", second_question), ("assistant", second_answer)],
                )

    def test_new_thread_reads_prior_material_and_saves_answer(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan.md"
            plan.write_text("# 原始计划\n检查现金流。\n", encoding="utf-8")
            with WorkspaceStore(root / "data") as store:
                workspace = store.create_workspace(
                    "公司研究", "检验经营韧性", "单家公司", "三个月",
                    "现金流稳定", {"报告语言": "中文"},
                )
                first = store.create_thread(workspace.id, "第一天")
                store.add_message(workspace.id, first.id, "user", "先核对现金流")
                file = store.import_file(workspace.id, plan, "plan")
                second = store.create_thread(workspace.id, "第二天")

            # Reopen storage to match a new process and ensure the model sees persisted state.
            with WorkspaceStore(root / "data") as store:
                client = FakeClient(first.id, file.id)
                answer = ask_workspace(store, client, "test-model", workspace.id, second.id, "继续昨天的研究")
                self.assertIn("旧会话和研究计划", answer)
                first_call = client.calls[0]
                context = first_call["messages"][0]["content"]
                self.assertIn("检验经营韧性", context)
                self.assertIn("现金流稳定", context)
                self.assertIn('"报告语言": "中文"', context)
                self.assertEqual(first_call["messages"][-1]["content"], "继续昨天的研究")
                self.assertEqual(
                    [(item.role, item.content) for item in store.get_thread(workspace.id, second.id).messages],
                    [("user", "继续昨天的研究"), ("assistant", answer)],
                )
                self.assertEqual(len(store.get_thread(workspace.id, first.id).messages), 1)


if __name__ == "__main__":
    unittest.main()

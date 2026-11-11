"""Recover incomplete tool exchanges using real graph checkpoints, without an API call."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver

from corpcom.run_coordinator import RunCoordinator
from corpcom.workspace import WorkspaceStore
from corpcom.workspace_agent import ask_workspace


class ProtocolCheckingModel(FakeMessagesListChatModel):
    """Enforce the provider's tool-result ordering before returning a scripted answer."""

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        pending = set()
        for message in messages:
            if message.type == "tool":
                pending.remove(message.tool_call_id)
            else:
                if pending:
                    raise ValueError("insufficient tool messages following tool_calls message")
                if message.type == "ai":
                    pending = {call["id"] for call in message.tool_calls}
        if pending:
            raise ValueError("insufficient tool messages following tool_calls message")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class WorkspaceRecoveryTests(unittest.TestCase):
    def test_recovery_preserves_all_results_when_only_pending_writes_exist(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint_path = str(root / "checkpoints.sqlite")
            effects = []

            @tool
            def action() -> str:
                """An already completed action must not be replayed during recovery."""
                effects.append("replayed")
                return "unexpected replay"

            with (
                WorkspaceStore(root) as store,
                SqliteSaver.from_conn_string(checkpoint_path) as saver,
                patch("corpcom.workspace_agent.create_workspace_tools", return_value=[action]),
            ):
                workspace = store.create_workspace("全部结果待提交", goal="保存 pending writes")
                thread = store.create_thread(workspace.id, "C")
                user, _ = store.add_user_message(workspace.id, thread.id, "执行两个工具")
                config = {"configurable": {"thread_id": f"{workspace.id}:{thread.id}"}}
                first = ProtocolCheckingModel(responses=[AIMessage(content="", tool_calls=[
                    {"name": "action", "args": {}, "id": "call-a"},
                    {"name": "action", "args": {}, "id": "call-b"},
                ])])
                agent = create_agent(first, tools=[action], checkpointer=saver, interrupt_before=["tools"])
                agent.invoke({"messages": [HumanMessage(id=f"business:{user.id}", content=user.content)]}, config)
                recorded = [
                    ToolMessage(id="result-a", content="A 的真实结果", tool_call_id="call-a"),
                    ToolMessage(id="result-b", content="B 的真实结果", tool_call_id="call-b"),
                ]
                snapshot = agent.get_state(config)
                saver.put_writes(snapshot.config, [("messages", recorded)], snapshot.tasks[0].id)
                checkpoint = saver.get_tuple(config)
                self.assertFalse(any(m.type == "tool" for m in checkpoint.checkpoint["channel_values"]["messages"]))
                self.assertTrue(any(channel == "messages" and value == recorded
                                    for _, channel, value in checkpoint.pending_writes))
                self.assertEqual([m for m in agent.get_state(config).values["messages"] if m.type == "tool"],
                                 recorded)
                store.start_thread_execution(workspace.id, thread.id)
                store.interrupt_running_executions()

                def check_before_ask(*args, **kwargs):
                    with SqliteSaver.from_conn_string(checkpoint_path) as reader:
                        saved = reader.get_tuple(config).checkpoint["channel_values"]["messages"]
                    # Equality checks IDs, contents and success statuses, with no unknown-result additions.
                    self.assertEqual([m for m in saved if m.type == "tool"], recorded)
                    return ask_workspace(*args, **kwargs)

                recovery = ProtocolCheckingModel(responses=[
                    AIMessage(content="恢复正常"), AIMessage(content="续聊正常"),
                ])
                coordinator = RunCoordinator(saver, max_concurrent_runs=2)
                with patch("corpcom.run_coordinator.ask_workspace", side_effect=check_before_ask):
                    coordinator.run(store, recovery, workspace.id, thread.id, "直接回复")
                coordinator.run(store, recovery, workspace.id, thread.id, "下一轮也不调用工具")
                saved = saver.get_tuple(config).checkpoint["channel_values"]["messages"]
                self.assertEqual([m for m in saved if m.type == "tool"], recorded)
                self.assertEqual(effects, [])
                detail = store.get_thread(workspace.id, thread.id)
                self.assertEqual(detail.execution_status, "succeeded")
                self.assertEqual([m.content for m in detail.messages if m.role == "assistant"],
                                 ["恢复正常", "续聊正常"])

    def test_recovery_preserves_pending_writes_in_a_partial_tool_batch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            effects = []

            @tool
            def action() -> str:
                """Record a side effect that must not be replayed during recovery."""
                effects.append("replayed")
                return "unexpected replay"

            tools = [action]
            with (
                WorkspaceStore(root) as store,
                SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver,
                patch("corpcom.workspace_agent.create_workspace_tools", return_value=tools),
            ):
                workspace = store.create_workspace("部分结果恢复", goal="保留已有工具结果")
                thread = store.create_thread(workspace.id, "C")
                user, _ = store.add_user_message(workspace.id, thread.id, "执行两个工具")
                config = {"configurable": {"thread_id": f"{workspace.id}:{thread.id}"}}
                first = ProtocolCheckingModel(responses=[AIMessage(content="", tool_calls=[
                    {"name": "action", "args": {}, "id": "recorded"},
                    {"name": "action", "args": {}, "id": "missing"},
                ])])
                agent = create_agent(first, tools=tools, checkpointer=saver, interrupt_before=["tools"])
                agent.invoke({"messages": [HumanMessage(id=f"business:{user.id}", content=user.content)]}, config)
                recorded = ToolMessage(id="saved-result", content="已完成的结果", tool_call_id="recorded")
                snapshot = agent.get_state(config)
                # The result exists only as a task write, not in the committed message channel.
                saver.put_writes(snapshot.config, [("messages", [recorded])], snapshot.tasks[0].id)
                checkpoint = saver.get_tuple(config)
                self.assertFalse(any(m.type == "tool" for m in checkpoint.checkpoint["channel_values"]["messages"]))
                self.assertTrue(any(channel == "messages" and recorded in value
                                    for _, channel, value in checkpoint.pending_writes))
                self.assertEqual([m for m in agent.get_state(config).values["messages"] if m.type == "tool"],
                                 [recorded])
                store.start_thread_execution(workspace.id, thread.id)
                store.interrupt_running_executions()

                recovery = ProtocolCheckingModel(responses=[AIMessage(content="恢复正常")])

                def check_before_ask(*args, **kwargs):
                    saved = saver.get_tuple(config).checkpoint["channel_values"]["messages"]
                    results = [m for m in saved if m.type == "tool"]
                    self.assertEqual([m.tool_call_id for m in results], ["recorded", "missing"])
                    self.assertEqual(results[0], recorded)
                    self.assertEqual(results[1].status, "error")
                    return ask_workspace(*args, **kwargs)

                coordinator = RunCoordinator(saver, max_concurrent_runs=2)
                with patch("corpcom.run_coordinator.ask_workspace", side_effect=check_before_ask):
                    coordinator.run(store, recovery, workspace.id, thread.id, "不再调用工具")
                saved = saver.get_tuple(config).checkpoint["channel_values"]["messages"]
                results = [m for m in saved if m.type == "tool"]
                self.assertEqual(len(results), 2)
                self.assertEqual(results[0], recorded)
                self.assertEqual(results[1].tool_call_id, "missing")
                self.assertEqual(results[1].status, "error")
                self.assertEqual(effects, [])
                coordinator.run(store, recovery, workspace.id, thread.id, "下一轮也不调用工具")
                saved = saver.get_tuple(config).checkpoint["channel_values"]["messages"]
                self.assertEqual([m for m in saved if m.type == "tool"], results)
                self.assertEqual(effects, [])

    def test_interrupted_exchange_is_persisted_before_ask_and_not_replayed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint_path = str(root / "checkpoints.sqlite")
            effects = []

            @tool
            def interrupted_action() -> str:
                """Perform an action whose result cannot be recorded before shutdown."""
                effects.append("performed")
                raise KeyboardInterrupt("backend stopped")

            tools = [interrupted_action]
            model = ProtocolCheckingModel(responses=[
                AIMessage(content="已有回答"),
                AIMessage(content="", tool_calls=[{
                    "name": "interrupted_action", "args": {}, "id": "lost-result",
                }]),
            ])
            with (
                WorkspaceStore(root) as store,
                SqliteSaver.from_conn_string(checkpoint_path) as saver,
                patch("corpcom.workspace_agent.create_workspace_tools", return_value=tools),
            ):
                workspace = store.create_workspace("恢复验收", goal="验证中断恢复")
                thread = store.create_thread(workspace.id, "C")
                coordinator = RunCoordinator(saver, max_concurrent_runs=2)
                coordinator.run(store, model, workspace.id, thread.id, "第一轮")
                with self.assertRaises(KeyboardInterrupt):
                    coordinator.run(store, model, workspace.id, thread.id, "执行工具")
                self.assertEqual(store.get_thread(workspace.id, thread.id).execution_status, "running")

            config = {"configurable": {"thread_id": f"{workspace.id}:{thread.id}"}}
            with (
                WorkspaceStore(root) as store,
                SqliteSaver.from_conn_string(checkpoint_path) as saver,
                patch("corpcom.workspace_agent.create_workspace_tools", return_value=tools),
            ):
                store.interrupt_running_executions()
                recovery = ProtocolCheckingModel(responses=[AIMessage(content="恢复正常")])

                def check_before_ask(*args, **kwargs):
                    detail = store.get_thread(workspace.id, thread.id)
                    self.assertEqual(detail.execution_status, "running")
                    self.assertEqual(detail.messages[-1].content, "执行工具")
                    # A separate connection proves recovery is committed before ask starts.
                    with SqliteSaver.from_conn_string(checkpoint_path) as reader:
                        saved = reader.get_tuple(config).checkpoint["channel_values"]["messages"]
                    self.assertEqual(saved[-1].tool_call_id, "lost-result")
                    self.assertEqual(saved[-1].status, "error")
                    self.assertIn("未知", saved[-1].content)
                    return ask_workspace(*args, **kwargs)

                coordinator = RunCoordinator(saver, max_concurrent_runs=2)
                with patch("corpcom.run_coordinator.ask_workspace", side_effect=check_before_ask):
                    answer = coordinator.run(store, recovery, workspace.id, thread.id, "直接回复恢复正常")
                self.assertEqual(answer.content, "恢复正常")
                self.assertEqual(effects, ["performed"])
                detail = store.get_thread(workspace.id, thread.id)
                self.assertEqual(detail.execution_status, "succeeded")
                self.assertIsNone(detail.execution_error)
                self.assertEqual([m.content for m in detail.messages if m.role == "assistant"],
                                 ["已有回答", "恢复正常"])

                with patch("corpcom.run_coordinator.recover_interrupted_checkpoint") as recover:
                    coordinator.run(store, recovery, workspace.id, thread.id, "再确认一次")
                    recover.assert_not_called()
                saved = saver.get_tuple(config).checkpoint["channel_values"]["messages"]
                self.assertEqual(sum(m.type == "tool" and m.tool_call_id == "lost-result" for m in saved), 1)
                self.assertEqual(effects, ["performed"])

    def test_non_interruption_tool_failure_is_not_repaired(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)

            @tool
            def broken_action() -> str:
                """Raise an ordinary tool exception, not a backend interruption."""
                raise RuntimeError("ordinary tool failure")

            model = ProtocolCheckingModel(responses=[AIMessage(content="", tool_calls=[{
                "name": "broken_action", "args": {}, "id": "failed-tool",
            }])])
            with (
                WorkspaceStore(root) as store,
                SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver,
                patch("corpcom.workspace_agent.create_workspace_tools", return_value=[broken_action]),
                patch("corpcom.run_coordinator.recover_interrupted_checkpoint") as recover,
            ):
                workspace = store.create_workspace("异常不可掩盖", goal="验证工具错误")
                thread = store.create_thread(workspace.id, "C")
                coordinator = RunCoordinator(saver, max_concurrent_runs=2)
                with self.assertRaisesRegex(RuntimeError, "ordinary tool failure"):
                    coordinator.run(store, model, workspace.id, thread.id, "调用工具")
                retry = ProtocolCheckingModel(responses=[AIMessage(content="不应生成")])
                with self.assertRaisesRegex(ValueError, "insufficient tool messages"):
                    coordinator.run(store, retry, workspace.id, thread.id, "尝试继续")
                recover.assert_not_called()
                detail = store.get_thread(workspace.id, thread.id)
                self.assertEqual(detail.execution_status, "failed")
                self.assertIn("insufficient tool messages", detail.execution_error)
                self.assertEqual([m.role for m in detail.messages], ["user", "user"])

    def test_recovery_failure_skips_ask_and_releases_capacity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                WorkspaceStore(root) as store,
                SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver,
            ):
                workspace = store.create_workspace("恢复失败", goal="验证额度释放")
                thread = store.create_thread(workspace.id, "C")
                store.start_thread_execution(workspace.id, thread.id)
                store.interrupt_running_executions()
                coordinator = RunCoordinator(saver, max_concurrent_runs=1)
                model = ProtocolCheckingModel(responses=[AIMessage(content="正常回答")])
                with (
                    patch("corpcom.run_coordinator.recover_interrupted_checkpoint",
                          side_effect=OSError("checkpoint unavailable")),
                    patch("corpcom.run_coordinator.ask_workspace") as ask,
                ):
                    with self.assertRaisesRegex(OSError, "checkpoint unavailable"):
                        coordinator.run(store, model, workspace.id, thread.id, "恢复")
                    ask.assert_not_called()
                detail = store.get_thread(workspace.id, thread.id)
                self.assertEqual(detail.execution_status, "failed")
                self.assertEqual(detail.execution_error, "checkpoint unavailable")
                self.assertEqual(detail.messages, [])
                other = store.create_thread(workspace.id, "可执行的新会话")
                self.assertEqual(coordinator.run(store, model, workspace.id, other.id, "继续").content,
                                 "正常回答")

    def test_interruption_before_first_checkpoint_allows_a_new_question(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                WorkspaceStore(root) as store,
                SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver,
            ):
                workspace = store.create_workspace("早期中断", goal="checkpoint 尚未创建")
                thread = store.create_thread(workspace.id, "C")
                store.start_thread_execution(workspace.id, thread.id)
                store.interrupt_running_executions()
                model = ProtocolCheckingModel(responses=[AIMessage(content="恢复正常")])
                answer = RunCoordinator(saver, max_concurrent_runs=2).run(
                    store, model, workspace.id, thread.id, "继续",
                )
                self.assertEqual(answer.content, "恢复正常")

"""Verify Agent → Bash → Docker with a scripted model and temporary storage.

Run from the project root: .venv/bin/python examples/probe_agent_bash.py
Requires Docker and the corpcom-sandbox:local image; no model API is called.
"""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import Field

from corpcom.sandbox import ensure_workspace_container
from corpcom.workspace import WorkspaceStore
from corpcom.workspace_agent import ask_workspace


class ScriptedModel(FakeMessagesListChatModel):
    """Script tool calls while keeping framework routing and Docker execution real."""

    requests: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append(deepcopy(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


with TemporaryDirectory(prefix="corpcom-agent-bash-") as temporary:
    root = Path(temporary).resolve()
    data = root / "data"
    source = root / "probe-note.txt"
    content = "Bash 集成验证：下一步核对现金流。\n"
    source.write_text(content, encoding="utf-8")

    with WorkspaceStore(data) as store:
        workspace = store.create_workspace("Agent Bash 验证", goal="验证文件发现与读取")
        thread = store.create_thread(workspace.id, "测试会话")
        artifact = store.import_file(workspace.id, source, "note")

    question = "请找到上传的笔记，读取后告诉我下一步做什么。"
    expected_answer = "笔记中的下一步是核对现金流。"
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{
            "id": "list_files", "name": "execute_bash",
            "args": {"command": "find /workspace/files -type f"},
        }]),
        AIMessage(content="", tool_calls=[{
            "id": "read_note", "name": "execute_bash",
            "args": {"command": "cat /workspace/files/*/probe-note.txt"},
        }]),
        AIMessage(content=expected_answer),
    ])
    workspace_dir = data / "workspaces" / workspace.id
    checkpoint_path = str(data / "checkpoints.sqlite")
    config = {"configurable": {"thread_id": f"{workspace.id}:{thread.id}"}}
    container_id = ensure_workspace_container(workspace.id, workspace_dir)
    try:
        with (
            WorkspaceStore(data) as store,
            SqliteSaver.from_conn_string(checkpoint_path) as checkpointer,
        ):
            answer = ask_workspace(
                store, model, workspace.id, thread.id, question, checkpointer,
            )
        assert answer == expected_answer, answer
        assert len(model.requests) == 3, len(model.requests)

        # 工具结果必须实际回传给下一次模型调用；固定回答本身不是读取成功的证据。
        listed = next(m for m in model.requests[1] if m.type == "tool")
        assert listed.tool_call_id == "list_files" and listed.status == "success"
        expected_listing = {
            "exit_code": 0,
            "stdout": f"/workspace/files/{artifact.id}/{source.name}\n",
            "stderr": "",
        }
        assert json.loads(listed.content) == expected_listing, listed.content
        read = next(m for m in model.requests[2] if m.type == "tool" and m.tool_call_id == "read_note")
        assert read.status == "success", read
        assert json.loads(read.content) == {
            "exit_code": 0, "stdout": content, "stderr": "",
        }, read.content
        for request in model.requests:
            prompt = request[0].content
            state = json.loads(prompt.split("当前 Workspace 的已保存研究状态：\n", 1)[1])
            assert "files" not in state, state
            assert artifact.id not in prompt, prompt

        # 重新打开存储，检查业务问答和框架工具消息均已落盘。
        with WorkspaceStore(data) as store:
            messages = store.get_thread(workspace.id, thread.id).messages
            assert [(m.role, m.content) for m in messages] == [
                ("user", question), ("assistant", expected_answer),
            ], messages
        with SqliteSaver.from_conn_string(checkpoint_path) as checkpointer:
            saved = checkpointer.get_tuple(config).checkpoint["channel_values"]["messages"]
            assert [m.type for m in saved] == ["human", "ai", "tool", "ai", "tool", "ai"]
            assert [m for m in saved if m.type == "tool"] == [listed, read]
            assert saved[-1].content == expected_answer

        assert ensure_workspace_container(workspace.id, workspace_dir) == container_id
        print("Agent → Bash → Docker 验证通过：文件列表、内容回传、消息落盘及容器复用正常")
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            check=True, capture_output=True, text=True, timeout=10,
        )
        print("测试容器已清理")

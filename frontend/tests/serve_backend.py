"""Serve disposable UI acceptance data with a scripted Agent, never a real model.

Run from the repository root with .venv/bin/python frontend/tests/serve_backend.py.
Point the frontend's CORPCOM_API_URL at http://127.0.0.1:8012.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from unittest.mock import patch
from uuid import uuid4

import httpx
import uvicorn
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from openai import APIConnectionError

from corpcom.api import create_app, get_model


def scripted_research(store, model, workspace_id, thread_id, question, checkpointer, *, on_event=None):
    """Exercise HTTP, persistence and file archival while replacing Agent execution."""
    store.add_user_message(workspace_id, thread_id, question)
    plan = AIMessage(id=uuid4().hex, content="正在核对本次研究资料。")
    tool_id = uuid4().hex
    if on_event is not None:
        on_event({"type": "text_delta", "message_id": plan.id, "text": plan.text})
    store.add_message(workspace_id, thread_id, "assistant", plan.text, model_message_id=plan.id)
    if on_event is not None:
        on_event({"type": "tool_start", "tool_run_id": tool_id, "name": "整理研究资料"})
    sleep(15 if "慢速" in question else 1)
    if "模拟失败" in question:
        if on_event is not None:
            on_event({"type": "tool_error", "tool_run_id": tool_id, "error": "脚本化工具失败"})
        raise APIConnectionError(request=httpx.Request("POST", "http://model.invalid"))

    if "v1" in question or "v2" in question:
        version = "v2" if "v2" in question else "v1"
        previous = store.get_workspace_detail(workspace_id).artifacts
        run = store.root / "workspaces" / workspace_id / ".runs" / uuid4().hex
        run.mkdir(parents=True)
        text = f"# 前端验收报告 {version}\n\n仅用于界面验收的合成数据，不构成真实研究。\n\n"
        if version == "v2":
            text += f"承接 v1 文件：{previous[0].id}。新增 2025 年样本，维持判断，旧文件保持不变。\n\n"
        text += "| 年份 | 样本比率 |\n| --- | --- |\n| 2024 | 1.1 |\n| 2025 | 1.2 |\n"
        (run / "report.md").write_text(text, encoding="utf-8")
        (run / "data.json").write_text('{"fixture": true, "value": 1.2}', encoding="utf-8")
        (run / "analysis.py").write_text('print("UI acceptance fixture")\n', encoding="utf-8")
        artifacts = store.archive_run_files(
            workspace_id, run,
            [(Path("report.md"), "report"), (Path("data.json"), "data"), (Path("analysis.py"), "code")],
            archive_path=Path(f"ui-acceptance/{version}"),
        )
        answer = f"已保存 **{version}** 验收报告、数据和代码。报告 ID：`{artifacts[0].id}`。"
    else:
        answer = "已读取当前研究。可以在新会话继续查看已有资料。（脚本化验收回答）"
    final_message = AIMessage(id=uuid4().hex, content=answer)
    if on_event is not None:
        on_event({"type": "tool_end", "tool_run_id": tool_id})
        for offset in range(0, len(answer), 4):
            on_event({"type": "text_delta", "message_id": final_message.id, "text": answer[offset:offset + 4]})
            sleep(0.12)
    return final_message


if __name__ == "__main__":
    with TemporaryDirectory(prefix="corpcom-frontend-qa-") as directory:
        app = create_app(Path(directory))
        app.dependency_overrides[get_model] = lambda: FakeMessagesListChatModel(
            responses=[AIMessage(content="unused")]
        )
        with patch("corpcom.run_coordinator.ask_workspace", side_effect=scripted_research):
            uvicorn.run(app, host="127.0.0.1", port=8012)

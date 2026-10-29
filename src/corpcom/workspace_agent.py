"""Continue one research conversation using its persistent workspace context."""

import json
import logging

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain.agents.middleware import ModelCallLimitMiddleware, wrap_tool_call
from langchain_core.messages import ToolMessage
from langchain_core.callbacks import BaseCallbackHandler

from corpcom.workspace import RecordNotFoundError, WorkspaceStore
from corpcom.workspace_tools import create_workspace_tools
from corpcom.function_catalog import list_function_summaries

logger = logging.getLogger(__name__)


class ToolLogHandler(BaseCallbackHandler):
  def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
    logger.info(
        "工具开始 name=%s run_id=%s input=%s",
        serialized["name"],
        run_id,
        input_str,
    )

  def on_tool_end(self, output, *, run_id, **kwargs):
    logger.info(
        "工具返回 name=%s run_id=%s output=%s",
        kwargs["name"],
        run_id,
        output,
    )

  def on_tool_error(self, error, *, run_id, **kwargs):
    logger.error("工具异常 run_id=%s error=%s", run_id, error)

@wrap_tool_call
def handle_workspace_tool_error(request, handler):
    """Return expected lookup failures as tool results the model can act on."""
    try:
        return handler(request)
    except RecordNotFoundError:
        return ToolMessage(
            content=(
                "当前 Workspace 中找不到指定资料。"
                "请检查资料索引中的 ID，再决定是否重新调用工具。"
            ),
            tool_call_id=request.tool_call["id"],
            name=request.tool_call["name"],
            status="error",
        )


def ask_workspace(
    store: WorkspaceStore, model: BaseChatModel,
    workspace_id: str, thread_id: str, question: str,
    checkpointer: BaseCheckpointSaver,
) -> str:
    """Answer in a thread, reading workspace material on demand and saving the exchange."""
    logger.info(
        "本轮开始 workspace=%s thread=%s",
        workspace_id,
        thread_id,
    )
    detail = store.get_workspace_detail(workspace_id)
    # Initialize once per workspace, including history in sibling conversations.
    initialize_research = (
        not any(
            value.strip() if isinstance(value, str) else value
            for value in detail.info.model_dump().values()
        )
        and not store.has_user_messages(workspace_id)
    )
    user_message = store.add_message(workspace_id, thread_id, "user", question)

    # Refresh workspace metadata and material indexes on every turn.
    context = {
        "title": detail.title,
        **detail.info.model_dump(),
        "other_threads": [
            {"id": item.id, "title": item.title}
            for item in detail.threads if item.id != thread_id
        ],
        "files": [
            {
                "id": item.id,
                "name": item.name,
                "kind": item.kind,
                "relative_path": item.relative_path,
            }
            for item in detail.artifacts
        ],
    }
    research_instruction = (
        "这是整个 Workspace 的第一条用户消息，且研究信息全部为空。"
        "请先根据本条用户消息初始化研究信息：整理具体的研究目标和范围，"
        "从研究问题形成可验证的初始假设，并注明这是待验证假设。"
        "观察周期和用户偏好仅从消息中提取有依据的内容，未提供时保持为空。"
        "先调用 update_research 保存这些信息，再继续处理用户的研究请求。"
        "如果消息只有问候等内容、没有研究主题，不编造研究信息，先询问研究目标。"
        if initialize_research else
        "用户明确要求修改或清空研究信息时，调用 update_research，仅提交要修改的字段。"
        "未填写的信息保持为空，不自行补全；分析结论不自动改写用户的初始假设。"
    )
    agent = create_agent(
        model,
        tools=create_workspace_tools(store.root, workspace_id),
        system_prompt=(
            "你是持续研究助手。回答时遵循当前 Workspace 的目标与用户偏好，"
            "将初始假设视为待验证判断。"
            "目标、初始假设、范围、观察周期和偏好保存在工作区的 research.json 中。"
            + research_instruction
            + "\n"
            "工具成功后才能说明已保存，本轮后续回答以工具返回的新状态为准。"
            "当问题需要旧会话或文件内容时，按索引调用工具读取；不要凭索引猜测内容。"
            "已有研究资料的读取限定在当前 Workspace。"
            "区分已有证据、用户陈述与推断，不编造来源、数据或投资建议。\n"
            "你可以通过 execute_python 在 Docker 沙箱中运行 Python。"
            "需要使用数据函数时，先根据下方简表判断本次任务需要哪些函数，"
            "仅对准备调用的函数使用 read_function_doc 获取完整契约；"
            "不要为了了解全部能力而读取所有函数文档。"
            "当前上下文已包含该函数完整契约时可以复用。"
            "纯概念问题无需读取函数文档或执行沙箱。"
            "当前函数不覆盖的数据需求应明确说明，不编造函数或参数。\n"
            "每次沙箱执行使用独立容器和运行目录，"
            "不能依赖上一次执行的 Python 变量或当前目录文件。"
            "完成本次分析所需的取数和计算应放在同一次执行中。"
            "保存本次研究所需的完整数据，并按 execute_python 的契约提交结果与待登记文件。"
            "回答所需的计算结果应附带来源摘要。"
            "仅 print() 不会完成执行契约。"
            "不要将导致本次任务失败的异常转换成 result.json 后正常退出；"
            "如需捕获异常记录诊断信息，记录后应重新抛出。"
            "执行失败时应说明失败，不能声称已经取得数据或完成计算。"
            "只有工具确认登记成功，才能声称文件已保存到 Workspace 资料索引。"
            "不能把输出文件名当作 artifact_id 传给 read_file。\n"
            "可用沙箱数据函数：\n"
            + json.dumps(list_function_summaries(), ensure_ascii=False)
            + "\n当前 Workspace 的已保存研究状态：\n"
            + json.dumps(context, ensure_ascii=False)
        ),
        middleware=[
            ModelCallLimitMiddleware(run_limit=10, exit_behavior="error"),
            handle_workspace_tool_error,
        ],
        checkpointer=checkpointer,
    )

    config = {
        "configurable": {
            "thread_id": f"{workspace_id}:{thread_id}",
        },
      "callbacks": [ToolLogHandler()],
    }

    # Import existing conversation history only before its first checkpoint.
    if checkpointer.get_tuple(config) is None:
        messages = store.get_thread(workspace_id, thread_id).messages
    else:
        messages = [user_message]
    result = agent.invoke({
        "messages": [
            {
                "id": f"business:{item.id}",
                "role":item.role,
                "content":item.content,
            } for item in messages
        ],

    }, config,)
    answer = result["messages"][-1].text
    if not answer.strip():
        raise RuntimeError("model returned no answer")
    store.add_message(workspace_id, thread_id, "assistant", answer)
    logger.info(
        "本轮完成 workspace=%s thread=%s",
        workspace_id,
        thread_id,
    )
    return answer

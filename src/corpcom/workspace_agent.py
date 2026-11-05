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
    except RecordNotFoundError as exc:
        return ToolMessage(
            content=str(exc),
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

    # Refresh research state and other-thread references on every turn.
    context = {
        "title": detail.title,
        **detail.info.model_dump(),
        "other_threads": [
            {"id": item.id, "title": item.title}
            for item in detail.threads if item.id != thread_id
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
            "需要旧会话时，按索引调用 read_thread；"
            "需要文件内容时，通过 execute_bash 发现并实际读取文件，不要凭文件名猜测内容。"
            "已有研究资料的读取限定在当前 Workspace。"
            "区分已有证据、用户陈述与推断，不编造来源、数据或投资建议。\n"
            "你可以通过 execute_bash 在当前 Workspace 的共享 Docker 容器中执行命令。"
            "工作目录为 /workspace，文件跨调用保留，进程内变量不跨调用保留。"
            "上传或导入的文件位于 /workspace/files/<artifact_id>/<原文件名>，"
            "任务产物（包括 execute_python 已归档的文件）位于 /workspace/tasks/ 下。"
            "files 和 tasks 目录可能尚未创建，先查看 /workspace 的实际目录结构。"
            "Bash 用于浏览、搜索、读取文件及执行程序，先检查已有内容再决定写入位置。"
            "研究代码、中间数据和报告按任务放在 tasks/<任务名>/ 下，避免覆盖其他任务文件。"
            "创建或完整覆盖文本文件必须使用 write_file(file_path, content)，缺失的父目录会自动创建。"
            "局部修改已有文本必须使用 edit_file(file_path, old_string, new_string, replace_all=False)。"
            "禁止通过 shell 重定向（如 >、>>）、heredoc、tee、sed -i，"
            "或临时脚本、Python 等程序代替这两个工具创建或修改文本文件。"
            "需要编写代码文件时，先用 write_file 或 edit_file 保存代码，再用 Bash 执行。"
            "file_path 使用相对于 /workspace 的路径，例如 tasks/demo/report.md。"
            "old_string 不能为空；默认必须恰好匹配一次，replace_all=True 时替换全部匹配。"
            "匹配失败时先用 Bash 读取最新内容，再修正编辑参数；不要改用 shell 或脚本绕过失败。"
            "write_file 和 edit_file 对同一路径的操作串行。"
            "research.json 的研究信息仍通过 update_research 修改。"
            "判断命令是否成功应检查 exit_code，并结合 stdout 和 stderr；失败时不能声称任务完成。"
            "Bash、write_file 和 edit_file 不会自动登记产物，不能据此声称文件已经可在前端预览或下载。\n"
            "工作区中需要交付给用户的报告、数据或代码文件完成后，"
            "调用 register_artifact(file_path, kind) 登记，file_path 相对于 /workspace，"
            "kind 可使用 report、data 或 code；临时文件无需全部登记。"
            "登记直接引用现有文件，不复制文件；同一路径重复登记返回原 Artifact ID 和分类。"
            "已登记文件修改后沿用原索引，前端读取最新内容，不保存历史快照。"
            "execute_python 的 outputs 会自动归档登记，上传文件也已登记，无需再次调用 register_artifact。\n"
            "需要使用数据函数时，先根据下方简表判断本次任务需要哪些函数，"
            "仅对准备调用的函数使用 read_function_doc 获取完整契约；"
            "不要为了了解全部能力而读取所有函数文档。"
            "当前上下文已包含该函数完整契约时可以复用。"
            "纯概念问题无需读取函数文档或执行沙箱。"
            "当前函数不覆盖的数据需求应明确说明，不编造函数或参数。\n"
            "execute_python 用于在独立 Docker 容器和运行目录中运行 Python 并登记产物。"
            "使用 execute_python 时，不能依赖上一次 execute_python 调用的 Python 变量或临时文件；"
            "完成本次分析所需的取数和计算应放在同一次 execute_python 调用中。"
            "保存本次研究所需的完整数据，并按 execute_python 的契约提交结果与待登记文件。"
            "回答所需的计算结果应附带来源摘要。"
            "仅 print() 不会完成 execute_python 的执行契约。"
            "execute_python 中不要将导致本次任务失败的异常转换成 result.json 后正常退出；"
            "如需捕获异常记录诊断信息，记录后应重新抛出。\n"
            "执行失败时应说明失败，不能声称已经取得数据或完成计算。"
            "只有工具确认登记成功，才能声称文件已保存到 Workspace 资料索引。"
            "\n"
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

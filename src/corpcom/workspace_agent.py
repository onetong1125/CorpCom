"""Continue one research conversation using its persistent workspace context."""

import json

from openai import OpenAI

from corpcom.workspace import WorkspaceStore


READ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_thread",
            "description": "Read messages from an earlier conversation in this workspace.",
            "parameters": {
                "type": "object",
                "properties": {"thread_id": {"type": "string"}},
                "required": ["thread_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the UTF-8 text of a saved file in this workspace. Use its artifact ID.",
            "parameters": {
                "type": "object",
                "properties": {"artifact_id": {"type": "string"}},
                "required": ["artifact_id"],
            },
        },
    },
]


def ask_workspace(
    store: WorkspaceStore, client: OpenAI, model: str,
    workspace_id: str, thread_id: str, question: str,
) -> str:
    """Answer in a thread, reading workspace material on demand and saving the exchange."""
    detail = store.get_workspace_detail(workspace_id)
    store.add_message(workspace_id, thread_id, "user", question)
    thread = store.get_thread(workspace_id, thread_id)

    # Only the active conversation is loaded in full. Other material is indexed for tool reads.
    context = {
        "title": detail.title,
        "goal": detail.goal,
        "scope": detail.scope,
        "observation_period": detail.observation_period,
        "initial_hypothesis": detail.initial_hypothesis,
        "preferences": detail.preferences,
        "other_threads": [
            {"id": item.id, "title": item.title}
            for item in detail.threads if item.id != thread_id
        ],
        "files": [
            {"id": item.id, "name": item.name, "kind": item.kind}
            for item in detail.artifacts
        ],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是持续研究助手。以下 JSON 是当前 Workspace 的已保存研究状态。"
                "回答时遵循其目标与用户偏好，将初始假设视为待验证判断。"
                "当问题需要旧会话或文件内容时，按索引调用工具读取；不要凭索引猜测内容。"
                "区分已有证据、用户陈述与推断，不编造来源、数据或投资建议。"
                "工具只能读取当前 Workspace 的资料。\n"
                + json.dumps(context, ensure_ascii=False)
            ),
        },
        *[{"role": item.role, "content": item.content} for item in thread.messages],
    ]

    for _ in range(6):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=READ_TOOLS, temperature=0,
        )
        reply = response.choices[0].message
        messages.append(reply.model_dump(exclude_none=True))
        if not reply.tool_calls:
            if not reply.content:
                raise RuntimeError("model returned no answer")
            store.add_message(workspace_id, thread_id, "assistant", reply.content)
            return reply.content

        for call in reply.tool_calls:
            arguments = json.loads(call.function.arguments)
            if call.function.name == "read_thread":
                history = store.get_thread(workspace_id, arguments["thread_id"])
                result = {
                    "title": history.title,
                    "messages": [
                        {"role": item.role, "content": item.content}
                        for item in history.messages
                    ],
                }
            elif call.function.name == "read_file":
                file = store.get_file(workspace_id, arguments["artifact_id"])
                result = {"name": file.name, "content": file.path.read_text(encoding="utf-8")}
            else:
                raise ValueError(f"unknown tool: {call.function.name}")
            messages.append({
                "role": "tool", "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    raise RuntimeError("model did not finish after six rounds")

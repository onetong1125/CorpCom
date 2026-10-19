# 本地后端接口

后端通过 FastAPI 暴露 Workspace、会话、文件和 Agent 对话，前端直接发送 HTTP 请求。研究状态与问答由 SQLite 和文件目录保存，直接调用 `ask_workspace`。存储结构、Agent 执行流程与自动验收见 [本地存储说明](workspace-local.md)。

## 启动

在项目根目录执行：

```bash
uv sync
uv run uvicorn corpcom.api:create_app --factory --host 127.0.0.1 --port 8000
```

- 交互接口文档：<http://127.0.0.1:8000/docs>
- OpenAPI 定义：<http://127.0.0.1:8000/openapi.json>
- 服务健康检查：`GET /health`，返回 `{"status":"ok"}`。

沿用项目 `.env` 的 `CORPCOM_MODEL`、`OPENAI_API_KEY` 和可选 `OPENAI_BASE_URL`。配置仅供后端使用。未配置模型时，研究、会话和文件接口仍可用，发送消息返回 `503`。

默认数据目录为启动目录下的 `data/`，与现有 Workspace 数据兼容。可通过 `CORPCOM_DATA_DIR` 指定绝对路径。默认允许前端来源 `http://localhost:5173` 和 `http://127.0.0.1:5173`，其他端口通过逗号分隔的 `CORPCOM_CORS_ORIGINS` 配置。

当前服务面向单用户本机开发，使用一个 Uvicorn worker。同一 Workspace 同时只接受一次 Agent 对话请求；占用状态保存在进程内，多 worker 部署需要另行实现跨进程协调。

## 接口清单

下表 `{wid}`、`{tid}`、`{aid}` 分别表示 Workspace、thread 和文件 ID。

| 方法 | 路径 | 请求与返回 |
| --- | --- | --- |
| POST | `/api/workspaces` | 创建研究，返回 `Workspace`，状态码 201 |
| GET | `/api/workspaces` | 返回 `WorkspaceSummary[]` |
| GET | `/api/workspaces/{wid}` | 返回 `WorkspaceDetail`，包含会话和文件索引 |
| PUT | `/api/workspaces/{wid}/preferences` | `{"preferences": {...}}` 整体替换偏好，返回 `Workspace` |
| POST | `/api/workspaces/{wid}/threads` | `{"title":"首次讨论"}` 创建会话，返回 `Thread`，状态码 201 |
| GET | `/api/workspaces/{wid}/threads/{tid}` | 返回 `ThreadDetail`，包含按写入顺序排列的消息 |
| POST | `/api/workspaces/{wid}/threads/{tid}/messages` | `{"content":"继续研究"}` 调用 Agent，返回已保存的助手 `Message`，状态码 201 |
| POST | `/api/workspaces/{wid}/files` | multipart 的 `file` 与可选 `kind` 字段，返回 `Artifact`，状态码 201 |
| GET | `/api/workspaces/{wid}/files/{aid}` | 返回 `Artifact` 文件元信息 |
| GET | `/api/workspaces/{wid}/files/{aid}/content` | 下载已保存文件的内容 |

消息接口接收本轮用户文本。当前 thread 尚无 checkpoint 时，后端从业务库导入完整问答历史；已有 checkpoint 时，由框架恢复历史，仅输入本轮用户消息。模型成功回答后，后端将最终回答保存到业务库。前端无需传入历史、角色或模型配置。接口一次返回完整回答，尚未接入流式输出。

`corpcom.db` 保存研究资料及用户、最终助手消息；同一数据目录下的 `checkpoints.sqlite` 保存 Agent 消息状态，包括工具调用和结果。服务通过 lifespan 管理 checkpoint 连接，使用 `workspace_id:thread_id` 标识执行历史。

## 从创建研究到连续对话

创建研究：

```bash
curl -X POST http://127.0.0.1:8000/api/workspaces \
  -H 'Content-Type: application/json' \
  -d '{"title":"经营韧性研究","goal":"持续检验经营韧性","initial_hypothesis":"现金流能够持续覆盖经营投入","scope":"单家公司跟踪","preferences":{"报告语言":"中文"}}'
```

`title`、`goal`、`initial_hypothesis` 必填且不能全为空白；`scope`、`observation_period` 默认空字符串，`preferences` 默认空对象。返回结果里的 `id` 用作 Workspace ID。

```bash
workspace_id="替换为 Workspace ID"
curl -X POST "http://127.0.0.1:8000/api/workspaces/$workspace_id/threads" \
  -H 'Content-Type: application/json' -d '{"title":"首次讨论"}'

thread_id="替换为 thread ID"
curl -X POST "http://127.0.0.1:8000/api/workspaces/$workspace_id/threads/$thread_id/messages" \
  -H 'Content-Type: application/json' -d '{"content":"根据研究目标，列出需要核对的问题。"}'
```

发送消息的成功响应是已保存的助手消息，字段为 `id`、`thread_id`、`role`、`content`、`created_at`。再次向同一地址发送新文本即可追问；重新获取 thread 详情可以恢复完整问答。

前端调用示例：

```javascript
const response = await fetch(
  `http://127.0.0.1:8000/api/workspaces/${workspaceId}/threads/${threadId}/messages`,
  {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: question }),
  },
);
const payload = await response.json();
if (!response.ok) throw new Error(JSON.stringify(payload.detail));
console.log(payload.content);
```

## 文件上传与下载

```bash
curl -X POST "http://127.0.0.1:8000/api/workspaces/$workspace_id/files" \
  -F 'file=@docs/research-workspace.md' -F 'kind=plan'
```

上传后生成独立副本；同名文件再次上传得到新的文件 ID。接口接收文件内容，文件名必须是普通文件名，不能包含目录。文件响应不返回服务器绝对路径，通过 `/content` 下载。文件内容可以是二进制，但当前 Agent 的 `read_file` 仅支持 UTF-8 文本。

## 错误与执行行为

| 状态码 | 含义 |
| --- | --- |
| 404 | 研究、会话或文件不存在，或不属于请求中的 Workspace |
| 409 | 同一 Workspace 已有 Agent 请求执行，本次提问尚未保存 |
| 422 | 请求字段不合法；`detail` 列出字段位置、原因和错误类型 |
| 502 | 模型接口失败或达到六次调用上限；已保存用户提问，没有保存助手回答 |
| 503 | 后端未配置模型，本次提问尚未保存 |

HTTP 错误返回 `{"detail": ...}`；未处理的程序错误由服务器返回 500 并记录异常。请求失败后可读取 thread 查看已保存消息；当前没有请求去重和中断恢复，重新发送会新增一条用户消息。

同步的 SQLite 和模型调用在请求工作线程内执行，模型等待不会占用 HTTP 事件循环。每个路由在执行线程内创建和关闭自己的数据库连接；Agent 工具也各自建立连接。

Agent 工具遇到 `RecordNotFoundError` 时，中间件返回匹配调用 ID、`status="error"` 的 ToolMessage，让模型继续处理；错误文本不透露其他 Workspace 的资料。其他未处理异常仍向外抛出。两个数据库之间没有原子事务，当前不保证执行中断后的自动恢复或双库崩溃一致性。

## 已验证

2026-10-10 全部 18 项自动测试通过，包含 HTTP 创建与读取、偏好替换、文件上传下载、问答保存、重新创建应用后的恢复、Workspace 隔离、非法输入、模型错误、并发请求拒绝以及浏览器 CORS 预检。

同日通过临时 Uvicorn 服务和现有真实模型接口验收：新会话读取旧会话与上传文件的随机标记，同会话第二轮准确承接上一轮信息；停止再启动服务后，四条问答消息和文件内容均能恢复。验收使用临时数据目录，测试服务已停止。

2026-10-19 全部 22 项自动测试通过。另使用临时数据目录完成真实模型验收：调用不存在文件的 read_file 后，匹配的失败 ToolMessage 和最终回答写入 checkpoint；停止并重新启动服务后，同一 thread 能准确回忆上一轮标记、工具参数与失败结果，未再次调用工具。磁盘核对得到六条 Agent 消息和四条业务问答，无重复历史。

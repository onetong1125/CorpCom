# 本地后端接口

后端通过 FastAPI 暴露 Workspace、会话、文件和 Agent 对话，前端直接发送 HTTP 请求。研究状态与问答由 SQLite 和文件目录保存，由 `RunCoordinator` 管理准入、Agent 执行和终态保存。存储结构、Agent 执行流程与自动验收见 [本地存储说明](workspace-local.md)。

## 服务配置

按[快速开始](../README.md#快速开始)启动服务后，可查看 [Swagger UI](http://127.0.0.1:8001/docs) 或 [OpenAPI 定义](http://127.0.0.1:8001/openapi.json)。健康检查 `GET /health` 返回 `{"status":"ok"}`。

开发服务使用一个 Uvicorn worker。同一 Thread 同时只接受一轮研究，整个进程最多执行两轮，额度用完时立即返回 503。未配置模型时仍可操作研究空间、会话和文件，发送消息返回 503。

Vite 开发服务代理 `/api`。浏览器直接跨域访问后端时，默认允许 `http://localhost:5173` 和 `http://127.0.0.1:5173`，其他来源通过逗号分隔的 `CORPCOM_CORS_ORIGINS` 配置。

每轮研究根据最新档案、工作笔记、两级记忆索引及其他会话索引渲染系统提示词。角色默认值和提示词正文分别位于 [prompts.yaml](../src/corpcom/agent/template/prompts.yaml) 和 [system.md.j2](../src/corpcom/agent/template/system.md.j2)。

可选变量 `CORPCOM_MODEL_INPUT_TOKEN_BUDGET` 接受正整数。应按模型上下文窗口扣除回答所需空间后设置。系统提示词、工具声明和对话消息一起估算 token 数。未设置时使用完整输入。超出预算时依次移除最早的完整对话轮次、按整行缩短两级记忆索引、截短本轮成功工具的文本结果，最后截短工作笔记。仍然超限则本轮报错。裁剪只作用于本次模型请求，磁盘文件和 checkpoint 保留原文。[输入压缩逻辑](../src/corpcom/agent/input_budget.py)

`CORPCOM_USER_MEMORY_DIR` 设置用户级长期记忆的目录，默认是数据目录下的 `user/memory/`。相对路径以数据目录为基准，也支持绝对路径和 `~`。服务启动时解析配置，修改后需重启。该目录与 `workspaces/` 必须相互独立，重叠时服务启动报错。

## 接口清单

下表 `{wid}`、`{tid}`、`{rid}`、`{aid}` 分别表示 Workspace、Thread、Run 和文件 ID。

| 方法 | 路径 | 请求与返回 |
| --- | --- | --- |
| POST | `/api/workspaces` | 创建研究，返回 `Workspace`，状态码 201 |
| GET | `/api/workspaces` | 返回 `WorkspaceSummary[]` |
| GET | `/api/workspaces/{wid}` | 返回 `WorkspaceDetail`，包含会话和文件索引 |
| GET | `/api/workspaces/{wid}/working-note` | 每次从该工作区读取完整 `agent.md`，返回 UTF-8 纯文本 |
| GET | `/api/workspaces/{wid}/memory/index?scope=workspace` | 读取所选范围的完整 `memory.md`，返回 UTF-8 纯文本。必填参数 `scope` 可取 `workspace` 或 `user`，索引缺失时正文为空 |
| GET | `/api/workspaces/{wid}/memory/topics/{key}?scope=workspace` | 按单个 Markdown 文件名返回所选范围的完整 UTF-8 原文，包含 YAML 头部。范围同上，主题缺失返回 404 |
| PUT | `/api/workspaces/{wid}/research` | 更新提交的研究字段，返回 `Workspace` |
| PUT | `/api/workspaces/{wid}/preferences` | 保留的兼容路由，支持相同的五类字段；原 `{"preferences": {...}}` 请求仍整体替换偏好 |
| POST | `/api/workspaces/{wid}/threads` | `{"title":"首次讨论"}` 创建会话，返回 `Thread`，状态码 201 |
| GET | `/api/workspaces/{wid}/threads/{tid}` | 返回 `ThreadDetail`，包含按写入顺序排列的消息 |
| POST | `/api/workspaces/{wid}/threads/{tid}/messages` | `{"content":"继续研究"}` 调用 Agent，返回已保存的助手 `Message`，状态码 201 |
| POST | `/api/workspaces/{wid}/threads/{tid}/messages/stream` | `{"content":"继续研究"}` 开始新一轮，返回 SSE |
| GET | `/api/workspaces/{wid}/threads/{tid}/runs` | 返回按 `run_index` 排序的 `ResearchRunSummary[]` |
| GET | `/api/workspaces/{wid}/threads/{tid}/runs/{rid}/events?after=0-0` | 从 Redis 游标之后回放，运行中继续等待，终态后排空事件并结束 |
| GET | `/api/workspaces/{wid}/threads/{tid}/runs/{rid}/history` | 返回 `{"run": {...}, "events": [...]}` 历史快照 |
| GET | `/api/workspaces/{wid}/threads/{tid}/history` | 返回 `{"thread_id":"...", "turns":[{"user_message_id":1,"events":[...]}]}`，不要求 Run 记录 |
| POST | `/api/workspaces/{wid}/files` | multipart 的 `file` 与可选 `kind` 字段，返回 `Artifact`，状态码 201 |
| GET | `/api/workspaces/{wid}/files/{aid}` | 返回 `Artifact` 文件元信息 |
| GET | `/api/workspaces/{wid}/files/{aid}/content` | 下载已保存文件的内容 |

消息接口接收本轮用户文本。当前 thread 尚无 checkpoint 时，后端从业务库导入完整问答历史；已有 checkpoint 时，由框架恢复历史，仅输入本轮用户消息。模型生成回答后，协调器在同一业务库事务中保存最终回答和 succeeded 状态。前端无需传入历史、角色或模型配置。`/messages` 保留一次返回完整回答的用法，页面使用 `/messages/stream` 接收流式事件。完整的中间助手消息（包括工具调用声明）也会保存。

协调器在同一业务库写事务中取得 Thread 执行资格并保留更新前的状态。仅旧状态为 `interrupted` 时，才检查 checkpoint 末尾的工具调用，为缺失结果补入 `status="error"` 的 ToolMessage，说明后端中断、实际执行结果未知。写回时将 live state 中的原始消息（包括 pending writes 中已有的工具结果）沿用消息 ID 一并保存，再加入缺失结果，避免只保存补充消息而丢失已完成结果。恢复独立保存 checkpoint 后，再调用 `ask_workspace` 保存新问题并执行研究，不自动重放旧工具。`failed` 等其他状态不进入恢复，不修补更早的异常历史。恢复本身失败时记录 failed、释放额度，且不进入 Agent 执行。

`corpcom.db` 保存 Workspace 标识、标题、时间、会话、文件索引、用户和助手消息，以及 `research_runs` 运行记录和结束时的 SSE 归档；研究目标、初始假设、范围、观察周期和偏好统一保存于 `workspaces/<wid>/research.json`。同一数据目录下的 `checkpoints.sqlite` 保存 Agent 消息状态，包括工具调用和结果。服务通过 lifespan 管理 checkpoint 连接，使用 `workspace_id:thread_id` 标识执行历史。

## 流式执行、重连与历史

首次发送使用 POST `/messages/stream`。准入失败在 SSE 响应开始前返回 HTTP 错误；开始后执行由后台任务管理，浏览器断开不会取消研究。每条过程事件先收集到内存，再写入 Redis，成功后才通知订阅者。成功或普通异常收尾时，累计事件与运行终态在同一业务库事务中保存。

SSE 的 `data` 是带 `type` 的 JSON。`id:` 是 Redis Stream ID，客户端成功应用事件后再保存游标：

| 事件 | 主要字段与含义 |
| --- | --- |
| `started` | `workspace_id`、`thread_id`、`run_id`，标识本轮 |
| `text_delta` | `message_id`、`text`，按消息 ID 累积文本 |
| `tool_start` | `tool_run_id`、`tool_call_id`、`name` |
| `tool_end` | `tool_run_id`、`tool_call_id`、`content`、`status`；`status` 可为 success/error |
| `tool_error` | `tool_run_id`、`error`，记录抛出的工具异常 |
| `succeeded` / `failed` | 原 POST 流的终态，分别携带保存的 `message` 或 `error` |
| `run_finished` | GET 重连流的终态，携带 `run_id`、`status`、`error` |

`tool_run_id` 是工具回调执行 ID，`tool_call_id` 是模型声明的调用 ID，两者不可互换。终态事件不写入 Redis 过程事件列表，也没有 Redis ID；重连通过运行记录重建终态。

断连且已取得 `run_id` 后，使用 GET `/runs/{rid}/events?after=<最后成功应用的ID>`。`after` 默认为 `0-0`，读取严格晚于游标的事件；每批最多 100 条，运行中有限阻塞等待，结束后排空剩余事件再发送 `run_finished`。该 GET 不创建新问题、不重新执行工具。尚未收到 `started` 时前端无法用 Run ID 重连，会查询已有消息与状态，不自动重发 POST。重连接口只读 Redis，不以 SQLite 归档替代游标流。

整页刷新或切换会话时，前端先读取 Run 列表：运行中的 Run 走 Redis SSE，已结束的 Run 走 `/history`。Run 的 `run_index` 在 Thread 内从 0 分配；`user_message_id` 关联本轮问题。历史快照使用下列来源：

1. checkpoint：读取消息并用 `add_messages` 合并消息通道的 pending writes，在内存中按 `business:{user_message_id}` 分轮；不写回 checkpoint，也不执行 Agent。
2. `research_runs.sse_events`：不为 `null` 时使用这份结束归档；`[]` 表示已归档但没有事件。
3. Redis：归档为 `null` 时从 `0-0` 分批读取已保留事件，用于中途退出、未完成归档的情况。

完整助手消息投影为 `assistant_message`（`message_id`、`text`、原始 `tool_calls`），工具结果投影为 `tool_result`（`message_id`、`tool_call_id`、`content`、`status`）。完整快照不是文本增量。合并时以 checkpoint 为主：相同消息、已声明调用和已有工具结果不重复添加；归档中的缺失尾部文本及工具开始/结束/错误补入历史，错误通过回调 ID 关联模型调用 ID。

没有 Run 的旧问题通过 Thread `/history` 从 checkpoint 分组恢复。前端只取尚无 Run 关联的问题，因此同一会话可同时展示旧历史和新 Run；不会补造运行记录。缺失的工具结果保持未知，历史读取不会推测执行成功。

所有历史入口先验证 Workspace、Thread 和 Run 的归属。进程崩溃不保证内存事件已归档；Redis 可恢复此前成功写入且仍保留的事件，AOF `everysec` 也不保证断电零丢失。启动会把遗留 running 标记为 interrupted，不自动续跑。尚无 Redis TTL/裁剪策略、请求去重或业务库与 checkpoint 的跨库原子事务。

## 从创建研究到连续对话

创建研究：

```bash
curl -X POST http://127.0.0.1:8001/api/workspaces \
  -H 'Content-Type: application/json' \
  -d '{"title":"经营韧性研究","goal":"持续检验经营韧性","initial_hypothesis":"现金流能够持续覆盖经营投入","scope":"单家公司跟踪","preferences":{"报告语言":"中文"}}'
```

`title` 必填且不能全为空白；`goal`、`initial_hypothesis`、`scope`、`observation_period` 选填，省略时默认空字符串，`preferences` 默认空对象。返回结果里的 `id` 用作 Workspace ID。

研究档案表单和对话中的 Agent 工具都可以更新上述五类信息。直接调用接口时，只提交要修改的字段：

```bash
curl -X PUT http://127.0.0.1:8001/api/workspaces/替换为WorkspaceID/research \
  -H 'Content-Type: application/json' \
  -d '{"goal":"核对现金流质量","scope":"","observation_period":"未来一年"}'
```

未提交的字段保持原值；文本字段传 `""` 清空，不接受 `null`；`preferences` 整体替换，传 `{}` 清空。返回值包含保存后的完整 Workspace，五类研究字段位于 `info` 对象中，例如 `response.info.goal`；创建和详情响应也使用相同结构，列表摘要仍返回顶层 `goal`。创建与更新请求的研究字段仍在顶层。旧 `/preferences` 路径接受相同请求。存储初始化只创建当前表结构，数据布局见 [本地存储](workspace-local.md#数据位置与隔离)。

```bash
workspace_id="替换为 Workspace ID"
curl -X POST "http://127.0.0.1:8001/api/workspaces/$workspace_id/threads" \
  -H 'Content-Type: application/json' -d '{"title":"首次讨论"}'

thread_id="替换为 thread ID"
curl -X POST "http://127.0.0.1:8001/api/workspaces/$workspace_id/threads/$thread_id/messages" \
  -H 'Content-Type: application/json' -d '{"content":"根据研究目标，列出需要核对的问题。"}'
```

发送消息的成功响应是已保存的助手消息，字段为 `id`、`thread_id`、`model_message_id`、`role`、`content`、`tool_calls`、`created_at`。再次向同一地址发送新文本即可追问；重新获取 thread 详情可以恢复完整问答。

## 文件上传与下载

```bash
curl -X POST "http://127.0.0.1:8001/api/workspaces/$workspace_id/files" \
  -F 'file=@docs/research-workspace.md' -F 'kind=plan'
```

上传后生成独立副本；同名文件再次上传得到新的文件 ID。接口接收文件内容，文件名必须是普通文件名，不能包含目录。文件响应不返回服务器绝对路径，通过 `/content` 下载。支持二进制上传和下载。Agent 可通过 `execute_bash` 按工作区路径读取文件。

## 错误与执行行为

| 状态码 | 含义 |
| --- | --- |
| 404 | 研究、会话、Run 或文件不存在，或归属不匹配 |
| 409 | 同一 Thread 已有执行，本次提问尚未保存（整体额度未满时） |
| 422 | 请求字段不合法；`detail` 列出字段位置、原因和错误类型 |
| 502 | 非流式消息接口的模型失败或调用上限；问题和已完成的中间消息可能已保存，未保存本轮成功终答 |
| 503 | 后端未配置模型，或整体两轮额度已满；本次提问尚未保存 |

SSE 开始后的执行失败通过 `failed` 事件返回，不能再改变已发送的 HTTP 状态码。HTTP 错误返回 `{"detail": ...}`；未处理的程序错误由服务器返回 500 并记录异常。请求失败后可读取 thread 查看已保存消息；当前没有请求去重或自动续跑，重新发送会新增一条用户消息。启动时仅将遗留 running 标记为 interrupted。

接口契约与历史恢复行为分别由 [HTTP 测试](../tests/test_api.py) 和 [Run 历史测试](../tests/test_run_history.py) 覆盖。

工具执行中的普通 `Exception` 会被中间件记录，并转换为匹配调用 ID、`status="error"` 的 ToolMessage，让模型继续处理。框架控制流异常 `GraphBubbleUp` 和 Redis 基础设施错误继续抛出；模型错误由整轮失败收尾。两个数据库之间没有原子事务，当前不提供进程中断后的自动续跑，也不保证双库崩溃一致性。

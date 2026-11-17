# 本地后端接口

后端通过 FastAPI 暴露 Workspace、会话、文件和 Agent 对话，前端直接发送 HTTP 请求。研究状态与问答由 SQLite 和文件目录保存，由 `RunCoordinator` 管理准入、Agent 执行和终态保存。存储结构、Agent 执行流程与自动验收见 [本地存储说明](workspace-local.md)。

## 启动

在项目根目录执行：

```bash
uv sync
# .env 中设置 CORPCOM_REDIS_URL=redis://127.0.0.1:6380/0
docker compose up -d redis
uv run uvicorn corpcom.api:create_app --factory --host 127.0.0.1 --port 8001
```

- 交互接口文档：<http://127.0.0.1:8001/docs>
- OpenAPI 定义：<http://127.0.0.1:8001/openapi.json>
- 服务健康检查：`GET /health`，返回 `{"status":"ok"}`。

沿用项目 `.env` 的 `CORPCOM_MODEL`、`OPENAI_API_KEY` 和可选 `OPENAI_BASE_URL`。配置仅供后端使用。未配置模型时，研究、会话和文件接口仍可用，发送消息返回 `503`。

默认数据目录为启动目录下的 `data/`，与现有 Workspace 数据兼容。可通过 `CORPCOM_DATA_DIR` 指定绝对路径。默认允许前端来源 `http://localhost:5173` 和 `http://127.0.0.1:5173`，其他端口通过逗号分隔的 `CORPCOM_CORS_ORIGINS` 配置。

开发时可加 `--reload`；未启用时，Python 修改后需重启服务，新路由才会加载。Redis 地址通过 `CORPCOM_REDIS_URL` 配置，仓库 Compose 映射到本机 6380，使用 AOF `everysec` 和 `noeviction`。

当前服务面向单用户本机开发，使用一个 Uvicorn worker。同 Thread 拒绝重叠执行，同 Workspace 不同 Thread 可并行，整个进程最多两轮；满额立即拒绝，不排队。Redis 不替代进程内执行额度管理，多 worker 协调尚未实现。

## 接口清单

下表 `{wid}`、`{tid}`、`{rid}`、`{aid}` 分别表示 Workspace、Thread、Run 和文件 ID。

| 方法 | 路径 | 请求与返回 |
| --- | --- | --- |
| POST | `/api/workspaces` | 创建研究，返回 `Workspace`，状态码 201 |
| GET | `/api/workspaces` | 返回 `WorkspaceSummary[]` |
| GET | `/api/workspaces/{wid}` | 返回 `WorkspaceDetail`，包含会话和文件索引 |
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

消息接口接收本轮用户文本。当前 thread 尚无 checkpoint 时，后端从业务库导入完整问答历史；已有 checkpoint 时，由框架恢复历史，仅输入本轮用户消息。模型生成回答后，协调器在同一业务库事务中保存最终回答和 succeeded 状态。前端无需传入历史、角色或模型配置。`/messages` 保留一次返回完整回答的用法，页面使用 `/messages/stream` 接收流式事件。完整的中间助手消息（包括工具声明）也会保存，最终回答与成功状态原子提交。

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

前端调用示例：

```javascript
const response = await fetch(
  `http://127.0.0.1:8001/api/workspaces/${workspaceId}/threads/${threadId}/messages`,
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
curl -X POST "http://127.0.0.1:8001/api/workspaces/$workspace_id/files" \
  -F 'file=@docs/research-workspace.md' -F 'kind=plan'
```

上传后生成独立副本；同名文件再次上传得到新的文件 ID。接口接收文件内容，文件名必须是普通文件名，不能包含目录。文件响应不返回服务器绝对路径，通过 `/content` 下载。文件内容可以是二进制；Agent 的 `read_file` 工具已暂时停用，工作区文件通过 `execute_bash` 按路径读取，上传与下载接口不受影响。

## 错误与执行行为

| 状态码 | 含义 |
| --- | --- |
| 404 | 研究、会话、Run 或文件不存在，或归属不匹配 |
| 409 | 同一 Thread 已有执行，本次提问尚未保存（整体额度未满时） |
| 422 | 请求字段不合法；`detail` 列出字段位置、原因和错误类型 |
| 502 | 非流式消息接口的模型失败或调用上限；问题和已完成的中间消息可能已保存，未保存本轮成功终答 |
| 503 | 后端未配置模型，或整体两轮额度已满；本次提问尚未保存 |

SSE 开始后的执行失败通过 `failed` 事件返回，不能再改变已发送的 HTTP 状态码。HTTP 错误返回 `{"detail": ...}`；未处理的程序错误由服务器返回 500 并记录异常。请求失败后可读取 thread 查看已保存消息；当前没有请求去重或自动续跑，重新发送会新增一条用户消息。启动时仅将遗留 running 标记为 interrupted。

同步的 SQLite 和模型调用在请求工作线程内执行，模型等待不会占用 HTTP 事件循环。每个路由在执行线程内创建和关闭自己的数据库连接；Agent 工具也各自建立连接。

工具执行中的普通 `Exception` 会被中间件记录，并转换为匹配调用 ID、`status="error"` 的 ToolMessage，让模型继续处理。框架控制流异常 `GraphBubbleUp` 和 Redis 基础设施错误继续抛出；模型错误由整轮失败收尾。两个数据库之间没有原子事务，当前不提供进程中断后的自动续跑，也不保证双库崩溃一致性。

## 已验证

2026-11-17：69 项 Workspace／API 后端测试、58 项前端测试及生产构建通过。后端历史选择、pending writes、投影与合并、SQLite 重开、Redis 分批回退和 HTTP 归属检查已用临时脚本验证，其中 Redis 使用 mock；这些历史专项仍待固化为仓库测试。前端回归已覆盖无 Run 旧会话、混合新旧轮次和侧栏首次切换加载历史。

同日只读浏览器验收：真实旧会话 A3 可展开 `execute_bash` 参数及结果，结果含 `338350`；从侧栏切换到 A2 后也能展开历史工具结果。本次没有发起新模型请求，不等同于重新完成真实模型断网／进程崩溃全链路验收。


2026-10-10 全部 18 项自动测试通过，包含 HTTP 创建与读取、偏好替换、文件上传下载、问答保存、重新创建应用后的恢复、Workspace 隔离、非法输入、模型错误、并发请求拒绝以及浏览器 CORS 预检。

同日通过临时 Uvicorn 服务和现有真实模型接口验收：新会话读取旧会话与上传文件的随机标记，同会话第二轮准确承接上一轮信息；停止再启动服务后，四条问答消息和文件内容均能恢复。验收使用临时数据目录，测试服务已停止。

2026-10-19 全部 22 项自动测试通过。另使用临时数据目录完成真实模型验收：调用不存在文件的 read_file 后，匹配的失败 ToolMessage 和最终回答写入 checkpoint；停止并重新启动服务后，同一 thread 能准确回忆上一轮标记、工具参数与失败结果，未再次调用工具。磁盘核对得到六条 Agent 消息和四条业务问答，无重复历史。

2026-11-05 产物主动登记验收：`tests/test_register_artifact.py` 的 7 项测试通过，包括已有文件原地登记、重复与并发登记复用 ID、编辑后通过 `/content` 读取最新内容及跨 Workspace 访问拒绝；现有工具测试 6 项也通过，覆盖旧 Python 归档流程。

同日用户确认真实模型与前端联动验收完成：模型写报告、调用 `register_artifact`、前端文件列表显示、预览和下载通过。该项为用户实测确认；此前的 HTTP 自动测试与脚本化模型测试分别记录，不替代页面验收。该次验收时 API 仍为 Workspace 互斥。M1—M3 的完整证据见 [本地验收记录](workspace-local.md#m1m3-验收记录2026-11-05)。

2026-11-10 M4 后端执行协调已验收。全量回归：67 项中 66 通过、1 项真实金融接口测试按配置跳过，API 的 12 项测试全部通过。已验证同 Thread 冲突、同 Workspace 双 Thread 并行、跨 Workspace 共用两轮额度、满额拒绝不写数据、失败后额度复用、启动识别中断及终态保留。使用临时数据和脚本化模型，不作为前端或真实模型并发验收。

Thread 响应新增 `execution_status`、`execution_started_at`、`execution_finished_at`、`execution_error`。未执行时均为空；状态取 running/succeeded/failed/interrupted，仅保留最近一轮。整体额度由应用启动时的 `RunCoordinator(..., max_concurrent_runs=2)` 设置，满额立即拒绝；整体满额检查先于 Thread 冲突检查。首版要求单进程运行。

空 Workspace 并发首轮的重复初始化指令已修复：首条用户消息判断与消息插入在同一短事务内完成，并发回归确认只有首条用户消息触发初始化指令。验证记录和后续范围见 [本地执行协调器验收](workspace-local.md#m4-执行协调器与验收2026-11-10)。

2026-11-10 M6 真实并发验收完成：用户通过页面验证两条研究共享容器、运行中上传、文件互读、失败隔离、刷新与重启中断恢复。修复缺失工具结果后，新建 C2 的恢复与下一轮续聊均成功；只读核对确认匹配的 ToolMessage 已保存、旧工具未重放、回答未重复，A/B 终态及共享容器 ID 保持不变。修复后的后端回归为 74 项中 73 通过、1 项按配置跳过；现场证据见 [M6 验收记录](workspace-local.md#m6-验收记录2026-11-10)。

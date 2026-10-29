# Workspace 本地存储与验收

Workspace 通过 [后端 HTTP 接口](backend-api.md) 操作，创建研究、连续对话和文件上传下载的示例统一维护在接口文档中。SQLite 保存 Workspace 标识、标题、时间、会话、消息与文件索引，磁盘保存结构化研究信息和文件副本；每次存储方法写入成功即提交，服务退出后仍可读取。

## 数据位置与隔离

```text
data/
  corpcom.db
  checkpoints.sqlite
  workspaces/<workspace_id>/
    research.json
    files/<artifact_id>/<原文件名>
```

默认使用服务启动目录下的 `data/`，可通过 `CORPCOM_DATA_DIR` 指定已有数据目录。停止服务后，备份需要同时保留 corpcom.db、checkpoints.sqlite 和文件目录。Workspace 的 `execute_python` 使用 `workspaces/<workspace_id>/.runs/<执行编号>/`，将声明的产物复制到 `workspaces/<workspace_id>/tasks/<任务路径与版本>/` 并登记索引；独立 PTC 命令仍使用根目录 `runs/`。

`research.json` 是研究目标、初始假设、自由文本范围、观察周期和偏好的统一存储来源：

```json
{
  "goal": "",
  "initial_hypothesis": "",
  "scope": "",
  "observation_period": "",
  "preferences": {}
}
```

创建时仅标题必填。更新只替换提交的字段，未提交字段保持原值；文本传 `""` 清空，偏好传 `{}` 清空。偏好对象仍整体替换，不合并键。用户可通过研究档案表单、HTTP 接口或对话中的 `update_research` 工具管理这些信息。整个 Workspace 的第一条用户消息到来时，如果五类研究信息全部为空（空白文本也视为空），Agent 根据该消息整理目标、范围和待验证初始假设，通过 `update_research` 保存后继续回答；观察周期和偏好只提取消息中有依据的内容。若消息没有研究主题，则先询问目标。已有任一字段或任一会话已有用户消息时，不再自动初始化，此后只按用户明确要求修改。新会话拥有独立消息列表，共享这些研究信息与文件。

存储初始化只创建当前表结构，不包含旧结构的兼容或自动迁移逻辑。开发阶段已有旧数据库时，停止后端后手动运行一次：

```bash
.venv/bin/python scripts/migrate_workspace_research.py data
```

脚本将五个研究字段写入各 Workspace 的 `research.json`，直接删除数据库中的对应列；标识、标题、时间和关联记录保持不变。脚本针对旧表结构执行一次，不做版本判断或重复迁移。新建数据目录无需执行。迁移后列表、详情和 Agent 上下文均从该文件读取，文件缺失或格式错误会明确报错。

接口和 Agent 工具共用 `WorkspaceStore.update_research`。更新在 SQLite 写事务内读取最新文件并合并指定字段，再通过临时文件替换完整 JSON，防止并发写入丢失其他字段或读到半份 JSON。文件替换与 SQLite 时间戳提交不构成跨介质原子事务；手工编辑文件也不更新数据库时间戳。

文件导入时生成独立副本，原文件后来被修改或删除不影响副本；同名文件重复导入会生成不同 ID，保留各自内容。已登记的副本丢失时明确报错。

所有会话和文件操作都检查所属 Workspace ID，跨 Workspace 读取会话、写消息或获取文件会失败。这是单用户应用的数据归属检查；同一操作系统用户仍可直接访问磁盘文件。

## 自动验收

在项目根目录执行：

```bash
uv run python -m unittest discover -s tests -v
```

测试使用临时数据目录，不修改已有研究资料：

- `tests/test_workspace.py` 直接验收存储层：独立进程重开后恢复目标、偏好、假设、旧消息和文件，并建立新会话；还覆盖独立迁移脚本及初始化不自动迁移、文件作为统一来源、部分更新与清空、跨 Workspace 隔离、同名文件独立副本、原文件删除、非法输入不改状态及已保存文件丢失。
- `tests/test_workspace_models.py` 验收重开后的对象类型与 JSON 序列化。
- `tests/test_workspace_agent.py` 使用替身模型运行真实框架循环，覆盖首条消息初始化及跨会话和部分填写的排除条件、对话修改研究信息与新会话读取、多轮上下文、SQLite 重开恢复、旧历史仅导入一次、不同 thread 的 checkpoint 隔离、工具结果落盘、可预期工具错误后的续聊、跨 Workspace 归属检查、非预期异常传播、模型调用上限和空回答。
- `tests/test_api.py` 验收 HTTP 合同、研究信息部分更新与旧偏好路由兼容、偏好 JSON 类型与数值校验、文件上传下载、应用重建后的恢复、模型错误、并发限制和 CORS。

自动测试中的模型为替身；真实模型的 HTTP 验收记录见 [后端接口文档](backend-api.md#已验证)。Agent 已可通过 Python 工具生成并登记报告产物，当前只保存初始假设，尚无正式假设修订版本或调度。执行协调器、流式、上下文和记忆的计划见 [开发路线与进度](roadmap.md)，不以业务 Run 管理为前置条件。

## 代码中的数据对象

`src/corpcom/workspace_models.py` 定义 Pydantic 数据对象；`WorkspaceStore` 负责数据库事务、文件保存与归属检查，FastAPI 将响应对象序列化为 JSON。

| 对象 | 用途 |
| --- | --- |
| `WorkspaceResearchInfo` | `research.json` 的五类字段，也用于创建和部分更新的输入契约 |
| `Workspace` | 标识、标题、时间与 `info: WorkspaceResearchInfo`，通过组合引用研究信息 |
| `Thread` / `Message` | 会话元信息与单条消息，通过 ID 关联 |
| `Artifact` | 文件索引，保存相对数据目录的路径 |
| `WorkspaceSummary` | 研究列表使用的简短字段集合 |
| `WorkspaceDetail` | 研究本体加会话和文件索引 |
| `ThreadDetail` | 会话加按写入顺序排列的消息 |
| `ArtifactFile` | 存储层使用的文件索引及绝对路径；HTTP 文件响应使用 `Artifact` |

`store.get_workspace(workspace_id)` 返回 `Workspace`，通过 `.info.goal`、`.info.preferences` 等访问研究信息；需要关联索引时调用 `store.get_workspace_detail(workspace_id)`。时间字段是 `datetime`，实际文件路径是 `Path`；`model_dump(mode="json")` 可转换为 JSON 兼容值。修改返回对象的属性不会自动写回数据库，持久化仍由存储方法完成。

## 模型上下文与 LangChain 执行循环

`workspace_agent.py` 负责组装研究上下文、注册工具、调用 Agent 并保存问答。Python 入口为 `ask_workspace(store, model, workspace_id, thread_id, question, checkpointer)`，`model` 是 LangChain 的 `BaseChatModel` 实例。checkpointer 接收框架的 BaseCheckpointSaver；HTTP 服务使用由 lifespan 管理的 SqliteSaver。

| 职责 | 实现 |
| --- | --- |
| 工具参数与描述 | `workspace_tools.py` 中的 `@tool` 从类型标注和英文 docstring 生成 |
| 模型调用、工具分发及结果回传 | LangChain `create_agent` 执行循环 |
| 模型连接 | API 的 `get_model` 创建 `ChatOpenAI`，读取 `CORPCOM_MODEL`、`OPENAI_API_KEY`、`OPENAI_BASE_URL` |
| 业务数据持久化 | `WorkspaceStore` 保存研究资料、用户消息及最终助手回答 |
| Agent 执行状态持久化 | `SqliteSaver` 保存消息状态，包括工具调用和结果，以 `workspace_id:thread_id` 区分会话 |

模型收到目标、范围、观察周期、初始假设、偏好、当前会话消息，以及其他会话和文件索引。`create_workspace_tools(data_dir, workspace_id)` 返回绑定当前研究的 `read_thread` 和 `read_file`，模型按需读取旧消息与 UTF-8 文本文件；Workspace ID 由程序绑定，不交给模型选择。`update_research(changes)` 将用户指定的字段保存到 `research.json`，返回更新后的全部研究信息，供本轮后续回答使用。工具在线程中运行，每次操作在执行线程内建立并关闭独立 SQLite 连接。

每次提问最多调用模型六次，超限明确报错。工具参数不符合 schema 时，框架将校验错误反馈给模型；工具执行中的 `RecordNotFoundError` 由中间件转换为匹配调用 ID、`status="error"` 的 ToolMessage，模型可以据此修正调用或解释失败。后续模型调用仍计入六次上限。其他未处理异常向调用方抛出；调用中断时保留已写入的用户提问，不保存成功回答。

当前 thread 尚无 checkpoint 时，从业务库导入已有问答，包括已经保存的本轮用户消息；之后每轮仅输入新用户消息，由 checkpoint 恢复此前的 Agent 消息。研究目标、偏好及资料索引仍在每轮刷新。

业务库与 checkpoint 数据库之间没有原子事务。当前支持已完成轮次的历史恢复，不保证进程中途退出后的自动恢复，也未实现请求去重或最终回答的跨库幂等同步。

Agent 工具已包含 `execute_python` 与按需查询数据函数契约的 `read_function_doc`，可使用已接入的数据函数取数并保存研究产物。文件读取仍仅支持 UTF-8 文本，未对长文件自动摘要。下一步的 Jinja2 模板、YAML 配置、运行时注入、上下文预算及研究记忆尚待实现，具体范围见 [当前路线](roadmap.md)。

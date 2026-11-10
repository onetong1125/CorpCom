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
    tasks/<任务名>/<代码、数据或报告>
    .runs/<旧 execute_python 执行编号>/
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

存储初始化只创建当前表结构，不包含旧结构的兼容或自动迁移逻辑。五个研究字段保存在各 Workspace 的 `research.json` 中；列表、详情和 Agent 上下文均从该文件读取，文件缺失或格式错误会明确报错。

接口和 Agent 工具共用 `WorkspaceStore.update_research`。更新在 SQLite 写事务内读取最新文件并合并指定字段，再通过临时文件替换完整 JSON，防止并发写入丢失其他字段或读到半份 JSON。文件替换与 SQLite 时间戳提交不构成跨介质原子事务；手工编辑文件也不更新数据库时间戳。

文件导入时生成独立副本，原文件后来被修改或删除不影响副本；同名文件重复导入会生成不同 ID，保留各自内容。已登记的副本丢失时明确报错。

所有会话和文件操作都检查所属 Workspace ID，跨 Workspace 读取会话、写消息或获取文件会失败。这是单用户应用的数据归属检查；同一操作系统用户仍可直接访问磁盘文件。

## Thread 最近执行状态

`Thread` 和 `threads` 表直接保存最近一轮的 `execution_status`、`execution_started_at`、`execution_finished_at`、`execution_error`，不另建执行表或执行 ID。状态可为 `running`、`succeeded`、`failed`、`interrupted`；尚未执行时四个字段均为空。每轮覆盖最近状态，消息历史仍保留在 `messages` 表。

2026-11-06：已通过 DDL SQL（`ALTER TABLE threads ADD COLUMN`）为已有 `threads` 表增加上述四个字段，并同步更新 `Thread` 模型和新库建表语句。已核对本地 `data/corpcom.db` 的表结构；执行生命周期于后续 M4 接入，验收范围见下文。

## M4 执行协调器与验收（2026-11-10）

HTTP 消息接口通过应用共用的 `RunCoordinator` 执行研究。首版单进程，整体最多两轮，不排队；先获取额度，再通过数据库条件 UPDATE 取得 Thread 执行资格。同 Thread 冲突返回 409，整体满额返回 503（满额检查优先）；两种拒绝均发生在用户问题保存前。同 Workspace 的不同 Thread 可并行执行。

`ask_workspace` 保存用户问题并生成回答，协调器通过 `complete_thread_execution` 在同一 SQLite 事务内插入最终回答、写入 succeeded 和结束时间并更新 Workspace 时间。失败时记录 failed 并原样抛出异常，finally 释放整体额度。业务库与 checkpoint 库之间仍没有跨库原子事务，也没有请求去重、自动重试或完整执行历史。

现有 Thread 详情及 Workspace 的 Thread 列表返回最近执行状态。应用启动、接受请求前将遗留 running 改为 interrupted，保留开始时间和消息；结束时间是确认中断的时间。普通打开 Store 不触发此处理，重复启动不刷新已有终态。不支持多个服务进程同时使用同一数据目录。

本次执行 `.venv/bin/python -m unittest discover -s tests -v`：67 项，66 通过、1 按配置跳过；跳过的是需 `CORPCOM_LIVE_TESTS=1` 的真实金融接口测试。模型测试使用脚本化替身。已补齐的仓库回归场景：

| 工件 | 验收内容 |
| --- | --- |
| `tests/test_api.py`（12 项通过） | 同 Thread 拒绝重叠；同 Workspace 双 Thread 同时进入执行；跨 Workspace 共用两轮额度，第三轮拒绝且数据不变；额度复用；连续两次模型失败后的状态和消息；应用启动标记中断、其他终态不变、再次启动不刷新结束时间 |
| `tests/test_workspace.py` | 在回答 INSERT 后用 SQLite 触发器拒绝状态 UPDATE，确认异常传播，回答和状态整体回滚；新连接读取仍与原快照一致 |
| `examples/probe_agent_bash.py` | 前述接入步骤已运行通过：脚本化模型、真实 Docker、工具结果回传、问答与 checkpoint 落盘及容器复用；本次仅新增测试，未重复执行该 probe |

**空 Workspace 并发首轮初始化竞态已修复并通过回归。** `add_user_message` 使用 `BEGIN IMMEDIATE`，在同一短事务内判断是否已有用户消息并插入本轮问题，返回消息及首条标记。`ask_workspace` 使用该标记和研究信息是否为空决定初始化指令。`tests/test_workspace.py` 验证并发插入只有一个首条标记；`tests/test_workspace_agent.py` 使用 Barrier 确保两个问题均已保存后才组装提示词，验证只有首条问题得到初始化指令、两轮均正常保存回答。测试使用临时数据库、真实框架和脚本化模型，不作为真实模型初始化质量证据。M4 最终验收待汇总。

前端 Thread 运行展示、切换及上传属于 M5，两个真实研究任务的完整并发验收属于 M6；M6 验收后才合并 PR。

## 共享容器与 Bash 验证

`sandbox.run_bash` 通过 `ensure_workspace_container` 获取同一 Workspace 的共享容器，再用 `docker exec` 执行命令。传入的 `workspace_dir` 挂载到容器的 `/workspace`，也是命令的工作目录；文件可跨调用读取，各次调用使用独立进程。返回值包含 `exit_code`、`stdout` 和 `stderr`。

命令默认限时 90 秒，可通过 `timeout_seconds` 调整。容器内的 GNU `timeout --signal=KILL` 在到期时终止命令进程组，不提供退出清理时间；宿主机的等待上限额外留出 12 秒，用于等待 Docker 返回，不代替容器内的超时控制。超时测试预期返回 `137`，但其他 KILL 终止也可能返回该值，不能单凭它判定超时。进程组方案不覆盖主动脱离进程组的进程。

新建共享容器使用 `--init` 回收孤儿进程，避免超时后积累僵尸进程。已有容器不会因重新启动而获得此选项，需要重建；挂载目录中的文件保留不等于安装环境保留。

### 手动生命周期与依赖维护

首版由开发者手动停止或删除指定 Workspace 容器，不做自动空闲停止或自动清理。操作前先等待研究请求结束并停止后端，避免执行中的命令被中断或新请求同时进入；当前没有协调停止与命令执行的接口。

容器名为 `corpcom-workspace-<workspace_id>`。手动执行 `docker stop <容器名>` 后，下次 Bash 调用会启动原容器；手动执行 `docker rm <已停止的容器名>` 后，下次调用会重新创建容器。删除容器不删除宿主机 Workspace 目录。这里的停止不是 `docker pause`。

正式依赖由 [Dockerfile.sandbox](../Dockerfile.sandbox) 维护，新容器使用 `corpcom-sandbox:local` 镜像中的环境；不保证恢复运行期间临时安装的依赖。当前镜像以 Python 3.11 slim 为基础，安装 `akshare==1.19.1`，未完整锁定基础镜像和间接依赖。更新正式依赖后，在项目根目录执行 `docker build -f Dockerfile.sandbox -t corpcom-sandbox:local .`，再按上述流程停止并删除旧容器，下次调用使用新镜像创建容器。仅停止后启动旧容器不会应用新镜像。

根文件系统只读、使用非 root 用户；`/workspace` 是持久挂载，`/tmp` 是临时 tmpfs，停止或删除容器后其内容不保留。依赖策略是按镜像重新提供环境，不是备份运行时环境。

### 可重复验证

在项目根目录、Docker 可访问且已构建 `corpcom-sandbox:local` 镜像时执行容器生命周期验证：

```bash
.venv/bin/python examples/probe_workspace_container.py
```

该脚本使用唯一 Workspace ID 和临时目录，验证 6 个线程经 Barrier 同时首次获取返回同一容器 ID、停止后恢复原 ID 且能读取文件，以及删除重建后 ID 改变但挂载文件保留；结束时清理测试容器。2026-11-05 用户补齐并发与停止恢复场景后，助手运行完整脚本通过。该 probe 不验证新增依赖或镜像更新流程。

Bash 执行验证：

```bash
.venv/bin/python examples/probe_workspace_bash.py
```

脚本使用随机 Workspace ID 和临时目录，结束时删除本次测试容器。验证内容包括：

- 工作目录为 `/workspace`，文件跨调用可读且宿主机内容一致。
- stderr 与非零退出码正确返回。
- 两条各等待 2 秒的命令并行执行，打印总耗时供检查。
- 一条命令超时后，另一条并行命令正常完成；忽略 TERM 的子进程及其 Bash 父进程均被终止并回收。
- 超时后仍可执行命令，且获取到的容器 ID 保持不变。

2026-11-05 验收：上述容器层行为已通过真实 Docker 验证，补齐后的仓库 probe 也由用户运行通过。Agent 已接入 `execute_bash`，完整调用链另由 `examples/probe_agent_bash.py` 验证；现有 `execute_python` 仍使用独立执行与复制归档流程。该次验收时 API 仍为 Workspace 互斥；当前后端 Thread 并发进展见 M4 验收，前端并发与真实双研究验收仍待 M5—M6。

## 工作区文本写入与编辑

系统提示词和工具说明要求：创建或完整覆盖文本文件使用 `write_file`，局部修改使用 `edit_file`；不得用 shell 重定向、heredoc、`tee`、`sed -i` 或临时脚本代替。Bash 用于浏览、搜索、读取文件及执行程序；需要编写代码文件时，先通过文件工具保存，再执行。编辑匹配失败时读取最新内容并修正参数，不切换到 shell 绕过。这是模型的工具使用规则，当前没有在 Bash 执行层增加命令拦截。

Agent 可调用 `write_file(file_path: str, content: str)` 创建或完整覆盖 UTF-8 文件，缺失的父目录会自动创建；`edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False)` 编辑已有 UTF-8 文件。两者的 `file_path` 都相对于 `/workspace`，例如 `tasks/demo/report.md`；绝对路径、越出工作区的路径及指向工作区外的符号链接会返回错误。

Edit 使用字面字符串匹配：`old_string` 不能为空，默认必须恰好匹配一次；`replace_all=True` 替换全部非重叠匹配。找不到匹配或默认模式下存在多处匹配时不改文件，返回错误工具消息供模型修正；`new_string` 可以为空，用于删除匹配内容。成功返回相对路径，Edit 还返回替换次数。未预期的 I/O 异常继续向调用方抛出。

两个工具在单进程内按解析后的实际路径共享锁，覆盖整个读改写过程，不同工具实例和指向同一路径的符号链接使用同一把锁。Bash、外部程序及其他服务进程的写入不受该锁保护，也不保证与这些写入并发时的文件一致性。这两个工具不自动登记 Artifact，`research.json` 仍通过 `update_research` 更新。

## 主动登记工作区产物

`register_artifact(file_path: str, kind: str)` 将当前 Workspace 中已有的普通文件登记到 Artifact 索引，不复制或移动文件。`file_path` 相对于 `/workspace`，例如 `tasks/demo/report.md`；`kind` 为非空分类，例如 `report`、`data`、`code`。绝对路径、越界路径、指向工作区外的符号链接、目录和不存在的文件会返回错误工具消息。

模型完成通过 `write_file` 或 Bash 执行程序生成的交付文件后，调用此工具；临时文件不必全部登记。返回值包含 `artifact_id`、`name`、`kind` 和相对于数据目录的 `relative_path`。同一解析后路径重复登记复用已有 ID 和分类，不修改分类；并发登记的查询与插入使用同一个写事务。上传文件和 `execute_python` 的 `outputs` 已自动登记，无需再次登记，旧 Python 复制归档流程保持原样。

登记后的文件通过已有文件列表、元数据和下载接口访问。后续编辑该文件沿用原 ID，接口读取最新内容；登记不创建不可变快照或历史版本。专项验证见 `tests/test_register_artifact.py`，覆盖登记、重复与并发登记、路径隔离，以及编辑后通过 HTTP 读取新内容。

## M1—M3 验收记录（2026-11-05）

本节分别记录自动测试、脚本化模型与真实 Docker 验证，以及用户的真实模型和前端确认。全量回归结果来自 2026-11-05 上一会话交接记录；本会话在补齐 M1 probe 后实际运行了该脚本，未重复运行未受影响的全量测试。

| 层次 | 证据与工件 | 结果 |
| --- | --- | --- |
| M1 容器层 | [容器生命周期 probe](../examples/probe_workspace_container.py) 已补齐并发首次获取和停止后恢复；本会话助手运行，真实 Docker | 6 个线程首次获取返回同一 ID；停止后恢复原 ID 并能读取文件；删除重建后 ID 改变、文件保留；脚本正常退出并清理测试容器 |
| M2 Bash 层 | [Bash probe](../examples/probe_workspace_bash.py)，真实 Docker；助手分步验证，最终仓库脚本由用户运行通过 | 工作目录、跨调用读写、输出与退出码、并行执行、超时父子进程终止与回收、原容器复用通过 |
| M3 Agent 到 Docker | [Agent Bash probe](../examples/probe_agent_bash.py)，助手实际运行；脚本化模型、真实框架及 Docker | 文件列表和内容回传、业务问答与 checkpoint 落盘、容器复用通过；固定模型脚本不作为自主工具选择证据 |
| M3 文件工具与上下文 | [文件工具测试](../tests/test_workspace_file_tools.py)、[Agent 测试](../tests/test_workspace_agent.py)、[工具测试](../tests/test_workspace_tools.py)；相关 Workspace 与 API 回归分批通过 | 创建覆盖、唯一/全部匹配、失败不改文件、路径限制、跨实例与路径别名串行、Write/Edit 共锁、错误后修正调用，以及不注入 files 验证通过 |
| M3 登记与 HTTP | [登记专项测试](../tests/test_register_artifact.py) 7 项及工具测试 6 项由助手运行通过 | 原地登记、重复/并发复用 ID、旧上传及 Python 归档索引复用、编辑后 HTTP 读取新内容、跨 Workspace 拒绝访问通过 |
| M3 真实模型与前端 | 用户确认实测验收完成 | 模型写报告 → 调用登记工具 → 前端文件列表出现 → 预览及下载通过；该项来源为用户实测确认 |

### 合并前回归与收尾结果

上一会话在提交 `fd443e0` 后运行了以下回归，结果由 2026-11-05 交接记录承接：

| 检查 | 结果 |
| --- | --- |
| `.venv/bin/python -m unittest discover -s tests -v` | 共 62 项，61 通过、1 按配置跳过；跳过 `test_fundamentals_live.FundamentalsLiveTests.test_matches_annual_report_baseline`，未启用 `CORPCOM_LIVE_TESTS=1`，不作为真实金融接口验证 |
| `frontend/` 下 `npm test` | 1 个测试文件、13 项测试通过 |
| `frontend/` 下 `npm run build` | 生产构建成功 |
| `probe_workspace_container.py`、`probe_workspace_bash.py`、`probe_agent_bash.py` | 三个真实 Docker probe 全部正常退出，使用唯一测试容器与临时目录并完成清理；Agent probe 使用脚本化模型 |

本会话后续只增加 M1 probe 场景并更新文档，未修改产品实现。新增场景的完整 probe 已运行通过；手动停止／删除、暂不自动清理，以及正式依赖由镜像提供、临时安装依赖不保证恢复的策略已由用户确认。

M1、M2、M3 在上述范围内已验收。2026-11-06 核对：已通过 PR #1 合并到 main，合并提交为 `65b6c02`。M4—M6 在新分支 `xyt/feat-thread-concurrency` 上推进，M6 验收完成后再合并 PR。

上述为 M1—M3 验收范围；后续 RunCoordinator 与 Thread 并发的进展见 M4 验收记录，M5—M6 尚未完成。

## 自动验收

在项目根目录执行：

```bash
uv run python -m unittest discover -s tests -v
```

测试使用临时数据目录，不修改已有研究资料：

- `tests/test_workspace.py` 直接验收存储层：独立进程重开后恢复目标、偏好、假设、旧消息和文件，并建立新会话；还覆盖文件作为统一来源、部分更新与清空、跨 Workspace 隔离、同名文件独立副本、原文件删除、非法输入不改状态及已保存文件丢失。
- `tests/test_workspace_models.py` 验收重开后的对象类型与 JSON 序列化。
- `tests/test_workspace_file_tools.py` 验收文本创建、覆盖、精确替换、路径限制，以及 Write/Edit 的单进程文件锁。
- `tests/test_register_artifact.py` 验收主动登记、去重、并发与现有 HTTP 产物访问链路。
- `tests/test_workspace_agent.py` 使用替身模型运行真实框架循环，覆盖首条消息初始化及跨会话和部分填写的排除条件、对话修改研究信息与新会话读取、多轮上下文、SQLite 重开恢复、旧历史仅导入一次、不同 thread 的 checkpoint 隔离、工具结果落盘、可预期工具错误后的续聊、跨 Workspace 归属检查、非预期异常传播、模型调用上限和空回答。
- `tests/test_api.py` 验收 HTTP 合同、研究信息部分更新与旧偏好路由兼容、偏好 JSON 类型与数值校验、文件上传下载、应用重建后的恢复、模型错误、并发限制和 CORS。

自动测试中的模型为替身；真实模型的 HTTP 验收记录见 [后端接口文档](backend-api.md#已验证)。Agent 已可通过 Python 工具生成并登记报告产物，当前只保存初始假设，尚无正式假设修订版本或调度。执行协调器的收尾及后续流式、上下文和记忆计划见 [开发路线与进度](roadmap.md)，不以业务 Run 管理为前置条件。

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

`workspace_agent.py` 负责组装研究上下文、注册工具、保存用户问题并调用 Agent 生成回答；最终回答由 RunCoordinator 调用存储方法与成功状态一并保存。Python 入口为 `ask_workspace(store, model, workspace_id, thread_id, question, checkpointer)`，`model` 是 LangChain 的 `BaseChatModel` 实例。checkpointer 接收框架的 BaseCheckpointSaver；HTTP 服务使用由 lifespan 管理的 SqliteSaver。

| 职责 | 实现 |
| --- | --- |
| 工具参数与描述 | `workspace_tools.py` 中的 `@tool` 从类型标注和英文 docstring 生成 |
| 模型调用、工具分发及结果回传 | LangChain `create_agent` 执行循环 |
| 模型连接 | API 的 `get_model` 创建 `ChatOpenAI`，读取 `CORPCOM_MODEL`、`OPENAI_API_KEY`、`OPENAI_BASE_URL` |
| 业务数据持久化 | `WorkspaceStore` 保存研究资料、用户消息及最终助手回答 |
| Agent 执行状态持久化 | `SqliteSaver` 保存消息状态，包括工具调用和结果，以 `workspace_id:thread_id` 区分会话 |

模型收到目标、范围、观察周期、初始假设、偏好、当前会话消息，以及其他会话索引；每轮 context 不再注入完整文件索引。`create_workspace_tools(data_dir, workspace_id)` 返回绑定当前研究的工具，模型通过 `read_thread` 读取旧消息，通过 `execute_bash` 按路径发现和读取工作区文件。提示词说明上传或导入文件位于 `/workspace/files/<artifact_id>/<原文件名>`，任务产物（包括 Python 归档产物）位于 `/workspace/tasks/`；目录可能尚未创建，应先查看实际目录结构。`read_file(artifact_id)` 暂时注释并停止注册。Workspace ID 由程序绑定，不交给模型选择；Artifact 索引和前端文件查询、下载接口保留。`update_research(changes)` 将用户指定的字段保存到 `research.json`，返回更新后的全部研究信息，供本轮后续回答使用。工具在线程中运行，每次存储操作在执行线程内建立并关闭独立 SQLite 连接。

每次提问最多调用模型十次，超限明确报错。工具参数不符合 schema 时，框架将校验错误反馈给模型；工具执行中的 `RecordNotFoundError` 由中间件转换为匹配调用 ID、`status="error"` 的 ToolMessage，模型可以据此修正调用或解释失败。后续模型调用仍计入十次上限。其他未处理异常向调用方抛出；调用中断时保留已写入的用户提问，不保存成功回答。

当前 thread 尚无 checkpoint 时，从业务库导入已有问答，包括已经保存的本轮用户消息；之后每轮仅输入新用户消息，由 checkpoint 恢复此前的 Agent 消息。研究状态及其他会话索引仍在每轮刷新；文件通过工具按需发现，历史消息中的文件引用和工具结果仍随 checkpoint 恢复。

业务库与 checkpoint 数据库之间没有原子事务。当前支持已完成轮次的历史恢复，不保证进程中途退出后的自动恢复，也未实现请求去重或最终回答的跨库幂等同步。

Agent 工具已包含 `execute_python` 与按需查询数据函数契约的 `read_function_doc`，可使用已接入的数据函数取数并保存研究产物。工作区文件通过 Bash 按需读取，未对长文件自动摘要。下一步的 Jinja2 模板、YAML 配置、运行时注入、上下文预算及研究记忆尚待实现，具体范围见 [当前路线](roadmap.md)。

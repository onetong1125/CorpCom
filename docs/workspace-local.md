# Workspace 存储与执行

## 数据位置与隔离

```text
data/
  corpcom.db
  checkpoints.sqlite
  workspaces/<workspace_id>/
    research.json
    agent.md
    files/<artifact_id>/<原文件名>
    tasks/<任务名>/<代码、数据或报告>
    .runs/<旧 execute_python 执行编号>/
```

默认使用服务启动目录下的 `data/`，可通过 `CORPCOM_DATA_DIR` 指定已有数据目录。停止服务后，备份需要同时保留 corpcom.db、checkpoints.sqlite 和文件目录；若还需保留未归档的过程事件，应同时保留 Redis 数据卷。Redis 使用独立的 Compose `redis-data` 卷，不在上述 data 目录内。Workspace 的 `execute_python` 使用 `workspaces/<workspace_id>/.runs/<执行编号>/`，将声明的产物复制到 `workspaces/<workspace_id>/tasks/<任务路径与版本>/` 并登记索引；独立 PTC 命令仍使用根目录 `runs/`。

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

### 工作笔记

每个 Workspace 根目录下的 `agent.md` 是该工作区所有会话共享的 UTF-8 Markdown。新建工作区时生成空模板，已有工作区首次读取时补建。每轮提问会读取磁盘上的最新正文。研究取得实质进展或用户指定下一步时，Agent 可用 `edit_file` 更新对应事项。

点击「研究档案」旁的「记忆」查看工作笔记。用本地编辑器保存修改后，点击「刷新工作笔记」或重新打开面板即可查看完整正文。关闭面板可继续当前对话和草稿。

### 长期记忆预览

打开「记忆 → 长期记忆」，在「当前工作区」和「我的全局记忆」间切换。面板按所选目录的 `memory.md` 列出主题。点击标题会从磁盘读取该主题，展示文件名、正文及其中记录的依据。用编辑器保存后，收起再展开可查看最新正文。若索引引用的文件缺失，面板会显示文件名，补回文件后可点击「重新读取」。

## 会话与运行

同一 Workspace 的 Thread 共享档案与文件，各自拥有独立的 Agent checkpoint。每轮调用都会加载研究档案和其他会话的索引，Agent 可以用 `read_thread` 读取旧对话，用 Bash 按需查找资料。

`threads` 保存会话最近的执行状态，`research_runs` 按轮保存运行记录、关联问题和结束时归档的事件。协调器在同一事务内保存最终回答和成功状态。运行中断后，下次提问时会保留 checkpoint 中已有的工具结果，将末尾缺失的结果标为未知。[流式执行、重连与历史](backend-api.md#流式执行重连与历史)

## 沙箱与文件工具

| 工具 | 执行与保存方式 |
| --- | --- |
| `execute_bash` | 同一 Workspace 复用一个 Docker 容器，工作目录为 `/workspace`。每次命令使用独立进程，文件可跨调用和会话读取。命令默认限时 90 秒。 |
| `write_file` / `edit_file` | 按相对路径写入或编辑 UTF-8 文本。编辑默认要求旧片段唯一匹配。同一进程的文件工具按路径加锁，直接从 Bash 写入的文件不受该锁保护。 |
| `register_artifact` | 将工作区中已有的文件原地登记为产物，同一路径重复登记复用 ID。编辑后下载的是最新内容。 |
| `execute_python` | 在独立容器中执行脚本，将声明的输出文件从本轮目录复制到任务目录并登记。上传文件也会自动登记。 |

### 手动生命周期与依赖维护

共享容器名为 `corpcom-workspace-<workspace_id>`。等待研究结束并停止后端后，可用 `docker stop <容器名>` 停止容器，下次调用 Bash 会重新启动它。若已删除该容器，下次调用会新建容器并挂载原工作区目录。

更新依赖时，修改 [Dockerfile.sandbox](../Dockerfile.sandbox)，重新构建 `corpcom-sandbox:local` 镜像，然后停止并删除使用旧镜像的工作区容器。工作区文件保存在宿主机，容器的 `/tmp` 为临时目录。

### 验证容器行为

在仓库根目录构建沙箱镜像后，可分别验证容器复用与重建，以及 Bash 并行与超时：

```bash
uv run python examples/probe_workspace_container.py
uv run python examples/probe_workspace_bash.py
```

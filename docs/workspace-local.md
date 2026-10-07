# Workspace 本地操作与验收

本批完成本地存储和命令行入口：SQLite 保存研究信息、会话、消息与文件索引，磁盘保存导入文件的副本。每条命令是一次独立进程，写入成功即提交，退出命令行后仍可读取。

## 1. 创建研究

在项目根目录执行 `uv sync`，再按 [README](../README.md#workspace-本地验收) 创建 Workspace。`title`、`goal`、`hypothesis` 为必填非空文本；范围与观察周期可先不填；偏好是 JSON 对象。未知偏好保持空对象，不根据目标自动推断。

从返回 JSON 复制 `id`：

```bash
workspace_id="替换为创建结果中的 id"
uv run corpcom-workspace list
uv run corpcom-workspace show "$workspace_id"
```

`show` 返回目标、研究范围、观察周期、初始假设、偏好、会话列表和文件列表。范围可以填写公司、行业或事件等文本，指标不会被写死。

## 2. 创建会话并保存消息

```bash
uv run corpcom-workspace thread-create "$workspace_id" --title '首次研究'
thread_id="替换为会话创建结果中的 id"
uv run corpcom-workspace message-add "$workspace_id" "$thread_id" \
  --content '先核对现金流变化，再判断是否支持假设。'
uv run corpcom-workspace thread-show "$workspace_id" "$thread_id"
```

`message-add` 只保存输入，默认角色为 `user`，也可显式传 `--role assistant` 保存已有助手文本；它不会生成回答。新会话的消息列表为空，同一个 Workspace 的目标、偏好与文件则共用一份。

## 3. 修改偏好并导入文件

```bash
uv run corpcom-workspace set-preferences "$workspace_id" \
  --preferences '{"关注指标":["经营现金流"],"报告语言":"中文","报告形式":"简短"}'
uv run corpcom-workspace file-add "$workspace_id" docs/research-workspace.md --kind plan
artifact_id="替换为文件导入结果中的 id"
uv run corpcom-workspace file-show "$workspace_id" "$artifact_id"
```

偏好命令**整体替换**原 JSON 对象，不是按键合并；传 `{}` 可清空。这里只记录用户明确输入的当前偏好，尚无偏好修改历史或关联消息的自动提取。

上面导入的是已有设计文档，用于验收文件保存，并非 Agent 生成的研究报告。可以替换为实际数据、研究计划或报告的本地文件。`--kind` 是自定义标签，如 `plan`、`data`、`report`。

文件导入时复制到 Workspace 目录，返回文件 ID 和绝对路径；原文件后来被修改或删除，不影响副本。同名文件重复导入会生成不同 ID，保留各自内容。`file-show` 返回副本路径，文件可用编辑器直接打开；如果副本已被手动删除，命令明确报错。

## 4. 退出后继续

退出终端后重新打开，在同一个项目目录执行：

```bash
uv run corpcom-workspace list
workspace_id="替换为已有研究的 id"
uv run corpcom-workspace show "$workspace_id"
uv run corpcom-workspace thread-create "$workspace_id" --title '第二天继续研究'
```

查看旧会话使用原来的 thread ID；查看文件使用原来的 artifact ID。终端变量无需持久化，ID 可通过 `list` 和 `show` 重新查找。

如需从任意目录使用同一份存储，将全局参数放在子命令前：

```bash
corpcom-workspace --data-dir /绝对路径/CorpCom/data list
```

这里假设已激活项目虚拟环境，命令可在 PATH 中找到。也可以使用项目 `.venv/bin/corpcom-workspace` 的绝对路径。默认 `data/` 相对于当前工作目录；换目录后不指定 `--data-dir` 会使用另一份存储。

## 5. 数据位置与隔离

```text
data/
  corpcom.db
  workspaces/<workspace_id>/
    files/<artifact_id>/<原文件名>
```

文件目录在首次导入时创建。备份需要同时复制数据库和文件目录。现有 Docker PTC 的 `runs/` 仍是独立目录，尚未自动归档到 Workspace。

所有会话和文件操作都要求提供所属 Workspace ID。使用另一个 Workspace ID 读取会话、写消息或获取文件路径会失败；`show` 也只列出本 Workspace 的内容。这是单用户应用的数据归属检查；同一操作系统用户仍可直接访问磁盘文件。

## 6. 自动验收与当前范围

```bash
uv run python -m unittest discover -s tests -v
```

测试使用临时数据目录，每次操作均启动新的 CLI 进程，覆盖：

- 重新打开后恢复目标、偏好、假设、旧消息和文件，新会话可继续建立。
- 跨 Workspace 读取会话、写消息、取文件均被拒绝，修改偏好不影响其他研究。
- 同名文件重复导入保留不同副本，删除原文件后副本仍可读取。
- 非法偏好、空会话标题、缺失文件、目录输入或不存在的 Workspace 不产生有效记录。
- 已登记文件丢失时明确失败。

当前只保存初始假设，尚无假设修订版本、Agent 上下文读取、自动报告、Run 管理或调度。下一批接入 Agent 时，再验证它实际利用已有研究继续分析。

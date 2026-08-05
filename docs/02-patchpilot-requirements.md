# PatchPilot：可评测仓库级 Coding Agent 需求文档

> 文档版本：v1.0  
> 项目性质：个人实习作品 / Agent 执行与评测框架  
> 主要目标：展示 Agent Loop、结构化工具调用、受控代码执行、测试反馈、质量门禁、成本治理与 Benchmark 能力

## 1. 项目概述

PatchPilot 是一个面向小型 Python 和 Go 仓库的自动代码修复 Agent。用户提供代码仓库、Issue 描述、允许修改的文件范围、验收命令和执行预算，系统创建隔离工作区，让模型通过结构化工具搜索代码、读取文件、提交补丁和运行测试。模型可以根据测试失败结果继续迭代，直到通过质量门禁、达到预算上限或主动结束。

每次运行都必须保存完整的事件轨迹、工具调用、Token、费用、Git Patch、测试结果和最终 Scorecard。项目还必须提供一套可重复运行的本地 Benchmark，用于比较单轮生成、Agent Loop、代码检索、测试反馈和上下文压缩等策略。

项目重点不是制作 IDE 插件，也不是复刻 Cursor，而是构建一个“小而完整、可以验证”的 Coding Agent Harness。

## 2. 项目目标

### 2.1 业务目标

1. 用户可以提交一个本地仓库修复任务。
2. Agent 可以自主读取代码、搜索符号、修改文件并运行测试。
3. 测试失败后，Agent 可以读取失败信息并继续修复。
4. 执行过程必须受到路径、命令、时间、Token 和费用限制。
5. 任务结束后生成 Patch、测试报告、事件轨迹和 Scorecard。
6. 开发者可以批量运行 Benchmark，并比较不同 Agent 策略。

### 2.2 技术目标

1. 自行实现清晰可解释的 Agent Loop，而非把全部控制权交给框架。
2. 使用 JSON Schema/Pydantic 定义严格的工具协议。
3. 模型层兼容 OpenAI-compatible API。
4. 使用临时工作区和 Docker 实现受控执行。
5. 使用 SQLite 作为 CLI 默认数据库，服务端模式支持 PostgreSQL。
6. 使用 Redis 和异步 Worker 执行长任务。
7. 自动化测试默认使用 Scripted/Fake Model，不调用真实付费 API。
8. 所有 Benchmark 结果可复现、可汇总、可审计。

## 3. 非目标

首版不实现：

- 不做 IDE 插件和实时代码补全。
- 不构建完整云端开发环境。
- 不支持任意编程语言，首版支持 Python，P1 增加 Go。
- 不实现通用浏览器或网页操作。
- 不允许模型执行任意 Shell。
- 不实现多 Agent 协作；首版使用单 Agent Loop。
- 不直接挑战完整 SWE-bench；使用自建、可本地运行的小型 Benchmark。
- 不自动推送 GitHub、创建 PR 或修改用户原始仓库。
- 不把模型生成的代码视为可信内容；任何通过状态必须来自自动验收。

## 4. 需求优先级

- **P0**：形成可运行 Coding Agent 闭环所必需。
- **P1**：达到高质量实习项目标准所必需。
- **P2**：扩展和研究性质功能。

## 5. 核心概念

### 5.1 Task

用户定义的修复任务，包含仓库来源、目标描述、可修改范围、验收命令和预算。

### 5.2 Run

一次具体执行。一个 Task 可以用不同模型或策略产生多个 Run。

### 5.3 Workspace

每个 Run 对应的独立仓库副本。Agent 不得修改原始仓库。

### 5.4 Tool Call

模型输出的结构化工具请求及工具执行结果。

### 5.5 Quality Gate

Agent 申请完成后执行的一组确定性检查，包括 Patch、测试、修改范围、预算和安全检查。

### 5.6 Benchmark

由多个固定 Task 和标准答案/验收测试组成的评测集合。

### 5.7 Strategy

控制 Agent 行为的实验配置，例如：

- `single_shot`
- `agent_loop`
- `agent_loop_with_tests`
- `agent_loop_with_search`
- `full`

## 6. TaskSpec 协议

TaskSpec 使用 YAML 或 JSON。协议必须进行版本控制。

### 6.1 示例

```yaml
version: "1"
id: "go-pagination-001"
title: "修复 page=0 时返回空列表的问题"

repository:
  path: "./benchmarks/repos/go-pagination"
  base_ref: "main"
  language: "go"

goal: |
  GET /items?page=0&size=10 应将 page=0 视为第一页，
  返回前 10 条数据，而不是空列表。

allowed_paths:
  - "internal/service/**"
  - "internal/service/**/*_test.go"

denied_paths:
  - ".git/**"
  - ".github/**"
  - "vendor/**"

acceptance:
  commands:
    - argv: ["go", "test", "./..."]
      timeout_seconds: 120
  required_tests:
    - "TestListItemsPageZero"

budget:
  max_steps: 15
  max_input_tokens: 80000
  max_output_tokens: 16000
  max_cost_usd: 0.20
  max_wall_time_seconds: 600
  max_changed_files: 5
  max_patch_lines: 300

execution:
  network: false
  cpu_limit: 2
  memory_limit_mb: 1024

metadata:
  difficulty: "easy"
  tags: ["go", "pagination", "boundary"]
```

### 6.2 校验规则

- `version`、`id`、`repository`、`goal`、`acceptance` 和 `budget` 必填。
- Task ID 在一个 Benchmark 内唯一。
- 仓库必须存在且包含 Git 元数据，或由 Benchmark 初始化脚本创建。
- `allowed_paths` 至少包含一项。
- `.git/**` 永远禁止修改，用户配置不能覆盖。
- 验收命令必须匹配语言 Profile 中的命令白名单。
- 所有数值预算必须大于 0，并设置系统级硬上限。
- 用户级预算不能超过系统级硬上限。

## 7. Run 状态机

```text
pending
   ↓
preparing
   ↓
running
   ├──→ passed
   ├──→ failed
   ├──→ timeout
   ├──→ budget_exceeded
   ├──→ cancelled
   └──→ system_error
```

### 状态规则

- `POST /runs` 创建后立即返回 `run_id`，不得同步等待 Agent 完成。
- 只有 Worker 可以将 `pending` 改为 `preparing` 或 `running`。
- 终态不可重新进入 `running`。
- 取消采用协作式取消；正在执行的子进程必须被终止。
- Worker 崩溃后，超时未更新的 Run 可以由恢复任务标记失败或重新入队。
- 同一 `Idempotency-Key + task_id + strategy + model` 不得创建重复 Run。

## 8. Agent Loop

### 8.1 P0 流程

1. 加载并校验 TaskSpec。
2. 创建独立 Workspace。
3. 记录基准 Commit 和初始 Git 状态。
4. 收集仓库摘要：
   - 文件树
   - 语言
   - README/贡献规则
   - 可用测试命令
5. 构建 System Prompt、Task Prompt 和工具 Schema。
6. 调用模型。
7. 解析模型工具调用。
8. 执行一个或多个允许的工具。
9. 将结构化工具结果追加到上下文。
10. 检查步数、Token、费用和总时间预算。
11. 重复步骤 6—10，直到模型调用 `finish` 或预算终止。
12. 执行 Quality Gate。
13. 生成 Patch、事件轨迹、报告和 Scorecard。
14. 清理或按配置保留 Workspace。

### 8.2 Agent 决策规则

- 模型每轮只能输出自然语言消息或已注册的结构化工具调用。
- 无法解析的工具调用不得猜测执行，应返回 `INVALID_TOOL_CALL` 给模型。
- 未注册工具必须拒绝。
- 相同无效工具调用连续出现 3 次时终止为 `failed`。
- `finish` 只表示“申请验收”，不代表任务通过。
- Quality Gate 失败后：
  - 若仍有预算，允许将失败摘要返回 Agent 继续修复。
  - 最多允许 2 次 Gate 失败回流，避免无限循环。
- 达到任一硬预算后必须停止，不允许模型自行扩大预算。

### 8.3 P1：上下文管理

上下文由以下部分组成：

1. 不可压缩的 System Prompt 和安全规则。
2. TaskSpec 摘要。
3. Repository Map。
4. 最近若干轮完整消息。
5. 早期事件压缩摘要。
6. 当前 Git Diff 和最近测试失败摘要。

要求：

- 工具返回必须设置最大字符数。
- 超长文件优先按行范围读取。
- 测试输出保留失败部分、堆栈和摘要，截断重复日志。
- 接近上下文阈值时调用独立的压缩流程。
- 压缩不得删除 Task 目标、允许路径、预算、未解决测试失败和已修改文件列表。
- 保存压缩前后 Token 估计和摘要内容，便于评测。

### 8.4 P2：规划

- Agent 可先生成只读计划，再进入修改阶段。
- 支持要求用户批准计划，但不作为首版必需功能。
- 不实现多 Agent。

## 9. 工具协议

所有工具输入使用 Pydantic 模型，输出为统一结构：

```json
{
  "ok": true,
  "tool": "read_file",
  "summary": "读取 src/service.py 第 1-120 行",
  "data": {},
  "error": null,
  "truncated": false,
  "duration_ms": 12
}
```

失败：

```json
{
  "ok": false,
  "tool": "edit_file",
  "summary": "拒绝修改",
  "data": null,
  "error": {
    "code": "PATH_NOT_ALLOWED",
    "message": "目标文件不在 allowed_paths 中"
  },
  "truncated": false,
  "duration_ms": 1
}
```

### 9.1 `list_files`

输入：

- `path`：相对 Workspace 根目录。
- `max_depth`：1—5。

要求：

- 忽略 `.git`、缓存、构建产物和依赖目录。
- 最多返回固定数量文件。
- 所有路径使用 `/` 作为逻辑分隔符。

### 9.2 `search_code`

输入：

- `query`
- `path` 可选
- `glob` 可选
- `max_results`

要求：

- 优先使用 ripgrep。
- 返回文件、行号和有限上下文。
- 禁止使用模型构造任意 Shell 字符串。
- 无结果不是系统错误。

### 9.3 `read_file`

输入：

- `path`
- `start_line`
- `end_line`

要求：

- 路径必须位于 Workspace。
- 默认最多返回 400 行。
- 二进制文件拒绝读取。
- 超限时返回 `truncated=true`。
- 输出包含行号。

### 9.4 `inspect_symbol`

P1 功能。

输入：

- `symbol`
- `path` 可选

首版允许通过文本和语言级简单解析查找函数、类、方法定义。不得把完整 LSP 作为 P0 前置条件。

### 9.5 `apply_patch`

输入：

- `patch`：统一 diff。

要求：

- Patch 中所有路径都必须位于 Workspace。
- 修改前检查 `allowed_paths` 和 `denied_paths`。
- 禁止文件重命名和二进制 Patch，首版只支持文本创建、更新、删除。
- Patch 应原子应用；部分失败时不得留下半应用状态。
- 应用后返回修改文件和行数统计。
- Patch 超过剩余预算时拒绝。

### 9.6 `git_diff`

输入：

- `path` 可选。
- `stat_only`。

输出：

- 统一 Diff 或统计摘要。
- 变更文件数、增加行、删除行。

不得允许模型直接执行任意 Git 子命令。

### 9.7 `run_tests`

输入：

- `profile_command_id`，从 TaskSpec 验收命令或语言 Profile 选择。
- 可选的受限 test selector。

要求：

- 命令以 argv 数组执行，禁止 `shell=True`。
- 设置超时、CPU、内存和输出上限。
- 捕获 stdout、stderr、退出码和耗时。
- 超时后终止整个进程树或容器。
- 网络默认关闭。
- 返回结构化测试摘要和截断日志。

### 9.8 `run_linter`

P1 功能。规则同 `run_tests`，仅能选择预配置命令。

### 9.9 `finish`

输入：

- `summary`
- `tests_run`
- `remaining_risks`

调用后触发 Quality Gate。模型不能直接设置 Run 为 `passed`。

## 10. 语言 Profile

语言 Profile 定义允许命令和默认忽略目录。

### 10.1 Python P0

允许：

- `python -m pytest`
- `python -m pytest <受限选择器>`
- P1：`ruff check`
- P1：`mypy`，仅任务明确配置时

忽略：

- `.venv`
- `venv`
- `__pycache__`
- `.pytest_cache`
- `.mypy_cache`
- `.ruff_cache`
- `dist`
- `build`

### 10.2 Go P1

允许：

- `go test ./...`
- `go test <受限包路径>`
- `go vet ./...`
- `gofmt` 只作用于已修改的 `.go` 文件

忽略：

- `vendor`
- `bin`
- `dist`
- `coverage`

### 10.3 命令安全

- Profile 由应用代码定义，不从仓库内可修改文件动态加载。
- TaskSpec 只能引用或收窄 Profile，不能扩展系统允许命令。
- 禁止 `bash -c`、`sh -c`、`cmd /c` 和 PowerShell 字符串执行。
- 禁止包安装、网络下载、Git push 和凭据读取。
- Benchmark 仓库依赖必须在镜像构建或任务准备阶段预置。

## 11. Workspace 与执行隔离

### 11.1 P0：本地 Workspace

- 将基准仓库复制或 `git worktree` 到应用管理的临时根目录。
- 解析后校验 Workspace 绝对路径位于允许的运行目录。
- 每次工具调用都重新校验目标路径，禁止仅在创建时校验。
- 禁止跟随指向 Workspace 外部的符号链接。
- 运行前工作区必须干净。
- 原始仓库只读，不得被修改。

本地模式只允许执行项目自带的可信 Benchmark，不用于运行未知第三方代码。

### 11.2 P1：Docker Sandbox

- 每个 Run 使用独立容器。
- 默认禁用网络。
- 文件系统除 Workspace 外尽可能只读。
- 使用非 root 用户。
- 设置 CPU、内存、PID 和总时间限制。
- 不挂载 Docker Socket。
- 不挂载宿主机凭据、SSH目录和环境 Secret。
- 容器结束后销毁。
- 模型 API 调用由宿主 Worker 完成，API Key 不进入执行容器。

### 11.3 Secret 与日志

- API Key 只从环境变量读取。
- 模型请求日志不得记录 Authorization Header。
- 仓库文件可能包含伪 Secret；报告和日志输出需限制长度。
- 不主动扫描或上传 `.env`、私钥、凭据文件内容。
- Repository Map 默认忽略常见 Secret 文件。

## 12. 模型适配

定义模型接口：

```python
class ModelClient(Protocol):
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        config: ModelConfig,
    ) -> ModelResponse: ...
```

`ModelResponse` 至少包含：

- 文本消息。
- 工具调用列表。
- finish reason。
- prompt_tokens。
- completion_tokens。
- total_tokens。
- model。
- provider_request_id。
- latency_ms。

### P0

- OpenAI-compatible Chat Completions。
- 配置 Base URL、API Key、模型名、Temperature、Max Tokens。
- 工具调用兼容 JSON Schema。
- 对缺少 usage 的提供商允许使用估算值，但必须标记 `estimated=true`。

### P1

- 429、5xx、超时使用带抖动的指数退避。
- 最大重试 3 次。
- 相同请求重试需记录每次 Attempt。
- 支持模型价格配置。
- 支持多个模型配置，但每个 Run 固定一个模型。
- 记录模型和 Prompt 版本，保证 Benchmark 可追溯。

### Fake/Scripted Model

必须提供不调用网络的测试模型：

- 按预设顺序返回工具调用。
- 可模拟无效 JSON、未知工具、429 和超时。
- 可断言收到的消息和工具结果。
- 用于覆盖 Agent Loop 所有状态和错误分支。

## 13. Quality Gate

### 13.1 执行顺序

1. 检查 Workspace 是否产生 Patch。
2. 检查修改文件是否全部匹配 `allowed_paths` 且不匹配 `denied_paths`。
3. 检查变更文件数量和 Patch 行数预算。
4. 检查工作区是否存在未允许的未跟踪文件。
5. 执行格式化或静态检查（若配置）。
6. 执行全部 acceptance commands。
7. 检查 required tests 是否实际执行。
8. 收集最终 Diff 和测试结果。
9. 生成 Scorecard。

### 13.2 结果分类

通过：

- `PASSED`

失败：

- `NO_PATCH`
- `TEST_FAILURE`
- `REGRESSION`
- `SCOPE_VIOLATION`
- `PATCH_TOO_LARGE`
- `REQUIRED_TEST_NOT_RUN`
- `TIMEOUT`
- `BUDGET_EXCEEDED`
- `INVALID_TOOL_LOOP`
- `MODEL_ERROR`
- `TOOL_ERROR`
- `SANDBOX_ERROR`
- `CANCELLED`
- `SYSTEM_ERROR`

分类必须由确定性代码决定，不允许让模型自行选择。

### 13.3 回流

首次 Gate 失败且仍有预算时，可向 Agent 返回：

- 失败分类。
- 失败测试名称。
- 关键错误输出。
- 当前 Diff 摘要。
- 剩余预算。

最多回流 2 次。`SCOPE_VIOLATION`、`SANDBOX_ERROR` 和硬预算超限默认不回流。

## 14. 事件与追踪

### 14.1 事件类型

- `run.created`
- `workspace.preparing`
- `workspace.ready`
- `model.requested`
- `model.responded`
- `model.retrying`
- `tool.requested`
- `tool.started`
- `tool.completed`
- `tool.failed`
- `context.compacted`
- `quality_gate.started`
- `quality_gate.failed`
- `quality_gate.passed`
- `run.completed`
- `run.cancelled`
- `run.failed`

### 14.2 JSONL

每个 Run 输出 `events.jsonl`。每行包含：

```json
{
  "schema_version": "1",
  "event_id": "uuid",
  "run_id": "uuid",
  "sequence": 12,
  "type": "tool.completed",
  "timestamp": "2026-08-06T10:00:00Z",
  "duration_ms": 42,
  "payload": {}
}
```

要求：

- `sequence` 单调递增。
- 事件写入尽可能追加式。
- 数据库事件与 JSONL 事件语义一致。
- 大型 stdout、文件内容和模型响应存入 Artifact，只在事件中保存引用和摘要。

## 15. 数据模型

### 15.1 tasks

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 主键 |
| external_id | varchar | TaskSpec ID |
| title | varchar | 标题 |
| task_spec | json | 完整协议快照 |
| spec_version | varchar | 版本 |
| created_at | datetime | 创建时间 |

### 15.2 runs

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 主键 |
| task_id | UUID | 任务 |
| status | varchar | 状态 |
| strategy | varchar | 策略 |
| model | varchar | 模型 |
| prompt_version | varchar | Prompt版本 |
| idempotency_key | varchar nullable | 幂等键 |
| workspace_id | varchar nullable | Workspace标识 |
| step_count | int | Agent步数 |
| prompt_tokens | int | 输入Token |
| completion_tokens | int | 输出Token |
| estimated_cost_usd | numeric | 费用 |
| result_code | varchar nullable | Scorecard结果 |
| error_code | varchar nullable | 系统错误 |
| started_at | datetime nullable | 开始 |
| finished_at | datetime nullable | 结束 |
| created_at | datetime | 创建 |

### 15.3 events

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 主键 |
| run_id | UUID | Run |
| sequence | int | 序号 |
| event_type | varchar | 类型 |
| payload | json | 小型事件数据 |
| duration_ms | int nullable | 耗时 |
| created_at | datetime | 时间 |

唯一约束：`run_id + sequence`。

### 15.4 model_calls

保存模型、请求序号、Attempt、Token、费用、耗时、finish reason、provider request ID、状态和错误码。

### 15.5 tool_calls

保存工具名、输入摘要、输出摘要、状态、错误码、耗时和对应 Artifact。

### 15.6 artifacts

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 主键 |
| run_id | UUID | Run |
| kind | varchar | patch/test_log/report/scorecard/event_log |
| path | varchar | 内部存储路径 |
| sha256 | varchar | 摘要 |
| size_bytes | bigint | 大小 |
| created_at | datetime | 时间 |

## 16. CLI

CLI 命令使用 Typer。

```text
patchpilot task validate task.yaml
patchpilot run task.yaml --model deepseek-chat --strategy full
patchpilot run show RUN_ID
patchpilot run cancel RUN_ID
patchpilot benchmark validate benchmarks/demo
patchpilot benchmark run benchmarks/demo --strategy full
patchpilot benchmark compare RESULT_A RESULT_B
patchpilot report build RUN_ID
```

### CLI要求

- `run` 默认以前台模式执行，方便开发。
- `--json` 输出机器可读结果。
- 非成功结果使用非零退出码。
- CLI 与服务端复用同一领域逻辑，不得复制一套 Agent Loop。
- CLI 默认 SQLite，数据和 Artifact 存放目录可配置。

## 17. HTTP API

服务端采用 FastAPI，统一前缀 `/api/v1`。

### 17.1 Tasks

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tasks` | 创建并校验Task |
| GET | `/tasks` | 任务列表 |
| GET | `/tasks/{id}` | 任务详情 |

### 17.2 Runs

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/runs` | 异步创建Run |
| GET | `/runs` | Run列表 |
| GET | `/runs/{id}` | Run详情 |
| POST | `/runs/{id}/cancel` | 取消 |
| GET | `/runs/{id}/events` | 分页事件 |
| GET | `/runs/{id}/stream` | SSE实时事件 |
| GET | `/runs/{id}/patch` | Patch |
| GET | `/runs/{id}/scorecard` | Scorecard |
| GET | `/runs/{id}/report` | HTML/JSON报告 |

### 17.3 Metrics

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/metrics/summary` | 成功率、成本、耗时 |
| GET | `/health/live` | 存活检查 |
| GET | `/health/ready` | 就绪检查 |
| GET | `/metrics` | Prometheus |

### API规则

- `POST /runs` 支持 `Idempotency-Key`。
- SSE 事件与持久化事件使用相同类型。
- Artifact 下载必须校验 Run 权限；首版可采用单用户模式，但接口层保留 owner 字段。
- 错误响应包含稳定错误码和 `request_id`，不得返回内部堆栈。

## 18. Benchmark

### 18.1 规模

简历发布前至少包含 20 个任务：

- Python 至少 12 个。
- Go 至少 8 个。
- easy、medium 至少各 5 个。
- 至少覆盖 6 类缺陷。

推荐缺陷类别：

- 边界条件。
- 空值与异常处理。
- 分页/排序。
- JSON/API兼容。
- 缓存失效。
- SQL条件遗漏。
- 并发状态。
- 文件路径处理。
- 回归测试补充。

### 18.2 Benchmark目录

```text
benchmarks/
└── local-v1/
    ├── benchmark.yaml
    ├── tasks/
    │   ├── py-001.yaml
    │   └── go-001.yaml
    ├── repos/
    │   ├── py-001/
    │   └── go-001/
    ├── hidden_tests/
    └── README.md
```

为避免 Agent 直接读取答案，隐藏验收测试应在 Quality Gate 阶段注入 Workspace，默认不出现在 Agent 可读取目录中。

### 18.3 Benchmark 指标

- `pass_rate`。
- `first_gate_pass_rate`。
- `average_steps`。
- `average_model_calls`。
- `average_tool_calls`。
- `average_prompt_tokens`。
- `average_completion_tokens`。
- `average_cost_usd`。
- `average_wall_time_seconds`。
- `scope_violation_rate`。
- `regression_rate`。
- 按语言、难度、缺陷类型分类的通过率。

### 18.4 必须比较的策略

1. `single_shot`：一次性生成 Patch，不使用测试反馈。
2. `agent_loop`：允许工具循环，但不向模型返回测试失败。
3. `agent_loop_with_tests`：增加测试反馈。
4. `full`：代码搜索、测试反馈、上下文压缩和 Quality Gate。

必须固定：

- Task集合。
- 模型和模型参数。
- Prompt版本。
- 最大预算。
- 重复运行次数。

由于模型存在随机性，最终报告推荐每个任务运行 3 次，至少报告一次完整运行和局限性。

### 18.5 报告

输出：

- 原始 JSONL。
- 汇总 JSON。
- Markdown 报告。
- HTML 可视化报告。

报告必须包含：

- 实验配置。
- 总体和分类指标。
- 成本/通过率对比。
- 至少 3 个成功案例。
- 至少 3 个失败案例。
- 失败原因分析。
- 不能据此得出的结论。

## 19. Scorecard

每个 Run 输出：

```json
{
  "schema_version": "1",
  "run_id": "uuid",
  "task_id": "go-pagination-001",
  "result": "PASSED",
  "checks": {
    "has_patch": true,
    "scope_valid": true,
    "patch_size_valid": true,
    "tests_passed": true,
    "required_tests_ran": true,
    "budget_valid": true
  },
  "metrics": {
    "steps": 8,
    "model_calls": 8,
    "tool_calls": 13,
    "changed_files": 2,
    "added_lines": 18,
    "deleted_lines": 4,
    "prompt_tokens": 14000,
    "completion_tokens": 2100,
    "estimated_cost_usd": 0.012,
    "wall_time_seconds": 96
  },
  "artifacts": {
    "patch": "artifacts/final.patch",
    "events": "artifacts/events.jsonl",
    "test_log": "artifacts/test.log",
    "report": "artifacts/report.html"
  }
}
```

## 20. 推荐系统架构

```text
Typer CLI ───────────────┐
                        │
Web / API Client ─ FastAPI
                        │
                        ▼
                 Task & Run Service
                        │
                  Redis Task Queue
                        │
                        ▼
                    Run Worker
        ┌───────────────┼────────────────┐
        │               │                │
        ▼               ▼                ▼
   Agent Loop      Model Adapter    Event Recorder
        │               │                │
        ▼               ▼                ▼
  Tool Registry   OpenAI-compatible   DB / JSONL
        │
        ▼
 Docker Sandbox / Trusted Local Workspace
        │
        ├── Search / Read
        ├── Apply Patch
        └── Test / Lint
```

采用模块化单体。CLI、API 和 Worker 共享核心包；不得分别实现三套执行逻辑。

## 21. 推荐目录结构

```text
patchpilot/
├── src/patchpilot/
│   ├── cli/
│   ├── api/
│   ├── worker/
│   ├── domain/
│   │   ├── task.py
│   │   ├── run.py
│   │   ├── events.py
│   │   └── scorecard.py
│   ├── agent/
│   │   ├── loop.py
│   │   ├── context.py
│   │   ├── prompts.py
│   │   └── strategies.py
│   ├── tools/
│   │   ├── base.py
│   │   ├── files.py
│   │   ├── search.py
│   │   ├── patch.py
│   │   ├── git.py
│   │   └── tests.py
│   ├── sandbox/
│   │   ├── workspace.py
│   │   ├── local.py
│   │   └── docker.py
│   ├── models/
│   │   ├── base.py
│   │   ├── openai_compatible.py
│   │   └── fake.py
│   ├── quality/
│   ├── benchmark/
│   ├── persistence/
│   ├── reporting/
│   └── observability/
├── migrations/
├── benchmarks/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── fixtures/
│   └── scripted_runs/
├── web/
├── docker/
├── artifacts/
├── pyproject.toml
├── docker-compose.yml
├── Makefile
├── .env.example
└── README.md
```

## 22. 非功能需求

### 22.1 安全

- 所有路径在使用前必须解析并验证位于 Workspace。
- 禁止通过符号链接逃逸。
- 所有子进程使用 argv，禁止 `shell=True`。
- 模型不能覆盖系统工具白名单。
- 未知代码只允许在 Docker Sandbox 中执行。
- 容器默认无网络、非 root、有限资源。
- 原始仓库保持只读和不变。
- API Key 不进入 Workspace 和容器。

### 22.2 可靠性

- 每个外部模型调用有超时。
- 每个工具有独立超时。
- 子进程超时后清理完整进程树。
- Run 终态和 Artifact 写入具有幂等性。
- Worker 重启不能将同一 Run 并行执行两次。
- Artifact 使用 SHA-256 校验。

### 22.3 性能

- API 创建 Run 在 500 ms 内返回，不含队列依赖异常。
- 事件查询分页。
- 大型日志和文件内容不得直接保存到数据库事件字段。
- 模型上下文和工具输出设置硬上限。
- Benchmark 支持可配置并发，但同一任务的多次运行相互隔离。

### 22.4 可观测性

- JSON结构化日志。
- `request_id`、`task_id`、`run_id`、`event_id` 全链路关联。
- Prometheus至少包含：
  - Run数量和状态。
  - Run耗时。
  - 模型请求、错误和重试。
  - Token和成本。
  - 工具调用及错误。
  - Sandbox启动失败。
  - Benchmark通过率。

## 23. 配置

`.env.example` 至少包含：

```dotenv
APP_ENV=development
DATABASE_URL=sqlite+aiosqlite:///./data/patchpilot.db
POSTGRES_DATABASE_URL=postgresql+asyncpg://patchpilot:patchpilot@postgres:5432/patchpilot
REDIS_URL=redis://redis:6379/0
ARTIFACT_ROOT=./artifacts
WORKSPACE_ROOT=./workspaces

MODEL_BASE_URL=
MODEL_API_KEY=
MODEL_NAME=
MODEL_TEMPERATURE=0
MODEL_MAX_TOKENS=4096

DEFAULT_MAX_STEPS=15
HARD_MAX_STEPS=30
HARD_MAX_COST_USD=1.00
HARD_MAX_WALL_TIME_SECONDS=1800
TOOL_OUTPUT_MAX_CHARS=20000
CONTEXT_MAX_TOKENS=64000

SANDBOX_MODE=local
SANDBOX_IMAGE_PYTHON=patchpilot-python:latest
SANDBOX_IMAGE_GO=patchpilot-go:latest
LOG_LEVEL=INFO
```

启动时必须校验目录、数据库、Redis和模型配置。CLI 使用 Fake Model 或仅校验 TaskSpec 时，不要求配置真实 API Key。

## 24. 测试要求

### 24.1 单元测试

必须覆盖：

- TaskSpec 校验。
- Run 状态转换。
- glob允许/拒绝路径匹配。
- 路径穿越和符号链接逃逸。
- 工具 Schema 和无效调用。
- Patch行数和文件数限制。
- Token、费用和步数预算。
- 模型重试。
- 上下文压缩不丢失关键约束。
- Quality Gate结果分类。
- Scorecard生成。

### 24.2 集成测试

必须覆盖：

- Scripted Model 完成一次成功修复。
- 测试失败反馈后第二次修复成功。
- `finish` 后 Gate 失败并回流。
- 修改白名单外文件被拒绝。
- Patch超限被拒绝。
- 命令超时后进程被终止。
- 达到步数或费用预算后停止。
- 取消Run。
- Idempotency-Key防止重复提交。
- JSONL事件顺序和数据库一致。
- Artifact生成和摘要校验。

### 24.3 Docker测试

P1至少覆盖：

- 容器无网络。
- 容器无法访问宿主Secret。
- CPU/内存/超时限制生效。
- Workspace外文件不可写。
- 容器结束后被清理。

### 24.4 真实模型测试

- 默认CI禁止真实模型调用。
- 真实测试通过显式环境变量开启。
- 设置极低预算。
- 测试结果不得作为确定性单元测试断言。

## 25. 前端最低要求

前端为 P1，重点是展示执行过程：

- Task创建和TaskSpec预览。
- Run列表、状态和取消。
- 实时事件时间线。
- 模型调用和工具调用详情。
- 当前Git Diff。
- 测试结果。
- Token、费用、步骤和耗时。
- 最终Scorecard。
- Benchmark策略对比图表。

前端不得允许用户绕过后端白名单提交任意 Shell 命令。

## 26. 交付物

- CLI、API和Worker源代码。
- SQLite/PostgreSQL迁移。
- Docker Sandbox镜像。
- Docker Compose。
- TaskSpec JSON Schema。
- OpenAPI文档。
- Fake/Scripted Model。
- 单元、集成和Sandbox测试。
- 至少20个本地Benchmark任务。
- JSONL轨迹、Patch、Scorecard、Markdown和HTML报告样例。
- README：
  - 问题背景
  - Agent Loop
  - 工具协议
  - 安全边界
  - Quality Gate
  - Benchmark方法
  - 实验结果
  - 快速启动
  - 已知限制
- 3分钟以内演示视频脚本或录屏。

## 27. Codex 实施约束

当 Codex 根据本文档生成代码时，必须遵循：

1. 先实现 Python P0 CLI 垂直切片，再增加 API、Worker、Docker 和 Go Profile。
2. Agent Loop 必须由项目自身代码清楚实现，不得仅包装第三方 Agent 框架。
3. 任何执行命令都必须使用参数数组，不得使用 `shell=True`。
4. 不得以“后续实现”替代路径校验、预算、Quality Gate和测试反馈。
5. 自动化测试必须使用 Fake/Scripted Model，不得依赖真实API。
6. 不得修改用户原始仓库；所有运行使用独立Workspace。
7. P0阶段不得为了界面推迟Agent闭环、测试和Benchmark。
8. 不得记录真实API Key、Authorization Header或完整Secret文件。
9. 每个里程碑结束后必须运行格式化、lint、类型检查和测试。
10. 每次只实施一个里程碑，并报告变更文件、命令、测试结果、安全影响和剩余事项。

## 28. 实施里程碑

### M0：协议和工程骨架

- `pyproject.toml`、Typer CLI、配置和日志。
- TaskSpec Pydantic模型与JSON Schema。
- Run领域模型和状态机。
- SQLite迁移。

验收：可以校验合法/非法TaskSpec，并创建持久化Run记录。

### M1：受控本地工具

- Workspace复制。
- 路径校验。
- list/search/read/apply_patch/git_diff/run_tests。
- Python语言Profile。

验收：不使用模型即可通过测试调用工具修改Fixture仓库并运行pytest；越权路径和命令被拒绝。

### M2：Agent Loop

- Model接口。
- OpenAI-compatible Client。
- Fake/Scripted Model。
- 工具调用循环。
- 步数、Token、费用和时间预算。
- JSONL事件。

验收：Scripted Model可以完成一次“读取—修改—测试—finish”流程。

### M3：Quality Gate与报告

- 完整Gate。
- Gate失败回流。
- Patch、测试日志和Scorecard。
- Markdown/HTML报告。

验收：成功、测试失败、越权、无Patch、超时和预算超限均有自动化测试。

### M4：服务化

- FastAPI。
- PostgreSQL。
- Redis异步Worker。
- Idempotency-Key。
- SSE事件和取消。

验收：API提交后立即返回run_id，Worker异步执行并可实时查看事件。

### M5：Docker Sandbox与Go

- Docker Sandbox。
- Python镜像。
- Go语言Profile和镜像。
- 资源、网络和Secret隔离测试。

验收：未知Fixture代码只能在无网络容器中运行；Python和Go任务均可验收。

### M6：Benchmark

- 至少20个任务。
- 四种策略。
- 汇总指标和对比报告。
- 成功/失败案例分析。

验收：一条命令运行Benchmark，并生成JSON、Markdown和HTML结果。

### M7：发布准备

- 前端或完整报告页。
- CI。
- README、架构图、演示任务和视频。
- 清理Secret、临时Workspace和大文件。

验收：新环境根据README可以在30分钟内运行一个Fake任务，并在配置API Key后运行一个真实任务。

## 29. 最终验收清单

- [ ] TaskSpec有版本、Schema和严格校验。
- [ ] Agent使用结构化工具调用而不是解析随意文本命令。
- [ ] 支持搜索、读取、应用Patch、Diff、测试和Finish。
- [ ] 测试失败可以回流给Agent继续修复。
- [ ] Workspace不会修改原始仓库。
- [ ] 路径逃逸、符号链接逃逸和越权修改被阻止。
- [ ] 子进程不使用`shell=True`。
- [ ] 步数、Token、费用、Patch和时间预算生效。
- [ ] `finish`必须经过Quality Gate。
- [ ] 结果分类由确定性代码生成。
- [ ] 每个Run产生Patch、事件、测试日志、Scorecard和报告。
- [ ] CLI可独立完成一次运行。
- [ ] API和Worker可异步运行并通过SSE观察。
- [ ] Docker Sandbox默认无网络、非root并限制资源。
- [ ] 自动化测试使用Fake/Scripted Model。
- [ ] 至少20个Python/Go Benchmark任务。
- [ ] 至少比较四种Agent策略。
- [ ] 报告包含通过率、成本、Token、耗时和失败分析。
- [ ] Docker Compose、OpenAPI和README完整。
- [ ] 仓库不存在真实Secret和Benchmark答案泄漏。

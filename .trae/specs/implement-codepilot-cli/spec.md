# CodePilot 编码智能体 CLI Spec

## Why
构建一个名为 CodePilot 的终端编码智能体 CLI 工具，让用户在终端中通过自然语言与 LLM 交互完成代码读写、命令执行、项目管理等任务。需支持 DeepSeek（OpenAI 兼容格式）和 Anthropic Claude 两种后端，并具备严格的安全沙箱与自动上下文压缩能力，避免误操作破坏用户系统。

## What Changes
- 新建 Python 3.11+ 项目 `codepilot/`，包含入口 `main.py` 与完整模块结构
- 实现配置系统：支持 `.codepilot.yml`、环境变量、CLI 参数三级优先级
- 实现 Provider 抽象层：统一 DeepSeek（OpenAI SDK 兼容）与 Anthropic（原生 Messages API）的流式响应与工具调用
- 实现 7 个核心工具：`read_file` / `write_file` / `edit_file` / `list_files` / `shell_exec` / `search_code` / `get_context`
- 实现安全系统：目录沙箱、命令黑白名单过滤、人工审批流程
- 实现上下文管理：token 计数、消息历史管理、自动压缩（summary/truncate/hybrid 三策略）
- 实现 Rich 终端 UI：启动 banner、流式输出、工具调用面板、diff 着色、slash 命令
- 实现核心 Agent 循环：tool-use agentic loop，单轮最多 25 次工具调用
- **BREAKING**：无（全新项目）

## Impact
- Affected specs: 无（新建项目）
- Affected code: 全部新建于 `d:\Project\编码智能体CLI\codepilot\` 目录下
- 依赖：openai>=1.40.0、anthropic>=0.40.0、rich>=13.7.0、prompt_toolkit>=3.0.0、pyyaml>=6.0、tiktoken>=0.7.0

## ADDED Requirements

### Requirement: 项目结构
系统 SHALL 按照 PRD 第一节定义的目录结构组织代码，包含 `main.py`、`config.py`、`providers/`、`tools/`、`security/`、`context/`、`ui/`、`agent/` 等模块，每个模块均有 `__init__.py`。

### Requirement: 配置系统
系统 SHALL 支持从以下来源加载配置，优先级从高到低：
1. 命令行参数（`--provider`、`--model`、`--api-key`、`--workspace`、`--no-approve`、`--config`、`--verbose`）
2. 环境变量（`CODEPILOT_PROVIDER`、`DEEPSEEK_API_KEY`、`ANTHROPIC_API_KEY`）
3. 当前目录 `.codepilot.yml`
4. 用户目录 `~/.config/codepilot/config.yml`
5. 程序内置默认值

配置 SHALL 包含 provider、security、context、ui 四大段，字段定义见 PRD 2.1。配置文件中 `${ENV_VAR}` 形式的值 SHALL 被环境变量替换。

#### Scenario: 配置加载
- **WHEN** 用户在当前目录放置 `.codepilot.yml` 并设置 `provider: deepseek`
- **AND** 通过 `--provider anthropic` 命令行参数覆盖
- **THEN** 系统使用 anthropic 作为 provider

#### Scenario: 环境变量引用
- **WHEN** 配置文件中 `api_key: "${DEEPSEEK_API_KEY}"`
- **AND** 环境变量 `DEEPSEEK_API_KEY` 已设置
- **THEN** 系统将该环境变量值作为 api_key

### Requirement: Provider 抽象层
系统 SHALL 提供 `BaseProvider` 抽象基类，定义 `async chat(messages, tools, stream=True) -> AsyncIterator[AgentEvent]` 方法。`AgentEvent` SHALL 包含五种类型：`TextDelta`、`ThinkingDelta`、`ToolCall(id, name, arguments)`、`Usage(input_tokens, output_tokens)`、`Done(stop_reason)`。

#### Scenario: DeepSeek 流式响应
- **WHEN** 使用 DeepSeek provider 发送消息
- **THEN** 通过 openai SDK 以 `base_url=https://api.deepseek.com` 调用 `/chat/completions`
- **AND** 流式返回 `TextDelta` 事件
- **AND** 工具调用以 OpenAI `tool_calls` 格式解析为 `ToolCall` 事件

#### Scenario: Anthropic 工具调用
- **WHEN** 使用 Anthropic provider 且模型决定调用工具
- **THEN** 响应 `stop_reason == "tool_use"`
- **AND** `content` 中的 `tool_use` block 被解析为 `ToolCall` 事件（input 已是 dict，无需 JSON.parse）
- **AND** 工具结果以 `role="user"` + `tool_result` block 形式回传

### Requirement: 工具系统
系统 SHALL 实现以下 7 个工具，每个工具经过 sandbox 路径校验后执行，危险操作经 approval 确认，返回结构化字符串给 LLM，并对输出做截断处理：

| 工具名 | 功能 |
|---|---|
| `read_file` | 读取文件内容（带行号），超过 100KB 截断 |
| `write_file` | 创建或覆写文件，显示 diff 预览，需确认 |
| `edit_file` | 搜索替换方式编辑文件局部内容 |
| `list_files` | 递归列出目录树，可配置深度和过滤 |
| `shell_exec` | 在 workspace 中执行终端命令，超时 30s，输出截断 200 行 |
| `search_code` | 在代码库中搜索字符串/正则（grep 风格） |
| `get_context` | 获取当前上下文使用统计信息 |

#### Scenario: 工具注册表
- **WHEN** 系统启动
- **THEN** `tools/registry.py` 注册全部 7 个工具
- **AND** 能将工具定义转换为 DeepSeek（OpenAI function 格式）和 Anthropic（原生 input_schema 格式）两种格式

#### Scenario: shell_exec 安全限制
- **WHEN** 调用 `shell_exec`
- **THEN** 命令在 `workspace_root` 目录下以 subprocess 执行
- **AND** 设置 30s 超时
- **AND** 禁止交互式命令（vim/nano/less 等）
- **AND** 输出截断到 200 行
- **AND** 返回 exit_code、stdout、stderr、truncated、duration_ms

### Requirement: 安全系统
系统 SHALL 实现目录沙箱、命令过滤、人工审批三层安全防护。

#### Scenario: 路径校验
- **WHEN** 工具尝试访问路径
- **THEN** `Sandbox.validate_path` 将路径 resolve 为绝对路径
- **AND** 检查路径遍历攻击（`../`、符号链接）
- **AND** 检查 blocked_paths（精确 + 前缀匹配）
- **AND** 检查是否在 workspace_root 或 allowed_dirs 内
- **AND** 对 write 操作额外检查 `.git/`、`.codepilot.yml` 等敏感文件
- **AND** 返回 `(is_safe, reason)`

#### Scenario: 命令校验
- **WHEN** 调用 `shell_exec`
- **THEN** `Sandbox.validate_command` 拆解链式命令（`|`、`&&`、`||`、`;`）
- **AND** 检查黑名单关键字（`rm -rf /`、`mkfs`、`dd if=`、`shutdown` 等）
- **AND** 白名单模式下检查命令前缀
- **AND** 禁止 `sudo`/`su` 和交互式命令

#### Scenario: 人工审批
- **WHEN** 操作类型在 `require_approval_for` 列表中（file_write/file_edit/shell_exec）
- **THEN** 显示操作详情面板（file 操作显示彩色 diff，shell_exec 高亮危险部分）
- **AND** 用户可选 `y`（本次批准）/ `n`（拒绝）/ `a`（本会话自动批准同类）/ `!`（YOLO 模式）

### Requirement: 上下文管理
系统 SHALL 实现 token 计数、消息历史管理、自动压缩三层上下文管理。

#### Scenario: Token 计数
- **WHEN** 安装了 tiktoken
- **THEN** 使用 `cl100k_base` 编码器精确计数
- **WHEN** 未安装 tiktoken
- **THEN** 使用字符数 / 3.5 粗略估算

#### Scenario: 自动压缩触发
- **WHEN** `total_tokens / max_tokens >= compression_threshold`（默认 0.70）
- **THEN** 触发压缩
- **AND** 保留 system_prompt + 最近 `preserve_recent_turns` 轮对话（默认 4）
- **AND** 其余历史消息按 `compression_strategy` 处理
- **WHEN** 达到 `critical_threshold`（默认 0.85）
- **THEN** 强制压缩

#### Scenario: summary 压缩策略
- **WHEN** strategy 为 `summary`
- **THEN** 调用 LLM 生成结构化摘要（CONTEXT/KEY ACTIONS/OUTCOMES/CURRENT STATE/IMPORTANT REFERENCES）
- **AND** 保留所有文件路径、函数/类/变量名、错误信息、设计决策
- **AND** 用摘要替换可压缩区
- **AND** 原始历史写入 `history_file`
- **AND** 终端显示压缩通知（前→后 token 数、缩减比例）

### Requirement: Agent 循环
系统 SHALL 实现 `agent/loop.py` 中的 agentic tool-use 循环。

#### Scenario: 单轮工具循环
- **WHEN** 用户输入消息
- **THEN** 将消息加入 context_manager
- **AND** 循环调用 provider.chat 获取响应
- **AND** 流式输出文本到终端
- **AND** 若响应包含 tool_calls，对每个 tool_call 执行：UI 显示→安全校验→审批确认→执行→显示结果→加入 context
- **AND** 继续循环让模型处理工具结果
- **AND** 单轮最多 25 次工具调用
- **WHEN** 响应为纯文本（stop_reason 非 tool_use）
- **THEN** 加入 context 并回到用户输入

### Requirement: 终端 UI
系统 SHALL 使用 rich 库实现终端 UI，包括启动 banner、交互显示、安全拒绝面板、压缩通知、slash 命令。

#### Scenario: 启动 banner
- **WHEN** 启动 `python main.py`
- **THEN** 显示 ASCII art banner、版本号、provider、workspace、安全状态、上下文配置、可用 slash 命令

#### Scenario: 交互显示要素
- **WHEN** 用户输入消息
- **THEN** 显示用户输入面板
- **AND** 每个工具调用显示工具名、参数、结果（截断）
- **AND** 需审批时显示 diff 预览和确认提示
- **AND** assistant 回复显示在面板中
- **AND** 底部显示 token 用量、上下文占比、费用估算

#### Scenario: Slash 命令
- **WHEN** 用户输入 `/help`、`/config`、`/stats`、`/clear`、`/compact`、`/history`、`/model <name>`、`/provider <p>`、`/approve`、`/undo`、`/quit`、`/exit`
- **THEN** 执行对应命令
- **AND** Ctrl+C 中断当前操作回到提示符，Ctrl+D 退出

### Requirement: CLI 入口
系统 SHALL 通过 `main.py` 提供 CLI 入口，支持交互模式（默认）和单次执行模式（传入参数）。

#### Scenario: 交互模式
- **WHEN** 运行 `python main.py`
- **THEN** 进入交互式 REPL

#### Scenario: 单次执行模式
- **WHEN** 运行 `python main.py "fix the bug in main.py"`
- **THEN** 执行单次任务后退出

### Requirement: 关键实现约束
- 所有文件 I/O 使用 async（aiofiles 或线程池）
- LLM 响应必须流式显示
- 网络错误、API 错误、工具执行错误都要优雅处理，不能 crash
- Ctrl+C 中断当前 LLM 调用，回到输入提示符
- 文件读写统一 UTF-8，二进制文件跳过
- 路径统一用 `os.path.realpath()` 解析后比较
- 读取文件超过 100KB 自动截断并告知 LLM
- async 下注意 context_manager 线程安全

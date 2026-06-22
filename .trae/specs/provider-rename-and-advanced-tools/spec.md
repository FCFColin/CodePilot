# Provider 命名泛化 + 多 API 配置 + 高级工具集 + 复杂场景测试 Spec

## Why
1. 当前配置命名（`DeepSeekConfig`、`CODEPILOT_DEEPSEEK__*`）暗示只能用 DeepSeek，但实际支持任何 OpenAI 兼容端点，命名具有误导性
2. 当前只支持两个固定 provider，不支持配置多个 API 端点并在执行时选取特定 API
3. 缺少网页抓取、错误收集等高级工具，无法完成复杂编码任务
4. 缺少循环检测、plan/spec 等智能体核心能力

## What Changes
- **BREAKING**: 重构配置系统，支持多 API 端点配置
  - 新增 `providers` 配置段：用户可定义任意数量的 API 端点（如 `deepseek`、`xunfei`、`openai_official` 等）
  - 每个 provider 指定 `type: openai | anthropic`（API 协议格式）
  - `provider` 字段指定默认使用的 provider 名称
  - CLI `--provider <name>` 选取特定 provider
  - 保留旧 `deepseek:` / `anthropic:` 配置段作为向后兼容别名
- 新增 `web_fetch` 工具：抓取网页内容并转为 Markdown
- 新增 `diagnose` 工具：收集错误信息（运行 linter、读取 traceback、检查文件状态）
- 新增 `plan` 工具：创建/更新执行计划（结构化 JSON）
- 新增循环检测机制：检测重复工具调用模式并自动中断
- 新增多步撤销：`/undo` 支持连续撤销多步操作
- 新增 `/rollback` slash 命令：回退到指定对话轮次
- 新增 `/plan` slash 命令：查看/编辑当前执行计划
- 新增 `/providers` slash 命令：列出所有已配置的 provider

## Impact
- Affected specs: provider 配置系统、工具系统、agent loop、slash 命令
- Affected code: `config.py`、`cli.py`、`providers/`、`app.py`、`agent/loop.py`、`tools/`、`.codepilot.yml.example`

## ADDED Requirements

### Requirement: 多 Provider 配置
系统 SHALL 支持在配置文件中定义任意数量的 API 端点，每个端点指定协议类型（openai/anthropic）、base_url、model、api_key 等。

#### Scenario: 配置多个 API 端点
- **WHEN** 用户在 `.codepilot.yml` 中定义多个 provider：
  ```yaml
  provider: xunfei  # 默认 provider
  providers:
    xunfei:
      type: openai
      api_key: "${XUNFEI_API_KEY}"
      base_url: "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2"
      model: "astron-code-latest"
    deepseek:
      type: openai
      api_key: "${DEEPSEEK_API_KEY}"
      base_url: "https://api.deepseek.com"
      model: "deepseek-chat"
    claude:
      type: anthropic
      api_key: "${ANTHROPIC_API_KEY}"
      model: "claude-sonnet-4-20250514"
  ```
- **THEN** 系统根据 `provider` 字段选择默认端点，也可通过 `--provider` CLI 参数切换

#### Scenario: CLI 切换 provider
- **WHEN** 用户执行 `codepilot --provider deepseek`
- **THEN** 系统使用名为 `deepseek` 的 provider 配置

#### Scenario: 向后兼容旧配置
- **WHEN** 用户使用旧格式（`deepseek:` 和 `anthropic:` 顶层键，无 `providers:` 段）
- **THEN** 系统自动将旧格式转换为新格式，发出 deprecation 警告

#### Scenario: 环境变量
- **WHEN** 用户设置 `CODEPILOT_PROVIDERS__XUNFEI__API_KEY=xxx`
- **THEN** 系统正确解析为 `providers.xunfei.api_key`

### Requirement: Provider 类型抽象
系统 SHALL 将 provider 配置分为两种协议类型：`openai`（OpenAI 兼容）和 `anthropic`（Anthropic 兼容），而非绑定特定服务商。

#### Scenario: 使用 OpenAI 兼容端点
- **WHEN** 用户配置 `type: openai` 并设置任意 `base_url`
- **THEN** 系统使用 OpenAI SDK 连接该端点

#### Scenario: 使用 Anthropic 兼容端点
- **WHEN** 用户配置 `type: anthropic` 并设置任意 `base_url`
- **THEN** 系统使用 Anthropic SDK 连接该端点

### Requirement: 网页抓取工具 (web_fetch)
系统 SHALL 提供 `web_fetch` 工具，抓取指定 URL 的内容并转为 Markdown 格式。

#### Scenario: 抓取网页
- **WHEN** LLM 调用 `web_fetch` 并传入 URL
- **THEN** 工具抓取网页内容，转为 Markdown 返回，超时 15 秒，最大 50KB

#### Scenario: URL 不可达
- **WHEN** URL 无法访问
- **THEN** 返回错误信息而非抛异常

### Requirement: 错误诊断工具 (diagnose)
系统 SHALL 提供 `diagnose` 工具，收集项目错误信息。

#### Scenario: 诊断项目错误
- **WHEN** LLM 调用 `diagnose` 并传入错误描述和文件路径
- **THEN** 工具运行 linter、读取相关 traceback、检查文件状态，返回结构化诊断报告

### Requirement: 执行计划工具 (plan)
系统 SHALL 提供 `plan` 工具，创建和更新结构化执行计划。

#### Scenario: 创建计划
- **WHEN** LLM 调用 `plan` 并传入步骤列表
- **THEN** 工具保存计划到内存，返回确认信息

#### Scenario: 更新计划进度
- **WHEN** LLM 调用 `plan` 并标记步骤完成
- **THEN** 工具更新计划状态

### Requirement: 循环检测机制
系统 SHALL 检测 Agent Loop 中的重复工具调用模式并自动中断。

#### Scenario: 检测重复调用
- **WHEN** 连续 3 次调用相同工具且参数相似度 > 80%
- **THEN** 系统中断循环，向 LLM 发送提示信息要求换策略

### Requirement: 多步撤销
系统 SHALL 支持连续撤销多步操作。

#### Scenario: 连续撤销
- **WHEN** 用户执行 `/undo` 多次
- **THEN** 每次撤销一步操作，直到栈为空

### Requirement: /rollback 命令
系统 SHALL 提供 `/rollback` 命令回退到指定对话轮次。

#### Scenario: 回退到指定轮次
- **WHEN** 用户执行 `/rollback 3`
- **THEN** 系统删除第 3 轮之后的所有对话和文件变更

### Requirement: /plan 命令
系统 SHALL 提供 `/plan` 命令查看当前执行计划。

#### Scenario: 查看计划
- **WHEN** 用户执行 `/plan`
- **THEN** 显示当前执行计划及各步骤状态

### Requirement: /providers 命令
系统 SHALL 提供 `/providers` 命令列出所有已配置的 provider。

#### Scenario: 列出 providers
- **WHEN** 用户执行 `/providers`
- **THEN** 显示所有已配置的 provider 名称、类型、base_url、model，标记当前活跃的 provider

## MODIFIED Requirements

### Requirement: CLI --provider 参数
`--provider` 参数接受任意已配置的 provider 名称（不再限于 deepseek/anthropic）。旧值 `deepseek`/`anthropic` 作为内置别名仍可使用。

### Requirement: YAML 配置格式
新增 `providers:` 顶层键，支持自定义任意数量的 provider。旧的 `deepseek:` 和 `anthropic:` 顶层键作为别名保留，自动转换为 `providers.deepseek` 和 `providers.anthropic`。

## REMOVED Requirements

### Requirement: 固定双 Provider 配置
**Reason**: 只支持两个固定 provider（deepseek/anthropic）无法满足多 API 端点需求
**Migration**: `DeepSeekConfig`/`AnthropicConfig` → `ProviderConfig`（含 `type: openai|anthropic`），`Config.deepseek`/`Config.anthropic` → `Config.providers: dict[str, ProviderConfig]`

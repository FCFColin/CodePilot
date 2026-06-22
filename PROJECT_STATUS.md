# CodePilot CLI 项目现状总结

> 文档生成时间：2026-06-22 | 版本：0.2.0 | 仓库：FCFColin/CodePilot

---

## 一、项目概述

CodePilot 是一个生产级终端 AI 编码智能体 CLI，使用 Python 3.11+ 开发。用户在终端中与 LLM 协作完成代码阅读、编写、编辑、搜索与命令执行，内置安全沙箱、审批机制与自动上下文压缩。

**核心能力**：Agentic tool-use 循环 — 用户输入 → LLM 流式响应 → 工具调用执行 → 结果回传 → 继续生成，直到 LLM 不再请求工具调用。

**当前规模**：47 个源文件，10,688 行代码；25+ 个测试文件，9,500+ 行测试代码；598 个测试全部通过。

---

## 二、架构总览

```
src/codepilot/
├── cli.py              (118行)  CLI 入口，argparse 参数解析
├── app.py              (952行)  应用组合根，REPL 主循环 + slash 命令
├── config.py           (635行)  Pydantic v2 BaseSettings 配置系统
├── exceptions.py        (25行)  自定义异常体系
│
├── agent/
│   └── loop.py         (793行)  核心 agentic tool-use 循环
│
├── providers/
│   ├── base.py         (261行)  BaseProvider ABC + AgentEvent 流式事件类型
│   ├── openai_compat.py(375行)  OpenAI 兼容 Provider（讯飞/DeepSeek/任意兼容端点）
│   └── anthropic.py    (352行)  Anthropic 原生 Messages API Provider
│
├── tools/
│   ├── registry.py     (314行)  工具注册表 + BaseTool ABC
│   ├── file_read.py    (147行)  读取文件（带行号，自动跳过二进制）
│   ├── file_write.py   (170行)  写入文件（diff 预览 + 审批）
│   ├── file_edit.py    (193行)  搜索替换编辑（diff 预览 + 审批）
│   ├── list_files.py   (196行)  递归目录树
│   ├── shell_exec.py   (207行)  Shell 命令执行（超时+截断+安全过滤）
│   ├── search_code.py  (259行)  正则代码搜索（grep 风格）
│   ├── web_fetch.py    (147行)  网页抓取→Markdown 转换
│   ├── diagnose.py     (225行)  错误诊断 + linter 检查
│   └── plan_tool.py    (197行)  结构化执行计划管理
│
├── security/
│   ├── sandbox.py      (319行)  路径沙箱校验（越界/符号链接逃逸防护）
│   ├── command_filter.py(155行) 命令四重检查（黑名单/交互式/白名单/提权）
│   └── approval.py     (256行)  用户审批（y/n/a/! + YOLO 模式）
│
├── context/
│   ├── manager.py      (350行)  上下文管理器（消息历史 + 压缩触发）
│   ├── compressor.py   (426行)  自动上下文压缩器（summary/truncate/hybrid）
│   └── token_counter.py(225行)  Token 计数（tiktoken 优先，字符估算兜底）
│
├── session/
│   ├── manager.py      (222行)  会话生命周期管理
│   ├── storage.py      (156行)  JSON 文件持久化存储
│   └── export.py       (132行)  Markdown/JSON 格式导出
│
├── git/
│   ├── manager.py      (276行)  Git 仓库管理（自动提交/撤销/脏文件检测）
│   └── commit.py       (161行)  提交消息生成
│
├── hooks/
│   ├── registry.py     (179行)  Hook 注册表
│   └── builtin.py      (480行)  内置 Hook（LintHook + GitCommitHook）
│
├── repomap/
│   └── mapper.py       (600行)  仓库结构摘要（tree-sitter，可选依赖）
│
└── ui/
    ├── display.py      (747行)  Rich 终端 UI 渲染（流式面板/工具调用/状态栏）
    ├── diff_view.py    (135行)  文件变更 diff 着色
    └── banner.py       (117行)  启动 banner + 状态信息
```

---

## 三、核心模块功能说明

### 3.1 配置系统 (config.py)

**多 Provider 字典式配置**，不再绑定特定厂商：

```python
class ProviderConfig(BaseModel):
    type: Literal["openai", "anthropic"] = "openai"
    api_key: SecretStr = SecretStr("")
    base_url: str = ""
    model: str = ""
    max_tokens: int = 8192
    temperature: float = 0.7
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)

class Config(BaseSettings):
    providers: dict[str, ProviderConfig]  # 任意多个 provider
    provider: str = "xunfei"              # 当前使用的 provider
```

**内置默认 Provider**：
- `xunfei`：讯飞星辰（astron-code-latest，OpenAI 兼容端点）
- `deepseek`：DeepSeek（deepseek-reasoner，OpenAI 兼容端点）

**四级配置优先级**：命令行参数 > 环境变量 > YAML 配置文件 > 默认值

**关键环境变量**：
- `CODEPILOT_API_KEY`：便捷变量，覆盖当前 provider 的 api_key
- `CODEPILOT_PROVIDER`：切换当前 provider
- `CODEPILOT_PROVIDERS__<NAME>__API_KEY`：嵌套环境变量

### 3.2 Agent 循环 (agent/loop.py)

核心 agentic tool-use 循环实现：
- 流式响应处理（TextDelta / ThinkingDelta / ToolCall / Usage / Done 五种事件）
- 工具调用执行 → 结果回传 → 继续生成
- 单轮最多 25 次工具调用（防无限循环）
- **循环检测**：使用 difflib.SequenceMatcher 检测重复工具调用模式
- **会话记录**：记录用户消息、assistant 回复（含工具调用摘要）、工具结果
- **中断支持**：cancel() 方法设置标志使循环在下次检查时退出

### 3.3 Provider 适配层

| Provider | 文件 | SDK | 特性 |
|----------|------|-----|------|
| OpenAI 兼容 | openai_compat.py | openai | 流式响应、工具调用、深度思考模式、tenacity 重试、stream_options 降级 |
| Anthropic | anthropic.py | anthropic | 原生 Messages API、content-block 架构、思考过程、tenacity 重试 |

**统一抽象**：BaseProvider ABC 定义 `chat()` 异步方法，返回 `AsyncIterator[AgentEvent]`，两种 Provider 将各自格式统一转换为 AgentEvent 流。

### 3.4 工具系统

**10 个工具**（PRD 要求 7 个 + 新增 3 个高级工具）：

| 工具 | 功能 | 安全措施 |
|------|------|----------|
| read_file | 读取文件内容（带行号） | 沙箱路径校验，>100KB 截断 |
| write_file | 创建/覆写文件 | 沙箱校验 + diff 预览 + 用户审批 |
| edit_file | 搜索替换编辑 | 沙箱校验 + diff 预览 + 用户审批 |
| list_files | 递归目录树 | 沙箱路径校验 |
| shell_exec | 执行 Shell 命令 | 命令过滤 + 用户审批 + 超时(30s) + 输出截断 |
| search_code | 正则代码搜索 | 沙箱路径校验 |
| get_context | 上下文使用统计 | 无危险操作 |
| **web_fetch** | 抓取网页→Markdown | httpx + markdownify，50KB 截断 |
| **diagnose** | 错误诊断 + linter | asyncio 子进程执行 ruff |
| **plan_tool** | 结构化执行计划 | 类级别 _current_plan 存储 |

### 3.5 安全系统

三层安全防护：
1. **Sandbox** (sandbox.py)：路径校验 — 越界检测、符号链接逃逸防护、敏感文件保护
2. **CommandFilter** (command_filter.py)：四重检查 — 黑名单/交互式/白名单/提权
3. **ApprovalManager** (approval.py)：用户审批 — y(本次)/n(拒绝)/a(本会话自动)/!(YOLO 模式)

### 3.6 上下文管理

- **TokenCounter**：tiktoken cl100k_base 优先，字符/3.5 估算兜底
- **ContextManager**：消息历史管理，自动压缩触发
- **ContextCompressor**：三种策略（summary/truncate/hybrid），70% 触发压缩，85% 强制压缩

### 3.7 会话系统

- **SessionManager**：会话生命周期，记录消息/工具调用/thinking/Token 用量
- **SessionStorage**：JSON 文件持久化到 `~/.codepilot/sessions/`
- **SessionExporter**：导出 Markdown（含元数据表 + 工具调用汇总 + 完整对话历史）或 JSON

### 3.8 Git 集成

- **GitManager**：自动提交（`[codepilot]` 前缀）、撤销最近提交、脏文件检测
- **UndoTracker**：文件操作撤销追踪，支持 /undo 单步撤销和 /rollback 按轮次回退（含文件内容恢复）

### 3.9 UI 显示

- **DisplayManager**：Rich 终端 UI，流式面板渲染、工具调用/结果面板、Token 用量状态栏
- **非 TTY 模式**：检测管道输出，累积文本后一次性打印完整面板（避免碎片化）
- **Thinking 累加器**：在 Live 面板内显示思考过程，而非每条 delta 创建独立面板

---

## 四、Slash 命令

| 命令 | 功能 |
|------|------|
| /help | 显示帮助信息 |
| /config | 显示当前配置 |
| /clear | 清空对话历史 |
| /compact | 手动触发上下文压缩 |
| /stats | 显示统计信息 |
| /undo | 撤销最近一次文件操作 |
| /rollback | 按轮次回退（含文件内容恢复） |
| /plan | 查看当前执行计划 |
| /providers | 列出所有已配置的 Provider |
| /history | 显示对话历史概要 |
| /quit / /exit | 退出 |

---

## 五、已验证的端到端测试成果

### 5.1 简单网页生成（DeepSeek）

使用 DeepSeek API 成功生成 `test_website/index.html`，验证了基础 tool-use 循环。

### 5.2 Flask 应用生成（讯飞星辰）

使用讯飞星辰 API 成功生成 `flask_demo/` 目录（app.py + requirements.txt + templates/index.html），验证了多文件创建能力。

### 5.3 复杂多页面网站（讯飞星辰，核心测试）

使用讯飞星辰 API 成功完成以下复杂任务：

**用户指令**：创建多页面个人作品集网站，要求使用 plan 工具制定计划、web_fetch 访问 Tailwind CSS 官网、diagnose 检查代码质量。

**执行过程**（23 次工具调用）：
1. plan 工具创建 6 步执行计划
2. web_fetch 访问 https://tailwindcss.com 获取 CSS 框架参考
3. write_file 创建 styles.css（720 行，14KB）
4. write_file 创建 index.html（211 行，8KB）
5. write_file 创建 projects.html（283 行，13KB）
6. write_file 创建 about.html（314 行，14KB）
7. diagnose 检查所有 4 个文件的代码质量
8. list_files 验证文件结构
9. plan 工具逐步更新步骤状态（pending → in_progress → completed）

**生成成果**：
```
portfolio_site/
├── styles.css        (720 行 - 深色主题 + 毛玻璃导航栏 + 响应式)
├── index.html        (211 行 - Hero + 精选项目 + 统计数字)
├── projects.html     (283 行 - 8个项目卡片 + 分类筛选)
└── about.html        (314 行 - 技能展示 + 时间线 + 联系CTA)
```

**对话日志质量**：完整的 23KB Markdown 日志，包含元数据表、工具调用汇总表（含参数摘要和结果摘要）、完整对话历史（assistant 消息有具体内容，工具消息用代码块展示）。

---

## 六、已解决的关键问题

### 6.1 讯飞星辰 API 401 认证错误

**根因**：环境变量 `CODEPILOT_DEEPSEEK__BASE_URL` 从之前测试中持久化，覆盖了 YAML 配置。

**解决**：清除环境变量，同时在新配置系统中使用 `CODEPILOT_PROVIDERS__XUNFEI__API_KEY` 等新格式。

### 6.2 流式输出碎片化

**根因**：`on_thinking_delta` 每条 delta 调用 `_stop_live()` 创建独立 Panel；`on_usage` 在非 TTY 模式下每次触发打印。

**解决**：
- 添加 `_current_thinking` 累加器，在 Live 面板内显示思考过程
- 非 TTY 模式：累积文本，`_stop_live` 时一次性打印完整面板
- `on_usage` 延迟到 `on_turn_end` 处理

### 6.3 对话日志空条目

**根因**：assistant 消息只有工具调用没有文本时，`accumulated_text` 为空，导致记录空字符串。

**解决**：当 `accumulated_text` 为空但有工具调用时，记录工具调用摘要：
```python
tool_summary = ", ".join(f"{tc.name}({', '.join(...)})" for tc in tool_calls)
self._record_session_message("assistant", f"[调用工具: {tool_summary}]")
```

### 6.4 配置系统从固定双 Provider 到灵活字典

**旧架构**：`DeepSeekConfig` + `AnthropicConfig` 两个固定类，只支持两家 API。

**新架构**：`ProviderConfig` 通用类 + `providers: dict[str, ProviderConfig]` 字典，支持任意数量、任意兼容格式的 API 端点。完全移除了旧代码，无 deprecation 警告。

---

## 七、测试覆盖

| 指标 | 数值 |
|------|------|
| 总测试数 | 598 passed, 1 skipped |
| 单元测试 | 17+ 个文件（含 UndoTracker、压缩策略、Provider 差异测试） |
| 集成测试 | 4 个文件（含 agent_loop 完整流程测试） |
| E2E 测试 | 2 个文件（15 个 CLI 子进程测试，覆盖所有 slash 命令） |
| 测试代码行数 | 9,500+ 行 |

**E2E 测试覆盖**：--version、--help、管道 /quit 退出、无 API Key 失败、python -m 一致性、/help、/config、/stats、/providers、/plan、/model、/provider、/approve、/rollback、/undo、/export、/clear、/unknown 命令。

**单元测试新增**：
- UndoTracker 多轮回退测试（3 轮文件操作后回退到第 1 轮）
- 上下文压缩策略测试（summary/truncate/preserve_recent_turns/force_compress/token 减少）
- Provider 差异测试（工具定义格式、stream_options 降级、content-block 架构、ToolCall 解析）

---

## 八、依赖

**核心依赖**：openai>=1.40.0, anthropic>=0.40.0, rich>=13.7.0, prompt_toolkit>=3.0.0, pyyaml>=6.0, tiktoken>=0.7.0, pydantic>=2.0, pydantic-settings>=2.0, structlog>=24.0, tenacity>=8.0, httpx>=0.27.0, markdownify>=0.13.0

**可选依赖**：tree-sitter-language-pack>=0.2, networkx>=3.0（repomap 功能）

**开发依赖**：pytest>=8.0, pytest-cov>=4.0, pytest-asyncio>=0.23, respx>=0.20, mypy>=1.8, ruff>=0.3, pre-commit>=3.6

---

## 九、Git 提交历史

```
55baca3 feat: multi-provider refactor, enhanced logging, detailed conversation logs, file rollback
3eb6015 [codepilot] add: portfolio_site/about.html
eec040a [codepilot] add: portfolio_site/projects.html
e6b44fa [codepilot] add: portfolio_site/index.html
aa227aa [codepilot] add: portfolio_site/styles.css
ed6739b feat: multi-provider config + web_fetch/diagnose/plan tools + loop detection + slash commands
edf56a3 [codepilot] add: flask_demo/templates/index.html
1f1bf8d [codepilot] add: flask_demo/app.py
c43a783 [codepilot] add: flask_demo/requirements.txt
ff03297 fix: streaming fragmentation + stream_options fallback + non-TTY display
6f4636e fix: streaming fragmentation + conversation logging enhancement
67712ff [codepilot] add: test_website/index.html
3fae40f feat: production-grade improvements - Git integration, session persistence, lint feedback loop, repo map
857fd89 [codepilot] add: hello_codepilot.txt
27a7cd4 initial snapshot for pre-commit
```

---

## 十、待改进项

1. ~~**复杂多轮测试不足**~~ → 已解决：添加 9 个 E2E slash 命令测试 + UndoTracker 多轮回退测试
2. ~~**/rollback 文件回退**~~ → 已解决：添加多轮回退测试验证
3. ~~**上下文压缩**~~ → 已解决：添加 5 个压缩策略测试 + force_compress 测试
4. ~~**PRD 与实际代码的差异**~~ → 已解决：/model、/provider、/approve 命令均已实现
5. ~~**repomap 可选功能**~~ → 已有 skipif 机制，tree-sitter 不可用时自动跳过
6. ~~**流式输出兼容性**~~ → 已解决：添加 10 个 Provider 差异测试（stream_options 降级、content-block 架构、ToolCall 解析）
7. ~~**日志导出路径**~~ → 已解决：日志导出到 ~/.codepilot/logs/，/export 导出到 ~/.codepilot/exports/

**当前仍需关注的改进方向**：
- 真实 API 长对话场景下的上下文压缩效果验证
- 更多 LLM 后端（如 GPT-4、Claude）的实际兼容性测试
- /rollback 在复杂嵌套文件操作场景中的边界情况

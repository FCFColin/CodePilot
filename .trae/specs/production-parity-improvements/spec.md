# CodePilot 生产级增量改进 Spec

## Why
CodePilot 已完成基础架构（src layout、Pydantic v2 配置、7 个工具、Agent 循环、324 个测试、mypy --strict 通过、86% 覆盖率），但存在 3 个工程基建问题（pre-commit 空壳、make test 无 clean、CI 缺格式检查），且缺少对标 Aider/gptme 的核心生产能力：Git 深度集成、会话持久化、Lint 反馈循环、Repo Map。本次增量改进在不动现有架构的前提下补齐这些能力，使 CodePilot 达到生产水准。

## What Changes
- 修复 pre-commit 空壳问题：运行 `pre-commit run --all-files` 修复 13 个格式问题，Makefile test 目标首行加入 pre-commit 钩子
- 修复 make test 无 clean 问题：Makefile test 目标首条命令清理 `__pycache__`
- 修复 CI 矩阵缺格式检查问题：`.github/workflows/ci.yml` lint Job 同时运行 `ruff check` 和 `ruff format --check`
- 新增 `src/codepilot/git/` 包：`GitManager`（init/auto_commit/undo_last_commit/get_dirty_files/is_git_repo）、`CommitMessageGenerator`（rules + llm 两种生成模式）
- 新增 `src/codepilot/session/` 包：`SessionStorage`（JSON 持久化，0o700 权限）、`SessionManager`（会话生命周期）、`SessionExporter`（markdown/json 导出）
- 新增 `src/codepilot/hooks/` 包：`HookRegistry`、`HookEvent` 枚举、`BaseHook` 抽象基类、`LintHook`（ruff/eslint/gofmt 自动 lint）、`GitCommitHook`
- 新增 `src/codepilot/repomap/` 包（可选，依赖 tree-sitter-language-pack）：`RepoMapper`（tree-sitter 解析 + networkx PageRank 排序 + SQLite 缓存）
- 配置新增 `GitConfig`、`HooksConfig`、`RepoMapConfig` 三个 Pydantic 子模型
- CLI 新增 `--no-auto-commit`、`-c/--continue`、`-r/--resume SESSION_ID` 标志
- slash 命令新增 `/sessions`、`/export [markdown|json]`；`/undo` 改为优先走 git 回滚，失败回退内存 UndoTracker
- AgentLoop 集成 HookRegistry（lint 重试循环，MAX_LINT_RETRIES=3）和 RepoMapper（系统提示注入仓库摘要）
- pyproject.toml 新增可选依赖组 `repomap`（tree-sitter-language-pack、networkx）
- 新增测试文件：`tests/unit/test_git.py`、`tests/unit/test_session.py`、`tests/unit/test_hooks.py`、`tests/unit/test_repomap.py`、`tests/integration/test_git_integration.py`

## Impact
- Affected specs: `production-grade-refactor`（在其完成的架构上增量扩展，不破坏现有约束）
- Affected code:
  - `Makefile`、`.pre-commit-config.yaml`、`.github/workflows/ci.yml`（工程基建修复）
  - `src/codepilot/app.py`（初始化 GitManager、SessionManager、HookRegistry、RepoMapper 并注入 AgentLoop）
  - `src/codepilot/agent/loop.py`（集成 hook 触发与 lint 重试循环、repo_map 注入系统提示）
  - `src/codepilot/cli.py`（新增 CLI 标志与 slash 命令路由）
  - `src/codepilot/config.py`（新增三个子配置模型）
  - `src/codepilot/ui/display.py`（show_sessions、lint 重试计数展示）
  - 新增 `src/codepilot/git/`、`src/codepilot/session/`、`src/codepilot/hooks/`、`src/codepilot/repomap/` 四个包
- 新增依赖：`tree-sitter-language-pack`、`networkx`（均作为 `[project.optional-dependencies]` 的 `repomap` 可选组，非必装）

## ADDED Requirements

### Requirement: 工程基建修复（Phase 1，前置必做）
系统 SHALL 在进入任何新功能前完成三项基建修复，每项修复后立即运行完整测试套件确认 0 回归。

#### Scenario: pre-commit 钩子实际运行
- **WHEN** 执行 `pre-commit run --all-files`
- **THEN** 全部钩子输出 `Passed`，无 `Failed`
- **AND** Makefile 的 `test` 目标首行为 `pre-commit run --all-files`

#### Scenario: make test 清理 pycache
- **WHEN** 执行 `make test`
- **THEN** 首条命令为 `find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true`
- **AND** 随后执行 pytest，无 stale .pyc 导致的 AttributeError

#### Scenario: CI 格式检查独立执行
- **WHEN** CI lint Job 运行
- **THEN** 同时执行 `ruff check src/ tests/` 和 `ruff format --check src/ tests/` 两条命令
- **AND** 任一失败则 lint Job 失败

### Requirement: Git 深度集成（Phase 2）
系统 SHALL 在 `src/codepilot/git/` 下提供 `GitManager` 和 `CommitMessageGenerator`，通过依赖注入接入 App，禁止 monkey-patch 全局状态。

`GitManager` SHALL：
- `init(workspace_root: Path)` 调用 `_detect_repo()` 检测 git 仓库，不在仓库则 `self.repo = None`
- `_detect_repo()` 使用 `subprocess.run(['git', 'rev-parse', '--show-toplevel'], ...)` 静默失败
- `is_git_repo() -> bool`
- `auto_commit(message: str, paths: list[Path]) -> str | None`：git add 指定路径后 git commit，提交信息前缀强制 `[codepilot]`（缺失则自动补），返回 8 位 hash，非 git 仓库返回 None
- `undo_last_commit() -> tuple[bool, str]`：检查 `git log --oneline -1` 是否 `[codepilot]` 开头，是则 `git reset --soft HEAD~1` 返回 `(True, msg)`，否则 `(False, '最近一次提交不是 codepilot 提交，拒绝回滚')`
- `get_dirty_files() -> list[Path]`：解析 `git status --porcelain`
- 所有 git 操作在非 git 仓库中 SHALL 静默失败返回 None/False，禁止抛异常

`CommitMessageGenerator` SHALL：
- `generate(diff_summary: str, max_length: int = 72) -> str`：纯规则生成，格式 `[codepilot] <action>: <file1>, <file2>`，超长截断加 `...`
- `generate_from_llm(provider: BaseProvider, diff_summary: str) -> str`：调用 provider.chat 生成不超过 72 字符的提交信息，前缀固定 `[codepilot]`，禁止 Markdown

`GitConfig` SHALL 包含 `auto_commit: bool = True`、`commit_message_style: Literal['rules', 'llm'] = 'rules'`、`no_auto_commit` 对应 `--no-auto-commit` CLI 标志。

#### Scenario: 自动提交加前缀
- **WHEN** 在 git 仓库中调用 `auto_commit('fix bug', [path])`
- **THEN** git log 中提交信息为 `[codepilot] fix bug`
- **AND** 返回值为 8 位 hash 字符串

#### Scenario: 撤销非 codepilot 提交被拒绝
- **WHEN** 最近一次提交不以 `[codepilot]` 开头
- **THEN** `undo_last_commit()` 返回 `(False, ...)` 且不执行 reset

#### Scenario: 非 git 仓库静默失败
- **WHEN** 在非 git 目录调用 `auto_commit` / `undo_last_commit` / `get_dirty_files`
- **THEN** 分别返回 None / (False, ...) / [] 且不抛异常

#### Scenario: /undo 优先 git 回滚
- **WHEN** 用户执行 `/undo` 且 git_manager 可用
- **THEN** 优先尝试 `git_manager.undo_last_commit()`
- **AND** 成功则展示撤销的提交信息
- **AND** 失败则回退到内存 UndoTracker.undo()

### Requirement: 会话持久化（Phase 3）
系统 SHALL 在 `src/codepilot/session/` 下提供 `SessionStorage`、`SessionManager`、`SessionExporter`。

`SessionRecord` TypedDict SHALL 包含 `session_id`、`start_time`（ISO 8601）、`end_time`、`workspace_root`、`messages`、`tool_calls`（含 tool_name/arguments/result/duration_ms/timestamp）、`token_usage`（input/output/total）、`provider`、`model`。

`SessionStorage` SHALL：
- `init(sessions_dir: Path = Path.home() / '.codepilot' / 'sessions')` 自动创建目录，权限 `mode=0o700`
- `save(record) -> Path`：序列化 JSON 写入 `{session_id}.json`
- `load(session_id) -> SessionRecord`：文件不存在抛 `SessionError`
- `list_sessions(limit=20) -> list[SessionRecord]`：按 start_time 降序
- `get_latest() -> SessionRecord | None`

`SessionManager` SHALL：
- 新建会话生成 `session_id`（uuid4 前 8 位 + 时间戳）
- `add_message(role, content)` 追加消息并更新 token 计数
- `record_tool_call(tool_name, arguments, result, duration_ms)` 自动加 timestamp
- `save()` 调用 storage.save，写入失败 SHALL 静默只 log warning，不影响主流程

`SessionExporter` SHALL：
- `to_markdown(record) -> str`：含元数据表格、工具调用汇总、对话历史（代码块高亮）
- `to_json(record) -> str`：`json.dumps` 缩进，`ensure_ascii=False`

CLI SHALL 新增 `-c/--continue`（加载最近会话历史注入 context_manager）和 `-r/--resume SESSION_ID`（加载指定会话）。slash 命令 SHALL 新增 `/sessions`（展示最近 10 个会话）和 `/export [markdown|json]`（导出当前会话到 `codepilot-session-{session_id}.{ext}`）。

#### Scenario: 会话目录权限隔离
- **WHEN** SessionStorage 初始化创建 sessions_dir
- **THEN** 目录权限为 `0o700`（仅当前用户可访问）

#### Scenario: 保存失败不阻断主流程
- **WHEN** sessions_dir 不可写
- **THEN** `save()` 不抛异常，仅 log warning

#### Scenario: 断点续跑
- **WHEN** 使用 `-c` 启动
- **THEN** 加载最近会话的 messages 注入 context_manager
- **AND** `context_manager.get_context()` 包含历史消息

#### Scenario: 禁止明文记录 API Key
- **WHEN** 序列化 SessionRecord
- **THEN** SecretStr 的 `get_secret_value()` 不出现在任何序列化路径上

### Requirement: Lint 反馈循环（Phase 4）
系统 SHALL 在 `src/codepilot/hooks/` 下提供 `HookRegistry`、`HookEvent` 枚举（TOOL_CALL_BEFORE/TOOL_CALL_AFTER/TURN_END/SESSION_START/SESSION_END/ERROR）、`HookResult` TypedDict（success/output/should_retry/retry_message）、`BaseHook` 抽象基类。

`HookRegistry` SHALL：
- `register(hook: BaseHook)`
- `trigger(event, context) -> list[HookResult]`：按注册顺序调用
- `trigger_tool_after(tool_name, path, result) -> HookResult | None`：返回第一个 `should_retry=True` 的结果

`LintHook` SHALL：
- name 为 `auto_lint`
- 在 `TOOL_CALL_AFTER` 且 tool_name ∈ `['write_file', 'edit_file']` 且 path 非None 时触发
- `.py` 文件运行 `python -m ruff check --output-format=json {path}`，解析 JSON，有错误则 `should_retry=True`，retry_message 格式 `以下 lint 错误需要修复：\n{错误列表}`，每个错误 `第{line}行：{message}（{code}）`
- `.js/.ts` 有 npx eslint 则运行，无则跳过；`.go` 有 gofmt 则运行，无则跳过
- 所有异常 SHALL 被 catch，log warning 后返回 `should_retry=False` 的 HookResult，禁止异常传播到 agent loop

`GitCommitHook` SHALL 在 `TOOL_CALL_AFTER` 且 tool_name ∈ `['write_file', 'edit_file']` 时调用 `app.git_manager.auto_commit()`。

AgentLoop SHALL 在工具执行完成后调用 `hook_registry.trigger_tool_after`，若 `should_retry=True` 则将 retry_message 作为 tool_result 追加到消息历史触发新一轮 LLM 调用，最多重试 `MAX_LINT_RETRIES=3` 次。UICallback.on_tool_result 展示中 SHALL 追加 `[Lint 修复尝试 1/3]` 格式计数。

`HooksConfig` SHALL 包含 `auto_lint: bool = True`、`auto_git_commit: bool = True`（依赖 git.auto_commit）、`max_lint_retries: int = 3`。

#### Scenario: lint 错误触发重试
- **WHEN** write_file 写入有 ruff 错误的 Python 文件
- **THEN** LintHook 返回 `should_retry=True`
- **AND** retry_message 包含行号和错误码

#### Scenario: lint 工具不可用静默
- **WHEN** ruff 不可用
- **THEN** LintHook 不抛异常，返回 `should_retry=False`

#### Scenario: lint 重试循环上限
- **WHEN** 连续 lint 错误超过 MAX_LINT_RETRIES 次
- **THEN** 停止重试，避免无限循环

### Requirement: Repo Map（Phase 5，可选）
系统 SHALL 在 `src/codepilot/repomap/` 下提供 `RepoMapper`，依赖 `tree-sitter-language-pack` 和 `networkx`（pyproject.toml 中作为 `[project.optional-dependencies]` 的 `repomap` 可选组声明）。tree-sitter 不可用时整个模块 SHALL 降级为 None，所有调用方 `if repo_map is None` 跳过，禁止抛异常。

`RepoMapper` SHALL：
- `init(workspace_root: Path, max_tokens: int = 1024, cache_db: Path | None = None)`
- `is_available() -> bool`：检查 tree_sitter_language_pack 可导入
- `build(relevant_files: list[Path] | None = None) -> str`：不可用返回空字符串；遍历 .py 文件（忽略 .git/__pycache__/.venv/node_modules/dist），tree-sitter 提取 function_definition/method_definition/class_definition 符号名和行号，networkx 构建引用图并运行 pagerank 排序，按分数选取文件生成紧凑文本（`文件路径\n class/def 符号名(签名): ...`）直到 token 数超 max_tokens（用 TokenCounter），SQLite 缓存（键=路径+mtime，值=符号列表）
- `build_for_query(query: str) -> str`：同 build 但优先选取文件名/符号名匹配 query 的文件

AgentLoop SHALL 在每轮对话开始（用户输入处理后、LLM 调用前）若 `repo_mapper is not None` 调用 `build_for_query(user_input)`，将结果以 `\n\n## 当前仓库结构摘要\n{map_text}` 追加到系统提示末尾。

`.codepilot.yml` SHALL 新增 `repomap.enabled: bool = True`、`repomap.max_tokens: int = 1024`、`repomap.languages: list[str] = ['python']`。

#### Scenario: tree-sitter 不可用降级
- **WHEN** `is_available()` 为 False
- **THEN** `build()` 返回空字符串，不触发导入

#### Scenario: token 预算受控
- **WHEN** 大量文件时调用 build
- **THEN** 结果 token 数不超过 `max_tokens * 1.1`

#### Scenario: SQLite 缓存命中
- **WHEN** 同一文件 mtime 不变时 build 两次
- **THEN** 第二次不重新解析

## MODIFIED Requirements

### Requirement: App 依赖注入组合根
`App.init` SHALL 在现有初始化基础上新增：GitManager、SessionManager、HookRegistry（根据 config.hooks 注册内置钩子）、RepoMapper（尝试初始化，不可用则 None）。这些实例 SHALL 通过构造参数注入 AgentLoop，禁止 monkey-patch 全局状态。TrackedToolWrapper.execute 成功完成后 SHALL 调用 `git_manager.auto_commit`（使用规则生成提交信息避免 LLM 延迟）。

### Requirement: AgentLoop 工具执行后处理
AgentLoop SHALL 在工具 execute 返回结果后调用 `hook_registry.trigger_tool_after(tool_name, path, result)`，根据 `should_retry` 触发 lint 重试循环（上限 MAX_LINT_RETRIES）。每轮对话开始 SHALL 若 repo_mapper 非None 调用 `build_for_query` 注入系统提示。

### Requirement: CLI 与 slash 命令
CLI SHALL 新增 `--no-auto-commit`、`-c/--continue`、`-r/--resume SESSION_ID` 标志。slash 命令 SHALL 新增 `/sessions`、`/export [markdown|json]`，`/undo` 改为优先 git 回滚失败回退内存。

### Requirement: 配置系统
`config.py` SHALL 新增 `GitConfig`、`HooksConfig`、`RepoMapConfig` 三个 Pydantic 子模型，作为 AppConfig 的子字段。

## REMOVED Requirements
无（本次为纯增量，不删除现有功能）

## 全局约束（每个 Phase 结束都必须验证）
每个子任务完成后 SHALL 按顺序运行以下命令，全部通过才算完成：
1. `make clean`（删除所有 pycache 和 .pyc）
2. `pre-commit run --all-files`（全部 Passed）
3. `pytest tests/ -v --cov=src/codepilot --cov-report=term-missing --cov-fail-under=85`
4. `mypy src/ --strict`（Success: no issues found）
5. `ruff check src/ tests/` 然后 `ruff format --check src/ tests/`（All checks passed）
6. `codepilot --version`（输出版本号）

## 禁止清单（继承原有全部约束，新增以下）
- 禁止自行修改 Provider 默认端点或模型名（`astron-code-latest` 是用户测试 API，不得替换）
- 禁止通过 monkey-patch 全局状态实现 Git 集成，必须依赖注入
- 禁止在 Session 存储中明文记录 API Key 任何部分（SecretStr.get_secret_value() 不得出现在序列化路径）
- 禁止 LintHook 任何异常传播到 agent loop（所有 except 必须 log warning 后返回 should_retry=False 的 HookResult）
- 禁止 GitManager 在非 git 仓库中抛异常（所有 git 操作必须静默失败返回 None 或 False）
- 禁止 RepoMapper 在 tree-sitter 不可用时抛异常（is_available() 为 False 时直接返回空字符串）
- 禁止新增功能测试文件中出现任何 hardcoded API Key 或 URL
- 禁止推倒重来，每一步必须保证现有测试不回归
- 禁止先写实现再补测试（TDD，新增功能测试必须在功能代码之前编写）
- 禁止 mock git 命令（Git 相关测试必须用 subprocess.run(['git', 'init']) 在 tmp_path 初始化真实 git 仓库）
- 新增 TypedDict/dataclass/Pydantic model 必须在对应测试中有 isinstance 或 model_validate 断言
- structlog 日志测试必须使用 structlog.testing.capture_logs() 验证关键字段
- 可选依赖（tree-sitter-language-pack、networkx）必须在 pyproject.toml 作为可选依赖组声明，测试中有 skipif 守卫

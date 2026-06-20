# Tasks

## Phase 1: 工程基建修复（前置必做，全部完成后才能进入 Phase 2）

- [ ] Task 1: 修复 pre-commit 空壳问题
  - [ ] SubTask 1.1: 运行 `pre-commit run --all-files`，修复 13 个文件的 ruff format 格式问题
  - [ ] SubTask 1.2: 在 Makefile 的 `test` 目标第一行加入 `pre-commit run --all-files`
  - [ ] SubTask 1.3: 验证 `pre-commit run --all-files` 输出全部 Passed
  - [ ] SubTask 1.4: 运行完整测试套件确认 0 回归，mypy --strict 0 error，ruff check/format 0 warning

- [ ] Task 2: 修复 make test 无 clean 问题
  - [ ] SubTask 2.1: 在 Makefile 的 `test` 目标首条命令加入 `find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true`（注意：与 Task 1.2 的 pre-commit 行协调顺序，pycache 清理在最前，pre-commit 次之，pytest 最后）
  - [ ] SubTask 2.2: 验证 `make test` 不再因 stale .pyc 报 AttributeError

- [ ] Task 3: 修复 CI 矩阵缺格式检查问题
  - [ ] SubTask 3.1: 在 `.github/workflows/ci.yml` 的 lint Job 中同时加入 `ruff check src/ tests/` 和 `ruff format --check src/ tests/` 两条命令
  - [ ] SubTask 3.2: 验证两条命令在本地均通过

## Phase 2: Git 深度集成（最高优先级新功能）

- [ ] Task 4: 编写 Git 模块测试（TDD，先于实现）
  - [ ] SubTask 4.1: 新建 `tests/unit/test_git.py`，编写 test_detect_git_repo、test_detect_non_git_dir、test_auto_commit_success、test_auto_commit_adds_prefix、test_undo_codepilot_commit、test_undo_non_codepilot_commit、test_auto_commit_no_repo、test_get_dirty_files、test_commit_message_generator_rules、test_commit_message_length 共 10 个用例
  - [ ] SubTask 4.2: 所有 git 测试使用 tmp_path + subprocess.run(['git', 'init']) 初始化真实 git 仓库，禁止 mock git 命令
  - [ ] SubTask 4.3: 新建 `tests/integration/test_git_integration.py`，编写 test_tracked_tool_auto_commit

- [ ] Task 5: 实现 Git 模块
  - [ ] SubTask 5.1: 创建 `src/codepilot/git/manager.py`，实现 GitManager（init/_detect_repo/is_git_repo/auto_commit/undo_last_commit/get_dirty_files），所有 git 操作在非 git 仓库静默失败返回 None/False
  - [ ] SubTask 5.2: 创建 `src/codepilot/git/commit.py`，实现 CommitMessageGenerator（generate 纯规则、generate_from_llm 调 provider）
  - [ ] SubTask 5.3: 创建 `src/codepilot/git/__init__.py`，导出 GitManager 和 CommitMessageGenerator
  - [ ] SubTask 5.4: 在 `src/codepilot/config.py` 新增 GitConfig（auto_commit/commit_message_style/no_auto_commit）

- [ ] Task 6: Git 与 App/CLI/AgentLoop 集成
  - [ ] SubTask 6.1: 在 `src/codepilot/app.py` 的 App.init 初始化 GitManager 并注入
  - [ ] SubTask 6.2: 在 TrackedToolWrapper.execute 成功后调用 `git_manager.auto_commit`（规则生成提交信息）
  - [ ] SubTask 6.3: `/undo` 命令改为优先 `git_manager.undo_last_commit()`，失败回退内存 UndoTracker
  - [ ] SubTask 6.4: CLI 新增 `--no-auto-commit` 标志，设置 `config.git.auto_commit = False`
  - [ ] SubTask 6.5: 运行 Phase 1 全局约束 6 步验证

## Phase 3: 会话持久化

- [ ] Task 7: 编写 Session 模块测试（TDD）
  - [ ] SubTask 7.1: 新建 `tests/unit/test_session.py`，编写 test_session_storage_save_load、test_session_storage_list_sorted、test_session_storage_dir_permissions、test_session_manager_records_tool_call、test_session_export_markdown_contains_metadata、test_session_export_json_valid、test_session_save_fails_silently、test_resume_session 共 8 个用例
  - [ ] SubTask 7.2: 验证 SessionRecord TypedDict 在测试中有 isinstance 断言

- [ ] Task 8: 实现 Session 模块
  - [ ] SubTask 8.1: 创建 `src/codepilot/session/storage.py`，定义 SessionRecord TypedDict 和 SessionStorage（0o700 权限、save/load/list_sessions/get_latest、SessionError）
  - [ ] SubTask 8.2: 创建 `src/codepilot/session/manager.py`，实现 SessionManager（session_id 生成、add_message、record_tool_call、save 静默失败）
  - [ ] SubTask 8.3: 创建 `src/codepilot/session/export.py`，实现 SessionExporter（to_markdown/to_json）
  - [ ] SubTask 8.4: 创建 `src/codepilot/session/__init__.py`，导出 SessionManager/SessionStorage/SessionExporter/SessionRecord
  - [ ] SubTask 8.5: 确保序列化路径不含 SecretStr.get_secret_value()

- [ ] Task 9: Session 与 App/CLI 集成
  - [ ] SubTask 9.1: App.init 初始化 SessionManager
  - [ ] SubTask 9.2: agent_loop.run() 完成后调用 session_manager.save()；工具调用完成后调用 record_tool_call()
  - [ ] SubTask 9.3: CLI 新增 `-c/--continue`（加载最近会话历史注入 context_manager）和 `-r/--resume SESSION_ID`
  - [ ] SubTask 9.4: slash 命令新增 `/sessions`（show_sessions 展示最近 10 个）和 `/export [markdown|json]`（导出到 codepilot-session-{session_id}.{ext}）
  - [ ] SubTask 9.5: 运行 Phase 1 全局约束 6 步验证

## Phase 4: Lint 反馈循环

- [ ] Task 10: 编写 Hooks 模块测试（TDD）
  - [ ] SubTask 10.1: 新建 `tests/unit/test_hooks.py`，编写 test_lint_hook_clean_file、test_lint_hook_error_file、test_lint_hook_non_python_file、test_lint_hook_ruff_not_found_silently、test_hook_registry_trigger_order、test_hook_registry_first_retry_wins、test_lint_retry_loop_in_agent 共 7 个用例
  - [ ] SubTask 10.2: HookResult TypedDict 在测试中有 isinstance 断言；structlog 日志测试用 capture_logs 验证关键字段

- [ ] Task 11: 实现 Hooks 模块
  - [ ] SubTask 11.1: 创建 `src/codepilot/hooks/registry.py`，定义 HookEvent 枚举、HookResult TypedDict、BaseHook 抽象基类、HookRegistry（register/trigger/trigger_tool_after）
  - [ ] SubTask 11.2: 创建 `src/codepilot/hooks/builtin.py`，实现 LintHook（ruff/eslint/gofmt，异常静默 should_retry=False）和 GitCommitHook
  - [ ] SubTask 11.3: 创建 `src/codepilot/hooks/__init__.py`，导出公开 API
  - [ ] SubTask 11.4: 在 config.py 新增 HooksConfig（auto_lint/auto_git_commit/max_lint_retries）

- [ ] Task 12: Hooks 与 AgentLoop/App 集成
  - [ ] SubTask 12.1: App.init 初始化 HookRegistry 并根据 config.hooks 注册内置钩子，注入 AgentLoop
  - [ ] SubTask 12.2: AgentLoop 工具 execute 返回后调用 trigger_tool_after，should_retry=True 时追加 retry_message 触发新一轮 LLM 调用，上限 MAX_LINT_RETRIES=3
  - [ ] SubTask 12.3: UICallback.on_tool_result 展示追加 `[Lint 修复尝试 1/3]` 计数
  - [ ] SubTask 12.4: 运行 Phase 1 全局约束 6 步验证

## Phase 5: Repo Map（可选，依赖可选依赖）

- [ ] Task 13: 配置可选依赖
  - [ ] SubTask 13.1: 在 pyproject.toml `[project.optional-dependencies]` 新增 `repomap` 组（tree-sitter-language-pack、networkx）
  - [ ] SubTask 13.2: .codepilot.yml.example 新增 repomap.enabled/max_tokens/languages 字段

- [ ] Task 14: 编写 RepoMap 测试（TDD，带 skipif 守卫）
  - [ ] SubTask 14.1: 新建 `tests/unit/test_repomap.py`，所有测试用 `pytest.mark.skipif(not RepoMapper(tmp_path).is_available(), reason='tree-sitter 不可用')` 守卫
  - [ ] SubTask 14.2: 编写 test_repo_mapper_unavailable_returns_empty、test_extracts_python_symbols、test_token_budget_respected、test_sqlite_cache_hit、test_sqlite_cache_miss_on_mtime_change 共 5 个用例

- [ ] Task 15: 实现 RepoMap 模块
  - [ ] SubTask 15.1: 创建 `src/codepilot/repomap/mapper.py`，实现 RepoMapper（is_available/build/build_for_query，tree-sitter 解析 + networkx pagerank + SQLite 缓存 + TokenCounter 预算控制）
  - [ ] SubTask 15.2: 创建 `src/codepilot/repomap/__init__.py`，导出 RepoMapper
  - [ ] SubTask 15.3: 在 config.py 新增 RepoMapConfig（enabled/max_tokens/languages）

- [ ] Task 16: RepoMap 与 App/AgentLoop 集成
  - [ ] SubTask 16.1: App.init 尝试初始化 RepoMapper，is_available() 为 False 则 self.repo_mapper = None
  - [ ] SubTask 16.2: AgentLoop 接收 repo_mapper 参数，每轮对话开始（用户输入处理后、LLM 调用前）若非 None 调用 build_for_query(user_input)，以 `\n\n## 当前仓库结构摘要\n{map_text}` 追加到系统提示末尾
  - [ ] SubTask 16.3: 运行 Phase 1 全局约束 6 步验证（tree-sitter 不可用时相关测试 skipif 跳过，不阻断）

## Phase 6: 最终全局验证

- [ ] Task 17: 全量回归验证
  - [ ] SubTask 17.1: make clean
  - [ ] SubTask 17.2: pre-commit run --all-files（全部 Passed）
  - [ ] SubTask 17.3: pytest tests/ -v --cov=src/codepilot --cov-report=term-missing --cov-fail-under=85
  - [ ] SubTask 17.4: mypy src/ --strict（Success: no issues found）
  - [ ] SubTask 17.5: ruff check src/ tests/ && ruff format --check src/ tests/
  - [ ] SubTask 17.6: codepilot --version

# Task Dependencies
- Task 2 依赖 Task 1（Makefile test 目标协调 pycache 清理与 pre-commit 顺序）
- Task 3 独立，可与 Task 1/2 并行
- Task 5 依赖 Task 4（TDD）
- Task 6 依赖 Task 5
- Task 8 依赖 Task 7（TDD）
- Task 9 依赖 Task 8
- Task 11 依赖 Task 10（TDD）
- Task 12 依赖 Task 11，且依赖 Task 6（GitCommitHook 依赖 GitManager）
- Task 15 依赖 Task 14（TDD）和 Task 13（可选依赖）
- Task 16 依赖 Task 15
- Task 17 依赖所有前置 Task 完成
- Phase 2/3/4 之间相对独立，可在 Phase 1 完成后并行推进（但同 Phase 内 Task 串行）

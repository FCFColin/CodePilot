# Checklist

## Phase 1: 工程基建修复
- [x] pre-commit run --all-files 输出全部 Passed，无 Failed
- [x] 13 个 ruff format 格式问题已修复
- [x] Makefile test 目标首行为 pycache 清理命令（`find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true`）
- [x] Makefile test 目标次行为 pre-commit run --all-files
- [x] Makefile test 目标末行为 pytest
- [x] .github/workflows/ci.yml lint Job 同时包含 `ruff check src/ tests/` 和 `ruff format --check src/ tests/`
- [x] Phase 1 完成后运行完整测试套件 0 回归
- [x] Phase 1 完成后 mypy src/ --strict 0 error
- [x] Phase 1 完成后 ruff check/format 0 warning

## Phase 2: Git 深度集成
- [x] tests/unit/test_git.py 存在且包含 10 个指定测试用例
- [x] tests/integration/test_git_integration.py 存在且包含 test_tracked_tool_auto_commit
- [x] 所有 git 测试使用 tmp_path + subprocess.run(['git', 'init']) 真实 git 仓库，无 mock git 命令
- [x] src/codepilot/git/manager.py 实现 GitManager 全部 6 个方法
- [x] src/codepilot/git/commit.py 实现 CommitMessageGenerator（generate + generate_from_llm）
- [x] src/codepilot/git/__init__.py 导出 GitManager 和 CommitMessageGenerator
- [x] config.py 新增 GitConfig（auto_commit/commit_message_style/no_auto_commit），测试中有 model_validate 断言
- [x] GitManager 在非 git 仓库中所有操作静默失败返回 None/False，不抛异常
- [x] auto_commit 提交信息强制 [codepilot] 前缀，返回 8 位 hash
- [x] undo_last_commit 检查 [codepilot] 前缀，非 codepilot 提交拒绝回滚返回 (False, ...)
- [x] App.init 初始化 GitManager 并注入（依赖注入，非 monkey-patch）
- [x] TrackedToolWrapper.execute 成功后调用 git_manager.auto_commit（规则生成提交信息）
- [x] /undo 命令优先 git_manager.undo_last_commit()，失败回退内存 UndoTracker
- [x] CLI 新增 --no-auto-commit 标志
- [x] Provider 默认端点和模型名未被修改（astron-code-latest 保留）

## Phase 3: 会话持久化
- [x] tests/unit/test_session.py 存在且包含 8 个指定测试用例
- [x] SessionRecord TypedDict 在测试中有 isinstance 断言
- [x] src/codepilot/session/storage.py 定义 SessionRecord 和 SessionStorage
- [x] sessions_dir 创建权限为 0o700（test_session_storage_dir_permissions 验证）
- [x] SessionStorage.save/load/list_sessions/get_latest 实现正确，list_sessions 按 start_time 降序
- [x] load 文件不存在抛 SessionError
- [x] src/codepilot/session/manager.py 实现 SessionManager（session_id 生成、add_message、record_tool_call、save）
- [x] SessionManager.save() 写入失败静默只 log warning，不抛异常（test_session_save_fails_silently 验证）
- [x] src/codepilot/session/export.py 实现 SessionExporter（to_markdown/to_json）
- [x] to_markdown 包含 session_id/provider/model 元数据
- [x] to_json 可被 json.loads 解析且含 messages 字段
- [x] 序列化路径不含 SecretStr.get_secret_value()（API Key 不明文记录）
- [x] src/codepilot/session/__init__.py 导出 SessionManager/SessionStorage/SessionExporter/SessionRecord
- [x] App.init 初始化 SessionManager
- [x] agent_loop.run() 完成后调用 session_manager.save()
- [x] 工具调用完成后调用 session_manager.record_tool_call()
- [x] CLI 新增 -c/--continue 和 -r/--resume SESSION_ID
- [x] -c 加载最近会话历史注入 context_manager（test_resume_session 验证 get_context 包含历史消息）
- [x] slash 命令新增 /sessions（展示最近 10 个会话）
- [x] slash 命令新增 /export [markdown|json]（导出到 codepilot-session-{session_id}.{ext}）

## Phase 4: Lint 反馈循环
- [x] tests/unit/test_hooks.py 存在且包含 7 个指定测试用例
- [x] HookResult TypedDict 在测试中有 isinstance 断言
- [x] structlog 日志测试使用 structlog.testing.capture_logs() 验证关键字段
- [x] src/codepilot/hooks/registry.py 定义 HookEvent 枚举（6 个值）、HookResult TypedDict、BaseHook 抽象基类、HookRegistry
- [x] HookRegistry.trigger 按注册顺序调用（test_hook_registry_trigger_order 验证）
- [x] HookRegistry.trigger_tool_after 返回第一个 should_retry=True 的结果（test_hook_registry_first_retry_wins 验证）
- [x] src/codepilot/hooks/builtin.py 实现 LintHook（name='auto_lint'）
- [x] LintHook 对 .py 文件运行 ruff check --output-format=json 并解析
- [x] LintHook 对 .js/.ts 有 eslint 则运行无则跳过，.go 有 gofmt 则运行无则跳过
- [x] LintHook retry_message 格式为 `以下 lint 错误需要修复：\n{错误列表}`，每个错误 `第{line}行：{message}（{code}）`
- [x] LintHook 所有异常被 catch，log warning 后返回 should_retry=False，不传播到 agent loop
- [x] src/codepilot/hooks/builtin.py 实现 GitCommitHook
- [x] src/codepilot/hooks/__init__.py 导出公开 API
- [x] config.py 新增 HooksConfig（auto_lint/auto_git_commit/max_lint_retries）
- [x] App.init 初始化 HookRegistry 并根据 config.hooks 注册内置钩子，注入 AgentLoop
- [x] AgentLoop 工具 execute 返回后调用 trigger_tool_after
- [x] should_retry=True 时追加 retry_message 触发新一轮 LLM 调用，上限 MAX_LINT_RETRIES=3
- [x] UICallback.on_tool_result 展示追加 `[Lint 修复尝试 1/3]` 计数
- [x] test_lint_retry_loop_in_agent 验证 mock provider 先返回错误代码、第二次返回修复代码的完整循环

## Phase 5: Repo Map（可选）
- [x] pyproject.toml [project.optional-dependencies] 新增 repomap 组（tree-sitter-language-pack、networkx）
- [x] .codepilot.yml.example 新增 repomap.enabled/max_tokens/languages
- [x] tests/unit/test_repomap.py 存在且所有测试有 skipif 守卫
- [x] src/codepilot/repomap/mapper.py 实现 RepoMapper（is_available/build/build_for_query）
- [x] is_available() 为 False 时 build() 返回空字符串，不抛异常，不触发导入
- [x] build 遍历 .py 文件忽略 .git/__pycache__/.venv/node_modules/dist
- [x] tree-sitter 提取 function_definition/method_definition/class_definition 符号名和行号
- [x] networkx 构建引用图并运行 pagerank 排序
- [x] token 预算控制不超过 max_tokens * 1.1（test_token_budget_respected 验证）
- [x] SQLite 缓存键=路径+mtime，mtime 不变命中缓存（test_sqlite_cache_hit 验证）
- [x] mtime 变化缓存失效重新解析（test_sqlite_cache_miss_on_mtime_change 验证）
- [x] src/codepilot/repomap/__init__.py 导出 RepoMapper
- [x] config.py 新增 RepoMapConfig（enabled/max_tokens/languages）
- [x] App.init 尝试初始化 RepoMapper，不可用则 self.repo_mapper = None
- [x] AgentLoop 每轮对话开始若 repo_mapper 非 None 调用 build_for_query(user_input)
- [x] 仓库摘要以 `\n\n## 当前仓库结构摘要\n{map_text}` 追加到系统提示末尾
- [x] tree-sitter 不可用时相关测试 skipif 跳过，不阻断全量验证

## Phase 6: 最终全局验证
- [x] make clean 成功
- [x] pre-commit run --all-files 全部 Passed，零 Failed
- [x] pytest tests/ -v --cov=src/codepilot --cov-report=term-missing --cov-fail-under=85 通过且覆盖率≥85%
- [x] mypy src/ --strict 输出 Success: no issues found
- [x] ruff check src/ tests/ 通过
- [x] ruff format --check src/ tests/ 通过
- [x] codepilot --version 输出版本号
- [x] 无回归（之前通过的测试未变为失败）

## 全局禁止清单核查
- [x] 未自行修改 Provider 默认端点或模型名（astron-code-latest 保留）
- [x] 未通过 monkey-patch 全局状态实现 Git 集成（全部依赖注入）
- [x] Session 存储中无明文 API Key（SecretStr.get_secret_value() 不在序列化路径）
- [x] LintHook 无异常传播到 agent loop（所有 except log warning 后返回 should_retry=False）
- [x] GitManager 在非 git 仓库中不抛异常（静默失败返回 None/False）
- [x] RepoMapper 在 tree-sitter 不可用时不抛异常（返回空字符串）
- [x] 新增功能测试文件中无 hardcoded API Key 或 URL
- [x] 未推倒重来，现有测试无回归
- [x] 新增功能测试先于实现编写（TDD）
- [x] Git 测试无 mock git 命令（真实 git 仓库）
- [x] 新增 TypedDict/dataclass/Pydantic model 测试中有 isinstance 或 model_validate 断言
- [x] structlog 日志测试使用 capture_logs 验证关键字段
- [x] 可选依赖在 pyproject.toml 作为可选依赖组声明，测试有 skipif 守卫

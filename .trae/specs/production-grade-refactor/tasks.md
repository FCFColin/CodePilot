# Tasks

## Phase 0: 工程骨架
- [x] Task 0: 创建 src layout 与工程文件
  - [x] SubTask 0.1: 创建 `src/codepilot/` 目录结构（agent/context/providers/security/tools/ui 子包，每个含 `__init__.py`）
  - [x] SubTask 0.2: 创建 `src/codepilot/__init__.py`（仅 `__version__ = "0.2.0"`）、`__main__.py`（调用 `cli.main`）
  - [x] SubTask 0.3: 创建 `src/codepilot/exceptions.py`（CodePilotError 基类 + ProviderError/ToolError/SecurityError/ConfigError）
  - [x] SubTask 0.4: 创建 `pyproject.toml`（PEP 517，name=codepilot-cli，[project.scripts] codepilot=codepilot.cli:main，dev 依赖，mypy strict，ruff 配置，pytest 配置，hatchling build）
  - [x] SubTask 0.5: 创建 `Makefile`（dev/test/lint/typecheck/build 目标）
  - [x] SubTask 0.6: 创建 `.pre-commit-config.yaml`（ruff + mypy 钩子）
  - [x] SubTask 0.7: 创建 `.github/workflows/ci.yml`（lint/typecheck/test/build 四串行 Job，test 矩阵 3.11/3.12）
  - [x] SubTask 0.8: 创建 `README.md`（安装、快速开始、配置、命令列表）、`CHANGELOG.md`（初始记录）
  - [x] SubTask 0.9: 创建 `tests/` 目录结构（conftest.py、unit/、integration/、e2e/，各含 `__init__.py`）
  - [x] SubTask 0.10: 创建 `src/codepilot/cli.py` 桩代码（argparse 解析 --version，main 函数）
  - [x] SubTask 0.11: 创建 `src/codepilot/app.py` 桩代码
  - [x] SubTask 0.12: 更新 `.codepilot.yml.example` 反映讯飞 maas-coding-api 端点与 astron-code-latest 模型
- [x] Task 0 验收: `pip install -e ".[dev]"` 成功 && `codepilot --version` 输出 `CodePilot v0.2.0` && `python -m codepilot --version` 一致 && ruff/mypy --strict 零问题

## Phase 1: 配置系统
- [x] Task 1: 实现 Pydantic v2 配置系统
  - [x] SubTask 1.1: 在 `src/codepilot/config.py` 用 Pydantic v2 BaseSettings 定义配置模型（provider/deepseek/anthropic/security/context/ui 段），API Key 用 SecretStr
  - [x] SubTask 1.2: 默认 base_url 设为讯飞星火端点（OpenAI: `https://maas-coding-api.cn-huabei-1.xf-yun.com/v2`，Anthropic: `https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic`），默认 model `astron-code-latest`
  - [x] SubTask 1.3: 实现环境变量覆盖（CODEPILOT_API_KEY、CODEPILOT_PROVIDER 等）
  - [x] SubTask 1.4: 实现 `.codepilot.yml` YAML 加载（pydantic-settings YAML 支持）
  - [x] SubTask 1.5: 实现命令行参数覆盖（在 cli.py 中）
  - [x] SubTask 1.6: 实现 fail-fast：缺少 API Key 时启动阶段报错退出（ConfigError）
  - [x] SubTask 1.7: 更新 `.codepilot.yml.example` 反映新端点和模型
- [x] Task 1 验收: 编写 `tests/unit/test_config.py`（环境变量覆盖、YAML 加载、优先级、fail-fast、无效配置），运行通过

## Phase 2: Provider 适配
- [ ] Task 2: 迁移 Provider 至 src layout 并补充类型
  - [ ] SubTask 2.1: 迁移 `providers/base.py`，AgentEvent 用 dataclass，Message/ToolCallResult 用 dataclass，format_tool_result/format_assistant_message 返回类型用 TypedDict
  - [ ] SubTask 2.2: 迁移 `providers/deepseek.py`，使用 openai SDK，base_url 默认讯飞端点，补充类型注解，网络调用加 tenacity 重试
  - [ ] SubTask 2.3: 迁移 `providers/anthropic.py`，使用 anthropic SDK，base_url 默认讯飞端点，补充类型注解，网络调用加 tenacity 重试
  - [ ] SubTask 2.4: 引入 structlog 日志，API Key 不入日志
- [ ] Task 2 验收: 编写 `tests/unit/test_providers.py`（format_tool_result/format_assistant_message 格式、_convert_messages 还原、respx mock HTTP 流式事件解析），运行通过

## Phase 3: 工具系统
- [ ] Task 3: 迁移工具系统至 src layout 并补充类型
  - [ ] SubTask 3.1: 迁移 `tools/registry.py`，BaseTool.execute 的 arguments 用 TypedDict，SandboxProtocol/ApprovalProtocol 用 Protocol
  - [ ] SubTask 3.2: 迁移 7 个工具（file_read/file_write/file_edit/list_files/shell_exec/search_code/get_context），补充类型注解，I/O 包装为自定义异常
  - [ ] SubTask 3.3: 引入 structlog 日志
- [ ] Task 3 验收: 编写 `tests/unit/test_tools.py`（parametrize 覆盖正常/边界/错误路径：read_file 二进制跳过/大文件截断、write_file 路径逃逸拒绝、edit_file 零匹配/多匹配、list_files 深度限制、shell_exec 超时/交互式拒绝/链式黑名单、search_code 正则/fnmatch），运行通过

## Phase 4: 安全系统
- [x] Task 4: 迁移安全系统至 src layout 并补充类型
  - [x] SubTask 4.1: 迁移 `security/sandbox.py`，validate_path/validate_command 返回类型用 NamedTuple 或 dataclass
  - [x] SubTask 4.2: 迁移 `security/command_filter.py` 和 `security/approval.py`，补充类型注解
  - [x] SubTask 4.3: 引入 structlog 日志
- [x] Task 4 验收: 编写 `tests/unit/test_security.py`（CommandFilter ≥50 边界用例：黑名单/白名单/交互式/提权/大小写绕过/链式拆解四种分隔符；Sandbox validate_path 路径逃逸/blocked_paths/写保护；ApprovalManager YOLO/会话级自动批准/跳过非审批操作），运行通过

## Phase 5: 上下文管理
- [ ] Task 5: 迁移上下文管理至 src layout 并补充类型
  - [ ] SubTask 5.1: 迁移 `context/token_counter.py`、`manager.py`、`compressor.py`，get_stats 返回 TypedDict，补充类型注解
  - [ ] SubTask 5.2: 引入 structlog 日志
- [ ] Task 5 验收: 编写 `tests/unit/test_context.py`（TokenCounter 精确/回退误差≤30%/缓存/多内容类型；ContextManager token 计数/maybe_compress 阈值/force_compress 回退 truncate/get_context 格式/clear 保留 system/并发线程安全；ContextCompressor truncate 保留 N 轮/summary 无 provider 回退），运行通过

## Phase 6: Agent 循环
- [ ] Task 6: 迁移 Agent 循环至 src layout 并补充类型
  - [ ] SubTask 6.1: 迁移 `agent/loop.py`，补充类型注解，UI 回调接口用 Protocol
  - [ ] SubTask 6.2: 引入 structlog 日志
- [ ] Task 6 验收: 编写 `tests/integration/test_agent_loop.py`（mock provider 测试完整 tool-use 循环、max_tool_calls 上限、cancel 中断、未知工具、sandbox 拒绝、approval 拒绝、多轮上下文累积）和 `tests/integration/test_tool_execution.py`（临时目录 write_file→read_file、edit_file、list_files、shell_exec），运行通过

## Phase 7: UI 与集成
- [ ] Task 7: 迁移 UI 与集成至 src layout
  - [ ] SubTask 7.1: 迁移 `ui/display.py`、`ui/diff_view.py`、`ui/banner.py`（ui/ 允许通过 rich Console 输出）
  - [ ] SubTask 7.2: 实现 `cli.py` 完整 argparse（交互/单次模式/--provider/--model/--api-key/--workspace/--no-approve/--config/--verbose/--version）
  - [ ] SubTask 7.3: 实现 `app.py` 依赖注入组合根（组装 provider/sandbox/approval/compressor/context_manager/tool_registry/ui/agent_loop）
  - [ ] SubTask 7.4: 实现 REPL 主循环与 slash 命令（/help /config /stats /clear /compact /history /model /provider /approve /undo /quit /exit）
  - [ ] SubTask 7.5: 错误处理（网络/API/工具错误优雅处理，Ctrl+C 中断回提示符，Ctrl+D 退出）
- [ ] Task 7 验收: 编写 `tests/e2e/test_cli.py`（subprocess 调用 codepilot：--version 返回码 0、--help 含关键参数、管道 /quit 退出、无 API Key 非零退出、python -m codepilot 一致性），运行通过

## 最终验收
- [ ] Task 8: 全量验收
  - [ ] SubTask 8.1: `pip install -e ".[dev]"` && `codepilot --version` 成功
  - [ ] SubTask 8.2: `pytest tests/ -v --cov=src/codepilot --cov-report=term-missing --cov-fail-under=80` 全通过
  - [ ] SubTask 8.3: `mypy src/ --strict` 零 error
  - [ ] SubTask 8.4: `ruff check src/` 零 warning
  - [ ] SubTask 8.5: `ruff format --check src/ tests/` 通过
  - [ ] SubTask 8.6: CHANGELOG.md 每个 Phase 有记录
  - [ ] SubTask 8.7: src/ 中无 print()（ui/ 例外）、无裸 dict 签名、无裸 except、无 time.sleep 在协程

# Task Dependencies
- Task 1-7 依赖 Task 0（工程骨架）
- Task 2-5 互相独立，可并行（迁移不同模块）
- Task 6 依赖 Task 2、Task 3、Task 5（agent loop 需要 provider/tools/context）
- Task 7 依赖 Task 1-6（集成所有组件）
- Task 8 依赖 Task 0-7 全部完成
- 每个 Phase 结束时运行四条验收命令（pip install/pytest/mypy/ruff）

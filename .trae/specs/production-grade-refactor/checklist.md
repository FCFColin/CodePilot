# Checklist

## Phase 0: 工程骨架
- [x] `src/codepilot/` 目录结构完整（agent/context/providers/security/tools/ui 子包）
- [x] `src/codepilot/__init__.py` 仅含 `__version__`
- [x] `src/codepilot/__main__.py` 调用 `cli.main`
- [x] `src/codepilot/exceptions.py` 定义 CodePilotError + ProviderError/ToolError/SecurityError/ConfigError
- [x] `src/codepilot/cli.py` 桩代码（argparse --version，main 函数）
- [x] `src/codepilot/app.py` 桩代码（依赖注入组合根占位）
- [x] `src/codepilot/config.py` 空文件（Phase 1 填充）
- [x] `pyproject.toml` name=codepilot-cli, requires-python>=3.11, [project.scripts] codepilot=codepilot.cli:main
- [x] `pyproject.toml` dev 依赖含 pytest/pytest-cov/pytest-asyncio/respx/mypy/ruff/pre-commit/build
- [x] `pyproject.toml` [tool.mypy] strict=true
- [x] `pyproject.toml` [tool.ruff] target-version=py311, line-length=88, select 含 E/F/W/I/N/UP/B/SIM
- [x] `pyproject.toml` [tool.pytest.ini_options] asyncio_mode=auto, markers 含 e2e
- [x] `pyproject.toml` [build-system] 用 hatchling
- [x] `Makefile` 含 dev/test/lint/typecheck/build 目标
- [x] `.pre-commit-config.yaml` 配置 ruff + mypy 钩子
- [x] `.github/workflows/ci.yml` 含 lint/typecheck/test/build 四串行 Job，test 矩阵 3.11/3.12
- [x] `README.md` 含安装（pip install codepilot-cli）、快速开始、配置说明、命令列表
- [x] `CHANGELOG.md` 初始记录
- [x] `tests/` 目录结构完整（conftest.py、unit/、integration/、e2e/）
- [x] `.codepilot.yml.example` 反映新端点（讯飞 maas-coding-api）与模型 astron-code-latest
- [x] `pip install -e ".[dev]"` 成功
- [x] `codepilot --version` 返回码 0 且输出 `CodePilot v0.2.0`
- [x] `python -m codepilot --version` 输出一致
- [x] `ruff check src/ tests/` 零 warning
- [x] `ruff format --check src/ tests/` 通过
- [x] `mypy src/ --strict` 零 error

## Phase 1: 配置系统
- [x] `src/codepilot/config.py` 使用 Pydantic v2 BaseSettings
- [x] API Key 使用 SecretStr 类型
- [x] 默认 OpenAI base_url: `https://maas-coding-api.cn-huabei-1.xf-yun.com/v2`
- [x] 默认 Anthropic base_url: `https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic`
- [x] 默认 model: `astron-code-latest`
- [x] 环境变量覆盖（CODEPILOT_API_KEY、CODEPILOT_PROVIDER 等）
- [x] `.codepilot.yml` YAML 加载
- [x] 命令行参数覆盖优先级
- [x] fail-fast：缺少 API Key 启动阶段报错退出（ConfigError）
- [x] `.codepilot.yml.example` 反映新端点和模型
- [x] `tests/unit/test_config.py` 覆盖环境变量/YAML/优先级/fail-fast/无效配置
- [x] `pytest tests/unit/test_config.py -v` 全部通过（31 个测试）
- [x] `mypy src/ --strict` 零 error
- [x] `ruff check src/ tests/` 零 warning
- [x] `python -c "from codepilot.config import Config, load_config; print('ok')"` 无报错

## Phase 2: Provider 适配
- [ ] `src/codepilot/providers/base.py` AgentEvent 用 dataclass，Message/ToolCallResult 用 dataclass
- [ ] format_tool_result/format_assistant_message 返回类型用 TypedDict
- [ ] `src/codepilot/providers/deepseek.py` 使用 openai SDK，base_url 默认讯飞端点
- [ ] `src/codepilot/providers/anthropic.py` 使用 anthropic SDK，base_url 默认讯飞端点
- [ ] 网络调用加 tenacity 重试（最多 3 次，指数退避）
- [ ] structlog 日志，API Key 不入日志
- [ ] 完整类型注解，mypy --strict 零 error
- [ ] `tests/unit/test_providers.py` 覆盖 format 方法/convert_messages/respx mock 流式事件

## Phase 3: 工具系统
- [ ] `src/codepilot/tools/registry.py` BaseTool.execute arguments 用 TypedDict
- [ ] SandboxProtocol/ApprovalProtocol 用 Protocol
- [ ] 7 个工具迁移至 src layout，完整类型注解
- [ ] I/O 操作包装为自定义异常
- [ ] structlog 日志
- [ ] `tests/unit/test_tools.py` parametrize 覆盖正常/边界/错误路径
- [ ] read_file: 正常/二进制跳过/大文件截断/文件不存在
- [ ] write_file: 正常写入/自动建父目录/路径逃逸拒绝
- [ ] edit_file: 唯一匹配/零匹配报错/多匹配报错
- [ ] list_files: 树状结构/深度限制/空目录
- [ ] shell_exec: 正常/超时/交互式拒绝/链式黑名单
- [ ] search_code: 正则匹配/fnmatch 过滤/零结果

## Phase 4: 安全系统
- [x] `src/codepilot/security/sandbox.py` validate_path/validate_command 返回 NamedTuple 或 dataclass
- [x] `src/codepilot/security/command_filter.py` 和 `approval.py` 完整类型注解
- [x] structlog 日志
- [x] `tests/unit/test_security.py` CommandFilter ≥50 边界用例
- [x] CommandFilter: 黑名单/白名单/交互式/提权/大小写绕过/链式拆解（||/&&/|/;）
- [x] Sandbox validate_path: 路径逃逸(../、符号链接、绝对路径)/blocked_paths/写保护(.git)
- [x] ApprovalManager: YOLO/会话级自动批准/跳过非审批操作

## Phase 5: 上下文管理
- [ ] `src/codepilot/context/` 三个模块迁移至 src layout
- [ ] get_stats 返回 TypedDict
- [ ] 完整类型注解
- [ ] structlog 日志
- [ ] `tests/unit/test_context.py` TokenCounter: 精确/回退误差≤30%/缓存/多内容类型
- [ ] ContextManager: token 计数/maybe_compress 阈值/force_compress 回退/get_context 格式/clear/并发安全
- [ ] ContextCompressor: truncate 保留 N 轮/summary 无 provider 回退

## Phase 6: Agent 循环
- [x] `src/codepilot/agent/loop.py` 迁移至 src layout，完整类型注解
- [x] UI 回调接口用 Protocol
- [x] structlog 日志
- [x] `tests/integration/test_agent_loop.py` mock provider 完整循环/上限/cancel/未知工具/sandbox 拒绝/approval 拒绝/多轮累积
- [x] `tests/integration/test_tool_execution.py` 临时目录 write→read/edit/list/shell

## Phase 7: UI 与集成
- [x] `src/codepilot/ui/` 三个模块迁移（允许 rich Console 输出）
- [x] `src/codepilot/cli.py` 完整 argparse（交互/单次/--provider/--model/--api-key/--workspace/--no-approve/--config/--verbose/--version）
- [x] `src/codepilot/app.py` 依赖注入组合根
- [x] REPL 主循环与 slash 命令（/help /config /stats /clear /compact /history /model /provider /approve /undo /quit /exit）
- [x] 错误处理（网络/API/工具优雅处理，Ctrl+C 中断，Ctrl+D 退出）
- [x] `tests/e2e/test_cli.py` subprocess 调用 codepilot
- [x] e2e: --version 返回码 0
- [x] e2e: --help 返回码 0 含关键参数
- [x] e2e: 管道 /quit 正常退出
- [x] e2e: 无 API Key 非零返回码
- [x] e2e: python -m codepilot --version 一致性

## 最终验收
- [x] `pip install -e ".[dev]"` && `codepilot --version` 成功
- [x] `pytest tests/ -v --cov=src/codepilot --cov-fail-under=80` 全通过且覆盖率≥80%（324 passed, 86.01%）
- [x] `mypy src/ --strict` 零 error（32 source files）
- [x] `ruff check src/` 零 warning
- [x] `ruff format --check src/ tests/` 通过（48 files formatted）
- [x] CHANGELOG.md 每个 Phase 有记录
- [x] src/ 中无 print()（ui/ 例外）
- [x] src/ 中无裸 dict 函数签名
- [x] src/ 中无裸 except / except Exception pass
- [x] src/ 协程中无 time.sleep()
- [x] API Key 不出现在日志/异常/traceback
- [x] 无 requirements.txt 作为主依赖管理
- [x] 测试文件无写死 API Key

## 禁止清单验证
- [x] 禁止 `python codepilot/main.py` 运行方式
- [x] 禁止 `python -c assert` 充当验收测试
- [x] 禁止 print() 在 src/（ui/ 例外）
- [x] 禁止裸 dict 函数签名类型
- [x] 禁止 requirements.txt 主依赖管理
- [x] 禁止测试写死 API Key
- [x] 禁止跳过 CI 任一 Job
- [x] 禁止裸 except / except Exception pass
- [x] 禁止协程 time.sleep()
- [x] 禁止 API Key 出现在日志/异常/traceback

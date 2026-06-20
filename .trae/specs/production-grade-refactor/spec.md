# CodePilot 生产级重构 Spec

## Why
现有 CodePilot CLI 已实现功能原型（flat layout、dataclass 配置、无测试、无 CI），但不满足生产级交付标准。需重构为 src layout、Pydantic v2 配置、完整测试套件（覆盖率≥80%）、CI/CD 流水线、mypy --strict 类型检查、structlog 结构化日志、自定义异常体系，并通过 `pip install` 后以 `codepilot` 命令运行。

## What Changes
- **BREAKING** 将 `codepilot/` flat layout 迁移至 `src/codepilot/` src layout
- **BREAKING** 废弃 `requirements.txt`，改用 `pyproject.toml`（PEP 517）作为唯一包配置
- **BREAKING** 废弃 `python codepilot/main.py` 运行方式，改为 `codepilot` CLI 入口点（`[project.scripts]`）和 `python -m codepilot`
- 新增 `pyproject.toml`、`Makefile`、`.pre-commit-config.yaml`、`.github/workflows/ci.yml`、`README.md`、`CHANGELOG.md`
- 新增 `src/codepilot/__main__.py`、`cli.py`（取代 main.py 的 argparse）、`app.py`（依赖注入组合根）、`exceptions.py`
- 配置系统从 dataclass 改为 Pydantic v2 BaseSettings，API Key 用 SecretStr，启动时 fail-fast
- 新增 `tests/` 三层测试（unit/integration/e2e），覆盖率≥80%
- 新增 structlog 结构化日志，src/ 中禁止 print()（ui/ 例外）
- 新增 tenacity 网络重试（最多 3 次，指数退避）
- 所有公共 API 完整类型注解，mypy --strict 零 error
- 禁止裸 dict 作为函数签名参数/返回类型，改用 TypedDict/dataclass/Pydantic model
- 默认 API 端点更新为讯飞星火 maas-coding-api（OpenAI 兼容 `/v2`、Anthropic 兼容 `/anthropic`），默认模型 `astron-code-latest`

## Impact
- Affected specs: `implement-codepilot-cli`（初版，被本次重构取代）
- Affected code: 全部现有 `codepilot/` 代码迁移至 `src/codepilot/`，配置系统重写，新增测试/CI/工程文件
- 新增依赖：pydantic>=2.0、pydantic-settings、structlog、tenacity；开发依赖：pytest、pytest-cov、pytest-asyncio、respx、mypy、ruff、pre-commit、build

## ADDED Requirements

### Requirement: 工程骨架（Phase 0）
系统 SHALL 采用 src layout，项目根目录包含 `pyproject.toml`、`Makefile`、`.pre-commit-config.yaml`、`.github/workflows/ci.yml`、`README.md`、`CHANGELOG.md`、`.codepilot.yml.example`。`src/codepilot/` 包含 `__init__.py`（仅 `__version__`）、`__main__.py`、`cli.py`、`app.py`、`config.py`、`exceptions.py` 及 agent/context/providers/security/tools/ui 子包。

`pyproject.toml` SHALL：
- name 为 `codepilot-cli`，requires-python `>=3.11`
- `[project.scripts]` 中 `codepilot` 指向 `codepilot.cli:main`
- `[project.optional-dependencies]` dev 组含 pytest/pytest-cov/pytest-asyncio/respx/mypy/ruff/pre-commit/build
- `[tool.mypy]` strict=true
- `[tool.ruff]` target-version=py311, line-length=88, select 含 E/F/W/I/N/UP/B/SIM
- `[tool.pytest.ini_options]` asyncio_mode=auto, markers 含 e2e
- `[build-system]` 用 hatchling

#### Scenario: CLI 入口可用
- **WHEN** 执行 `pip install -e ".[dev]"`
- **THEN** `codepilot --version` 返回码 0 且输出版本号
- **AND** `python -m codepilot --version` 输出一致

### Requirement: 自定义异常体系
`src/codepilot/exceptions.py` SHALL 定义 `CodePilotError` 基类，派生 `ProviderError`、`ToolError`、`SecurityError`、`ConfigError`。所有 I/O 操作 SHALL try/except 并包装为自定义异常。禁止裸 `except:`，禁止 `except Exception as e: pass`。

### Requirement: Pydantic v2 配置系统（Phase 1）
配置 SHALL 使用 Pydantic v2 BaseSettings，支持三级覆盖：命令行参数 > 环境变量（`CODEPILOT_API_KEY`、`CODEPILOT_PROVIDER` 等）> `.codepilot.yml`。API Key SHALL 使用 `SecretStr` 类型。启动时 fail-fast：缺少 API Key 立即报错退出。

默认 API 端点：
- OpenAI 兼容 base_url: `https://maas-coding-api.cn-huabei-1.xf-yun.com/v2`
- Anthropic 兼容 base_url: `https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic`
- 默认 model: `astron-code-latest`

#### Scenario: fail-fast 缺少 API Key
- **WHEN** 启动时未配置 API Key
- **THEN** 立即输出清晰错误信息并以非零返回码退出

#### Scenario: 环境变量覆盖
- **WHEN** 设置 `CODEPILOT_API_KEY` 环境变量
- **THEN** 该值覆盖配置文件中的 api_key

### Requirement: 结构化日志
系统 SHALL 使用 structlog 记录日志。src/ 中禁止 `print()`（ui/ 目录通过 rich Console 输出例外）。verbose 模式输出 DEBUG 级别，默认 WARNING。API Key 不得出现在日志、异常消息、traceback 中。

### Requirement: 网络重试
网络调用 SHALL 使用 tenacity 重试，最多 3 次，指数退避。禁止在协程中使用 `time.sleep()`，必须用 `asyncio.sleep()`。

### Requirement: 类型严格
全部公共 API SHALL 有完整类型注解，`mypy src/ --strict` 零 error。禁止裸 dict 作为函数签名参数/返回类型，必须用 TypedDict/dataclass/Pydantic model。

### Requirement: Provider 适配（Phase 2）
保留现有 DeepSeek（OpenAI 兼容）和 Anthropic 适配逻辑，迁移至 src layout 并补充类型注解。`format_tool_result` 和 `format_assistant_message` 返回类型 SHALL 用 TypedDict 而非裸 dict。流式事件解析为 `AgentEvent` 联合类型。

### Requirement: 工具系统（Phase 3）
保留现有 7 个工具逻辑，迁移至 src layout。工具参数和返回类型 SHALL 用 TypedDict/dataclass。`BaseTool.execute` 参数 `arguments` SHALL 用 TypedDict 而非裸 dict。

### Requirement: 安全系统（Phase 4）
保留现有 sandbox/command_filter/approval 逻辑，迁移至 src layout 并补充类型注解。`validate_path`/`validate_command` 返回类型 SHALL 用 NamedTuple 或 dataclass 而非裸 tuple。

### Requirement: 上下文管理（Phase 5）
保留现有 token_counter/manager/compressor 逻辑，迁移至 src layout。`get_stats` 返回类型 SHALL 用 TypedDict/dataclass。

### Requirement: Agent 循环（Phase 6）
保留现有 agentic loop 逻辑，迁移至 src layout。单轮最多 25 次工具调用，工具失败错误回传，每次工具调用后检查上下文压缩。

### Requirement: UI 与集成（Phase 7）
保留现有 display/diff_view/banner/slash 命令逻辑，迁移至 src layout。`cli.py` 取代 main.py 的 argparse，`app.py` 负责依赖注入组合根。REPL 主循环用 prompt_toolkit 异步读取。

### Requirement: 测试套件
系统 SHALL 提供三层测试：
- `tests/unit/`：每个模块独立测试，外部依赖全 mock，覆盖率≥80%
- `tests/integration/`：组件协作测试（agent_loop、tool_execution）
- `tests/e2e/`：通过 subprocess 调用 `codepilot` 命令验证真实 CLI 入口

#### Scenario: 单元测试覆盖
- **WHEN** 运行 `pytest tests/unit/ -v --cov=src/codepilot --cov-fail-under=80`
- **THEN** 全部通过且覆盖率≥80%

#### Scenario: e2e CLI 验证
- **WHEN** 运行 e2e 测试
- **THEN** `codepilot --version` 返回码 0
- **AND** `codepilot --help` 返回码 0 且含关键参数
- **AND** 管道 `echo "/quit" | codepilot` 正常退出
- **AND** 无 API Key 时非零返回码退出

### Requirement: CI 流水线
`.github/workflows/ci.yml` SHALL 包含 4 个串行 Job：lint（ruff check + format check）、typecheck（mypy --strict）、test（矩阵 3.11/3.12，unit+integration+e2e）、build（python -m build + 安装验证）。触发条件：push to main 和 PR to main。任一 Job 失败阻断后续。

### Requirement: 禁止清单
- 禁止 `python codepilot/main.py` 运行方式
- 禁止 `python -c "..." assert` 充当验收测试
- 禁止 `print()` 出现在 src/（ui/ 例外）
- 禁止裸 dict 作为函数签名类型
- 禁止 `requirements.txt` 作为主依赖管理
- 禁止测试文件写死 API Key
- 禁止跳过 CI 任一 Job
- 禁止裸 `except:` 和 `except Exception as e: pass`
- 禁止协程中 `time.sleep()`
- 禁止 API Key 出现在日志/异常/traceback

## MODIFIED Requirements

### Requirement: 现有功能逻辑保留
迁移过程中 SHALL 保留现有功能逻辑不丢失：7 个工具、安全沙箱、上下文压缩、Agent 循环、UI 显示、slash 命令。发现的 bug 或设计缺陷 SHALL 修复并在 CHANGELOG 记录。

## REMOVED Requirements

### Requirement: flat layout 与 requirements.txt
**Reason**: 生产级项目要求 src layout 和 pyproject.toml
**Migration**: `codepilot/` 内容迁移至 `src/codepilot/`，`requirements.txt` 依赖迁移至 `pyproject.toml`，原文件可保留但不再作为主依赖管理

### Requirement: dataclass 配置
**Reason**: 生产级要求 Pydantic v2 验证和 SecretStr
**Migration**: `config.py` 重写为 Pydantic BaseSettings，字段映射保持一致

# CodePilot CLI

终端 AI 编码智能体。在终端中与 LLM 协作完成代码阅读、编写、编辑、搜索与命令执行，
内置安全沙箱、审批机制与上下文压缩。

## 安装

```bash
pip install codepilot-cli
```

开发模式安装（含测试、lint、typecheck 工具链）：

```bash
make dev
# 等价于
pip install -e ".[dev]"
pre-commit install
```

## 快速开始

```bash
# 交互式 REPL
codepilot

# 单次执行模式
codepilot "解释 src/codepilot/cli.py 的入口逻辑"

# 指定 provider 与模型
codepilot --provider anthropic --model astron-code-latest "重构这个函数"

# 禁用审批（YOLO 模式，自动批准所有操作）
codepilot --no-approve "运行测试"
```

## 配置

CodePilot 支持三级配置覆盖（优先级从高到低）：

1. **命令行参数**：`--provider` / `--model` / `--api-key` / `--workspace` 等
2. **环境变量**：`CODEPILOT_API_KEY`、`CODEPILOT_PROVIDER` 等
3. **配置文件**：项目根目录 `.codepilot.yml`（参考 `.codepilot.yml.example`）

### 环境变量

```bash
export CODEPILOT_API_KEY="your-api-key"
export CODEPILOT_PROVIDER="deepseek"   # deepseek | anthropic
```

### 配置文件 `.codepilot.yml`

复制示例文件并按需修改：

```bash
cp .codepilot.yml.example .codepilot.yml
```

默认端点（讯飞星火 maas-coding-api，OpenAI/Anthropic 兼容）：

- OpenAI 兼容：`https://maas-coding-api.cn-huabei-1.xf-yun.com/v2`
- Anthropic 兼容：`https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic`
- 默认模型：`astron-code-latest`

API Key 通过 `${CODEPILOT_API_KEY}` 引用环境变量，避免硬编码。

## CLI 参数

| 参数 | 说明 |
| --- | --- |
| `prompt` | 可选位置参数，单次执行模式的提示词；省略则进入交互 REPL |
| `--provider` | LLM provider，可选 `deepseek` / `anthropic` |
| `--model` | 模型名 |
| `--api-key` | 直接传入 API Key |
| `--workspace` | 工作目录 |
| `--no-approve` | 禁用审批（YOLO 模式，自动批准所有操作） |
| `--config` | 配置文件路径 |
| `--verbose` | 详细日志（DEBUG 级别） |
| `--version` | 显示版本号并退出 |

## Slash 命令（交互模式）

| 命令 | 说明 |
| --- | --- |
| `/help` | 显示帮助 |
| `/config` | 查看当前配置 |
| `/stats` | 查看上下文 token 使用统计 |
| `/clear` | 清空对话历史 |
| `/compact` | 手动压缩上下文 |
| `/history` | 查看对话历史 |
| `/model [name]` | 查看或切换模型 |
| `/provider [name]` | 查看或切换 provider |
| `/approve` | 切换 YOLO 审批模式 |
| `/undo` | 撤销最近一次文件操作 |
| `/quit` `/exit` | 退出 REPL |

## 开发

```bash
make test        # 运行测试套件（覆盖率 ≥ 80%）
make lint        # ruff 检查与格式校验
make typecheck   # mypy --strict 类型检查
make build       # 构建分发包
```

## 项目结构

```
src/codepilot/        # src layout 主包
├── cli.py            # CLI 入口（argparse）
├── app.py            # 依赖注入组合根
├── config.py         # Pydantic v2 配置
├── exceptions.py     # 自定义异常体系
├── agent/            # Agent 循环
├── context/          # 上下文管理
├── providers/        # LLM Provider 适配
├── security/         # 安全沙箱与审批
├── tools/            # 工具系统
└── ui/               # 终端 UI（rich）
tests/                # unit / integration / e2e 三层测试
```

## License

MIT

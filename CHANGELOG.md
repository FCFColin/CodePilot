# Changelog

## [0.2.0] - 2026-06-19
### Phase 0: 工程骨架
- 创建 src layout 项目结构
- 添加 pyproject.toml（PEP 517，hatchling build）
- 添加 CLI 入口点 codepilot = codepilot.cli:main
- 添加 Makefile、.pre-commit-config.yaml、CI 流水线
- 添加自定义异常体系 exceptions.py
- 添加 tests/ 目录结构

### Phase 1: 配置系统
- 使用 Pydantic v2 BaseSettings 重写配置系统（config.py）
- API Key 使用 SecretStr 类型，不在 repr/str 中暴露明文
- 默认端点更新为讯飞 maas-coding-api（OpenAI 兼容 /v2、Anthropic 兼容 /anthropic）
- 默认模型更新为 astron-code-latest
- 实现四级配置优先级：CLI 参数 > 环境变量 > YAML > 默认值
- 支持 CODEPILOT_API_KEY 便捷环境变量（覆盖当前 provider 的 api_key）
- 支持 CODEPILOT_PROVIDER、CODEPILOT_DEEPSEEK__API_KEY 等嵌套环境变量
- 支持 YAML 配置文件中的 ${ENV_VAR} 引用替换
- YAML 配置加载路径：--config > .codepilot.yml > ~/.config/codepilot/config.yml
- 实现 fail-fast 校验：缺少 API Key 时抛出 ConfigError
- cli.py 集成配置加载，ConfigError 输出到 stderr 并 sys.exit(1)
- 引入 structlog 结构化日志（API Key 不入日志）
- 添加 31 个单元测试覆盖默认值/SecretStr/环境变量/YAML/优先级/fail-fast/无效配置

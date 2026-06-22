## 多 Provider 配置系统
- [ ] `ProviderConfig` 类存在，含 `type: openai|anthropic`、`api_key`、`base_url`、`model` 等字段
- [ ] `Config` 类包含 `providers: dict[str, ProviderConfig]` 字段
- [ ] 旧 `deepseek:`/`anthropic:` 格式自动转换为 `providers:` 格式（向后兼容）
- [ ] CLI `--provider <name>` 接受任意已配置的 provider 名称
- [ ] `create_app()` 根据 `providers[name].type` 动态创建 Provider 实例
- [ ] `DeepSeekProvider` 已重命名为 `OpenAICompatProvider`
- [ ] `.codepilot.yml.example` 展示新的 `providers:` 格式
- [ ] 测试验证多 provider 配置、向后兼容、CLI 切换

## web_fetch 工具
- [ ] `src/codepilot/tools/web_fetch.py` 存在且实现 web_fetch 工具
- [ ] 使用 httpx 抓取 URL，markdownify 转 Markdown
- [ ] 超时 15 秒，最大 50KB 截断
- [ ] URL 不可达时返回错误信息而非抛异常
- [ ] 在 tools/__init__.py 注册
- [ ] pyproject.toml 添加 httpx 和 markdownify 依赖
- [ ] 测试 `tests/unit/test_web_fetch.py` 通过

## diagnose 工具
- [ ] `src/codepilot/tools/diagnose.py` 存在且实现 diagnose 工具
- [ ] 运行 linter、读取 traceback、检查文件状态
- [ ] 在 tools/__init__.py 注册
- [ ] 测试 `tests/unit/test_diagnose.py` 通过

## plan 工具
- [ ] `src/codepilot/tools/plan_tool.py` 存在且实现 plan 工具
- [ ] 创建/更新结构化执行计划
- [ ] 在 tools/__init__.py 注册
- [ ] 测试 `tests/unit/test_plan_tool.py` 通过

## 循环检测
- [ ] agent/loop.py 中添加循环检测逻辑
- [ ] 连续 3 次相似调用 → 中断 + 提示换策略
- [ ] 测试验证循环检测

## 多步撤销 + /rollback + /plan + /providers
- [ ] /undo 支持连续撤销多步
- [ ] /rollback N 命令回退到第 N 轮对话
- [ ] /plan slash 命令查看当前执行计划
- [ ] /providers slash 命令列出所有已配置的 provider
- [ ] 测试通过

## 复杂场景测试
- [ ] 多轮问答测试通过
- [ ] 中断（Ctrl+C）后恢复测试通过
- [ ] 回退（/undo、/rollback）测试通过
- [ ] 网页抓取（web_fetch）测试通过
- [ ] 错误诊断（diagnose）测试通过
- [ ] plan 工具测试通过
- [ ] 循环检测测试通过

## 全局验证
- [ ] pytest tests/ -v --cov-fail-under=85 通过
- [ ] mypy src/ --strict 通过
- [ ] ruff check + format 通过
- [ ] codepilot --version 输出版本号

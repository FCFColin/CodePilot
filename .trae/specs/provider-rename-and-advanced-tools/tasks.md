# Tasks

- [x] Task 1: 多 Provider 配置系统重构
  - [ ] SubTask 1.1: 新增 `ProviderConfig` 类（含 `type: openai|anthropic`、`api_key`、`base_url`、`model`、`max_tokens`、`temperature`、`top_p`、`stream`、`thinking` 等字段）
  - [ ] SubTask 1.2: 修改 `Config` 类，新增 `providers: dict[str, ProviderConfig]` 字段，保留 `deepseek`/`anthropic` 旧字段作为向后兼容别名
  - [ ] SubTask 1.3: 实现 `_migrate_legacy_config()` 方法，将旧 `deepseek:`/`anthropic:` 自动转换为 `providers:` 格式
  - [ ] SubTask 1.4: 修改 `load_config()` 支持 `providers:` 段的解析和验证
  - [ ] SubTask 1.5: 修改 `create_app()` 根据 `providers[name].type` 动态创建对应 Provider 实例
  - [ ] SubTask 1.6: 修改 CLI `--provider` 参数接受任意已配置的 provider 名称
  - [ ] SubTask 1.7: 重命名 `DeepSeekProvider` → `OpenAICompatProvider`，`deepseek.py` → `openai_compat.py`
  - [ ] SubTask 1.8: 更新 `.codepilot.yml.example` 展示新的 `providers:` 格式
  - [ ] SubTask 1.9: 更新所有引用文件（app.py、cli.py、providers/__init__.py、测试文件）
  - [ ] SubTask 1.10: 编写测试验证多 provider 配置、向后兼容、CLI 切换

- [x] Task 2: 新增 web_fetch 工具
  - [ ] SubTask 2.1: 创建 `src/codepilot/tools/web_fetch.py`，使用 `httpx` 抓取 URL 内容，`markdownify` 转 Markdown
  - [ ] SubTask 2.2: 在 `pyproject.toml` 添加 `httpx` 和 `markdownify` 依赖
  - [ ] SubTask 2.3: 在 `tools/__init__.py` 注册 `web_fetch` 工具
  - [ ] SubTask 2.4: 编写测试 `tests/unit/test_web_fetch.py`

- [x] Task 3: 新增 diagnose 工具
  - [ ] SubTask 3.1: 创建 `src/codepilot/tools/diagnose.py`，运行 linter、读取 traceback、检查文件状态
  - [ ] SubTask 3.2: 在 `tools/__init__.py` 注册 `diagnose` 工具
  - [ ] SubTask 3.3: 编写测试 `tests/unit/test_diagnose.py`

- [x] Task 4: 新增 plan 工具
  - [ ] SubTask 4.1: 创建 `src/codepilot/tools/plan_tool.py`，创建/更新结构化执行计划
  - [ ] SubTask 4.2: 在 `tools/__init__.py` 注册 `plan` 工具
  - [ ] SubTask 4.3: 编写测试 `tests/unit/test_plan_tool.py`

- [x] Task 5: 循环检测机制
  - [ ] SubTask 5.1: 在 `agent/loop.py` 中添加循环检测逻辑（连续 3 次相似调用 → 中断 + 提示）
  - [ ] SubTask 5.2: 编写测试验证循环检测

- [x] Task 6: 多步撤销 + /rollback + /plan + /providers 命令
  - [ ] SubTask 6.1: 修改 `/undo` 支持连续撤销多步（UndoTracker 栈不限制单步）
  - [ ] SubTask 6.2: 新增 `/rollback N` 命令，回退到第 N 轮对话
  - [ ] SubTask 6.3: 新增 `/plan` slash 命令查看当前执行计划
  - [ ] SubTask 6.4: 新增 `/providers` slash 命令列出所有已配置的 provider
  - [ ] SubTask 6.5: 编写测试

- [x] Task 7: 复杂场景端到端测试
  - [ ] SubTask 7.1: 设计复杂测试场景（多轮问答 + 中断 + 回退 + 网页抓取 + plan + 循环检测）
  - [ ] SubTask 7.2: 用真实 API 执行测试并记录结果
  - [ ] SubTask 7.3: 根据测试结果修复发现的问题

# Task Dependencies
- Task 2, 3, 4, 5 互相独立，可并行
- Task 1 需先完成（其他 Task 引用新命名和配置结构）
- Task 6 依赖 Task 1（/providers 命令）和 Task 4（/plan 命令）
- Task 7 依赖 Task 1-6 全部完成

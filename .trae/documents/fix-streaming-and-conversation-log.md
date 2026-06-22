# 计划：修复讯飞星辰流式碎片化 + 对话记录增强 + 网页实测

## 总结

1. 修复讯飞星辰 API 流式输出碎片化问题（根因：thinking delta 无累积 + Live 频繁启停）
2. 增强对话记录功能（自动保存完整对话为 Markdown，含 thinking 内容）
3. 用真实 API 测试创建简单网页，验证成果

## 现状分析

### 碎片化根因（按严重程度排序）

1. **`on_thinking_delta` 无累积机制**：每个 ThinkingDelta 都生成独立 Panel 并 `console.print`，没有像 `on_text_delta` 那样的 `_current_text` 累积器。连续 reasoning chunk 产生大量碎片化 Thinking 面板。
2. **Thinking/Text 交替导致 Live 频繁启停**：`on_thinking_delta` 调用 `_stop_live()` 停止文本流式面板，`on_text_delta` 又调用 `_start_live()` 重建。交替到达时 Live 面板反复销毁/重建，视觉闪烁断裂。
3. **`show_thinking=False` 仍触发 `_stop_live()`**：即使不显示 thinking 内容，Live 面板仍被中断。
4. **Provider 层逐 chunk 直传无缓冲**：每个 `delta.content` chunk 都立即 yield TextDelta，API 返回粒度越细 UI 更新越频繁。

### 对话记录现状

- SessionManager 已有 `add_message(role, content)` 记录消息
- SessionExporter 已有 `to_markdown()`/`to_json()` 导出
- `/export` 命令已实现
- **缺陷**：thinking 内容不被记录；对话不会自动保存为可读格式；需要手动执行 /export

## 计划步骤

### Step 1: 修复流式输出碎片化

**文件**: `src/codepilot/ui/display.py`

修改 `on_thinking_delta` 方法：
- 添加 `_current_thinking: str` 累积器（类似 `_current_text`）
- 不再每个 delta 都 `_stop_live()` + `console.print` 独立 Panel
- 改为在 Live 面板内累积显示 thinking 内容（用 Thinking 子面板嵌入主面板）
- thinking 完成后（收到 TextDelta 或 Done 时）才固化显示

修改 `on_text_delta` 方法：
- 如果之前有累积的 thinking 内容，先固化 thinking 再显示文本

修改 `_stop_live` / `_build_assistant_panel`：
- `_build_assistant_panel` 中如果有 `_current_thinking`，在文本上方嵌入 Thinking 区域

**文件**: `src/codepilot/providers/deepseek.py`

添加 chunk 缓冲逻辑（可选但推荐）：
- 对极短 chunk（<5 字符且非工具调用）做微缓冲，减少 UI 更新频率
- 或在 `_iter_stream` 中对连续的 TextDelta 做简单合并（如 50ms 窗口内合并）

### Step 2: 增强对话记录功能

**文件**: `src/codepilot/session/manager.py`

- `add_message` 增加 `thinking_content: str | None = None` 参数，记录 thinking 内容
- 新增 `add_thinking(self, content: str) -> None` 方法，单独记录 thinking

**文件**: `src/codepilot/session/export.py`

- `to_markdown()` 中增加 thinking 内容显示（折叠块 `<details><summary>Thinking</summary>...</details>`）
- 增加工具调用的完整参数和返回值显示

**文件**: `src/codepilot/agent/loop.py`

- 在 `_emit_thinking_delta` 后累积 thinking 内容
- 在 `_record_session_message("assistant", accumulated_text)` 时同时传入 thinking 内容

**新增功能**: 自动对话日志

**文件**: `src/codepilot/session/manager.py`

- 新增 `auto_save_log` 逻辑：每轮对话结束时自动将完整对话保存为 Markdown 到工作目录的 `codepilot-conversation-log.md`
- 文件名包含日期：`codepilot-log-{session_id}.md`
- 在 `save()` 方法中追加调用 `self._auto_export_log()`

### Step 3: 用真实 API 测试创建简单网页

使用 DeepSeek 官方 API（已验证可用）测试：
1. 让 CLI 创建一个简单的 HTML 网页（如个人介绍页）
2. 启动本地 HTTP 服务器展示网页
3. 验证网页可访问
4. 检查对话日志是否自动保存

## 假设与决策

1. **Live 面板内嵌 Thinking**：将 thinking 内容嵌入 Live 面板而非独立打印，避免频繁启停
2. **thinking 累积器**：参考 `_current_text` 的模式，为 thinking 添加 `_current_thinking` 累积器
3. **对话日志自动保存**：每轮对话结束自动保存 Markdown 格式到工作目录，无需手动 /export
4. **DeepSeek 官方 API 为主测试**：讯飞星辰 API 碎片化修复后再验证

## 验证步骤

1. `pytest tests/unit/test_ui.py tests/unit/test_session.py -v` — 单元测试通过
2. `pytest tests/ -v --cov-fail-under=85` — 全量测试通过
3. `mypy src/ --strict` — 类型检查通过
4. 用 DeepSeek API 测试流式输出无碎片化
5. 用讯飞星辰 API 测试流式输出无碎片化
6. 创建网页任务，验证对话日志自动保存
7. 验证网页可访问

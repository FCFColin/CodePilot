"""app 模块单元测试。

覆盖：App 组件组装、create_app 工厂、slash 命令处理、run_single、
UndoTracker 撤销逻辑、TrackedToolWrapper 工具包装器。
使用 mock 隔离 provider 网络调用，tmp_path 隔离文件系统。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from codepilot.app import App, TrackedToolWrapper, UndoTracker, create_app
from codepilot.config import (
    AnthropicConfig,
    Config,
    DeepSeekConfig,
    SecurityConfig,
)
from codepilot.exceptions import CodePilotError
from codepilot.providers.anthropic import AnthropicProvider
from codepilot.providers.deepseek import DeepSeekProvider
from codepilot.tools.registry import BaseTool

# ============================================================================
# 辅助函数
# ============================================================================


def _clear_codepilot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除所有 CODEPILOT_ 前缀的环境变量，避免污染测试。"""
    for key in list(os.environ.keys()):
        if key.startswith("CODEPILOT_"):
            monkeypatch.delenv(key, raising=False)


def _make_config(
    tmp_path: Path,
    provider: str = "deepseek",
) -> Config:
    """构造测试用 Config，workspace_root 指向 tmp_path。"""
    _clear_codepilot_env(pytest.MonkeyPatch())
    return Config(
        provider=provider,
        deepseek=DeepSeekConfig(api_key=SecretStr("sk-test-deepseek")),
        anthropic=AnthropicConfig(api_key=SecretStr("sk-test-anthropic")),
        security=SecurityConfig(
            workspace_root=str(tmp_path),
            blocked_paths=[],
        ),
    )


class _MockTool(BaseTool):
    """测试用 mock 工具，记录 execute 调用。"""

    name = "mock_tool"
    description = "Mock tool for testing"

    def __init__(self) -> None:
        self.execute_called: bool = False
        self.execute_args: dict[str, Any] | None = None

    def get_parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(
        self,
        arguments: dict[str, Any],
        sandbox: Any = None,
        approval: Any = None,
    ) -> str:
        self.execute_called = True
        self.execute_args = arguments
        return "mock result"


# ============================================================================
# TestApp
# ============================================================================


class TestApp:
    """App 组件组装与 slash 命令测试。"""

    def test_app_creates_deepseek_provider(self, tmp_path: Path) -> None:
        """App 使用 deepseek provider 时创建 DeepSeekProvider。"""
        config = _make_config(tmp_path, provider="deepseek")
        app = App(config)
        assert isinstance(app.provider, DeepSeekProvider)

    def test_app_creates_anthropic_provider(self, tmp_path: Path) -> None:
        """App 使用 anthropic provider 时创建 AnthropicProvider。"""
        config = _make_config(tmp_path, provider="anthropic")
        app = App(config)
        assert isinstance(app.provider, AnthropicProvider)

    def test_app_components_initialized(self, tmp_path: Path) -> None:
        """App 初始化所有组件。"""
        config = _make_config(tmp_path)
        app = App(config)
        assert app.config is config
        assert app.provider is not None
        assert app.token_counter is not None
        assert app.sandbox is not None
        assert app.approval is not None
        assert app.compressor is not None
        assert app.context_manager is not None
        assert app.undo_tracker is not None
        assert app.tool_registry is not None
        assert app.display is not None
        assert app.agent_loop is not None

    def test_create_app_factory(self, tmp_path: Path) -> None:
        """create_app 工厂函数返回 App 实例。"""
        config = _make_config(tmp_path)
        app = create_app(config)
        assert isinstance(app, App)
        assert app.config is config

    def test_tool_registry_wraps_write_and_edit(self, tmp_path: Path) -> None:
        """工具注册表中 write_file 和 edit_file 被 TrackedToolWrapper 包装。"""
        config = _make_config(tmp_path)
        app = App(config)
        write_tool = app.tool_registry.get("write_file")
        edit_tool = app.tool_registry.get("edit_file")
        assert write_tool is not None
        assert edit_tool is not None
        assert isinstance(write_tool, TrackedToolWrapper)
        assert isinstance(edit_tool, TrackedToolWrapper)

    async def test_slash_help(self, tmp_path: Path) -> None:
        """/help 命令返回 False 且不退出。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/help")
        assert result is False

    async def test_slash_config(self, tmp_path: Path) -> None:
        """/config 命令返回 False 且不退出。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/config")
        assert result is False

    async def test_slash_stats(self, tmp_path: Path) -> None:
        """/stats 命令返回 False 且不退出。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/stats")
        assert result is False

    async def test_slash_clear(self, tmp_path: Path) -> None:
        """/clear 命令清空对话历史并返回 False。"""
        config = _make_config(tmp_path)
        app = App(config)
        # 先添加一条消息
        await app.context_manager.add_message("user", "test")
        assert len(app.context_manager.messages) > 0
        result = await app._handle_slash_command("/clear")
        assert result is False
        assert len(app.context_manager.messages) == 0

    async def test_slash_compact(self, tmp_path: Path) -> None:
        """/compact 命令触发压缩并返回 False。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/compact")
        assert result is False

    async def test_slash_history(self, tmp_path: Path) -> None:
        """/history 命令返回 False 且不退出。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/history")
        assert result is False

    async def test_slash_model_no_arg(self, tmp_path: Path) -> None:
        """/model 无参数时显示当前模型并返回 False。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/model")
        assert result is False

    async def test_slash_model_with_arg(self, tmp_path: Path) -> None:
        """/model 带参数时切换模型并返回 False。"""
        config = _make_config(tmp_path)
        app = App(config)
        original_model = config.deepseek.model
        result = await app._handle_slash_command("/model new-model-name")
        assert result is False
        assert config.deepseek.model == "new-model-name"
        assert config.deepseek.model != original_model

    async def test_slash_provider_no_arg(self, tmp_path: Path) -> None:
        """/provider 无参数时显示当前 provider 并返回 False。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/provider")
        assert result is False

    async def test_slash_provider_with_arg(self, tmp_path: Path) -> None:
        """/provider 带参数时切换 provider 并返回 False。"""
        config = _make_config(tmp_path, provider="deepseek")
        app = App(config)
        result = await app._handle_slash_command("/provider anthropic")
        assert result is False
        assert config.provider == "anthropic"
        assert app.display.provider_name == "anthropic"

    async def test_slash_provider_invalid_arg(self, tmp_path: Path) -> None:
        """/provider 带无效参数时不切换并返回 False。"""
        config = _make_config(tmp_path, provider="deepseek")
        app = App(config)
        result = await app._handle_slash_command("/provider invalid")
        assert result is False
        assert config.provider == "deepseek"

    async def test_slash_quit(self, tmp_path: Path) -> None:
        """/quit 命令返回 True 表示应退出。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/quit")
        assert result is True

    async def test_slash_exit(self, tmp_path: Path) -> None:
        """/exit 命令返回 True 表示应退出。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/exit")
        assert result is True

    async def test_slash_unknown(self, tmp_path: Path) -> None:
        """未知 slash 命令返回 False 且不退出。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/unknown_cmd")
        assert result is False

    async def test_slash_approve_toggle(self, tmp_path: Path) -> None:
        """/approve 命令切换 YOLO 模式。"""
        config = _make_config(tmp_path)
        app = App(config)
        assert not app.approval._yolo_mode
        # 第一次：开启 YOLO
        await app._handle_slash_command("/approve")
        assert app.approval._yolo_mode is True
        # 第二次：关闭 YOLO
        await app._handle_slash_command("/approve")
        assert app.approval._yolo_mode is False

    async def test_slash_undo_empty(self, tmp_path: Path) -> None:
        """/undo 命令在空栈时返回 False。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/undo")
        assert result is False

    async def test_run_single_normal(self, tmp_path: Path) -> None:
        """run_single 正常执行 agent_loop.run。"""
        config = _make_config(tmp_path)
        app = App(config)
        app.agent_loop.run = AsyncMock()
        await app.run_single("test prompt")
        app.agent_loop.run.assert_called_once_with("test prompt")

    async def test_run_single_keyboard_interrupt(self, tmp_path: Path) -> None:
        """run_single 捕获 KeyboardInterrupt 并调用 cancel。"""
        config = _make_config(tmp_path)
        app = App(config)
        app.agent_loop.run = AsyncMock(side_effect=KeyboardInterrupt())
        app.agent_loop.cancel = MagicMock()
        await app.run_single("test prompt")
        app.agent_loop.cancel.assert_called_once()

    async def test_run_single_codepilot_error(self, tmp_path: Path) -> None:
        """run_single 捕获 CodePilotError 并通过 display.on_error 输出。"""
        config = _make_config(tmp_path)
        app = App(config)
        app.agent_loop.run = AsyncMock(side_effect=CodePilotError("test error"))
        app.display.on_error = AsyncMock()
        await app.run_single("test prompt")
        app.display.on_error.assert_called_once()


# ============================================================================
# TestUndoTracker
# ============================================================================


class TestUndoTracker:
    """UndoTracker 撤销逻辑测试。"""

    def test_undo_empty_stack(self) -> None:
        """空栈时 undo 返回 (False, 提示信息)。"""
        tracker = UndoTracker()
        success, message = tracker.undo()
        assert success is False
        assert "没有可撤销" in message

    def test_undo_new_file(self, tmp_path: Path) -> None:
        """撤销新建文件（old_content 为 None）时删除文件。"""
        tracker = UndoTracker()
        file_path = tmp_path / "new_file.txt"
        file_path.write_text("content", encoding="utf-8")
        tracker._stack.append((str(file_path), None))
        success, message = tracker.undo()
        assert success is True
        assert "已删除" in message
        assert not file_path.exists()

    def test_undo_restore_content(self, tmp_path: Path) -> None:
        """撤销文件修改时恢复原内容。"""
        tracker = UndoTracker()
        file_path = tmp_path / "file.txt"
        file_path.write_text("modified", encoding="utf-8")
        tracker._stack.append((str(file_path), "original"))
        success, message = tracker.undo()
        assert success is True
        assert "已恢复" in message
        assert file_path.read_text(encoding="utf-8") == "original"

    def test_undo_file_already_deleted(self, tmp_path: Path) -> None:
        """撤销新建文件但文件已不存在时返回成功。"""
        tracker = UndoTracker()
        non_existent = str(tmp_path / "gone.txt")
        tracker._stack.append((non_existent, None))
        success, message = tracker.undo()
        assert success is True
        assert "不存在" in message

    def test_undo_restores_parent_dir(self, tmp_path: Path) -> None:
        """撤销时若父目录不存在则自动创建。"""
        tracker = UndoTracker()
        nested_path = tmp_path / "new_dir" / "file.txt"
        tracker._stack.append((str(nested_path), "content"))
        success, message = tracker.undo()
        assert success is True
        assert nested_path.read_text(encoding="utf-8") == "content"

    def test_read_file_not_exist(self, tmp_path: Path) -> None:
        """_read_file 读取不存在的文件返回 None。"""
        tracker = UndoTracker()
        result = tracker._read_file(str(tmp_path / "nonexistent.txt"))
        assert result is None

    def test_read_file_with_content(self, tmp_path: Path) -> None:
        """_read_file 读取存在的文件返回内容。"""
        tracker = UndoTracker()
        file_path = tmp_path / "file.txt"
        file_path.write_text("hello world", encoding="utf-8")
        result = tracker._read_file(str(file_path))
        assert result == "hello world"


# ============================================================================
# TestTrackedToolWrapper
# ============================================================================


class TestTrackedToolWrapper:
    """TrackedToolWrapper 工具包装器测试。"""

    def test_wrapper_initialization(self, tmp_path: Path) -> None:
        """包装器初始化时复制 name 和 description。"""
        tool = _MockTool()
        tracker = UndoTracker()
        wrapper = TrackedToolWrapper(tool, tracker, str(tmp_path))
        assert wrapper.name == "mock_tool"
        assert wrapper.description == "Mock tool for testing"

    def test_wrapper_get_parameters(self, tmp_path: Path) -> None:
        """get_parameters 委托给原始工具。"""
        tool = _MockTool()
        tracker = UndoTracker()
        wrapper = TrackedToolWrapper(tool, tracker, str(tmp_path))
        params = wrapper.get_parameters()
        assert params == {"type": "object", "properties": {}}

    async def test_wrapper_execute_records_and_delegates(self, tmp_path: Path) -> None:
        """execute 记录原内容并委托给原始工具。"""
        file_path = tmp_path / "target.txt"
        file_path.write_text("old content", encoding="utf-8")
        tool = _MockTool()
        tracker = UndoTracker()
        wrapper = TrackedToolWrapper(tool, tracker, str(tmp_path))
        result = await wrapper.execute({"path": "target.txt"})
        assert result == "mock result"
        assert tool.execute_called is True
        # 撤销栈应记录一条
        assert len(tracker._stack) == 1
        abs_path, old_content = tracker._stack[0]
        assert old_content == "old content"

    async def test_wrapper_execute_no_path(self, tmp_path: Path) -> None:
        """execute 无 path 参数时不记录撤销栈但仍委托执行。"""
        tool = _MockTool()
        tracker = UndoTracker()
        wrapper = TrackedToolWrapper(tool, tracker, str(tmp_path))
        result = await wrapper.execute({})
        assert result == "mock result"
        assert len(tracker._stack) == 0

    async def test_wrapper_execute_new_file(self, tmp_path: Path) -> None:
        """execute 对新文件记录 old_content 为 None。"""
        tool = _MockTool()
        tracker = UndoTracker()
        wrapper = TrackedToolWrapper(tool, tracker, str(tmp_path))
        await wrapper.execute({"path": "brand_new.txt"})
        assert len(tracker._stack) == 1
        _, old_content = tracker._stack[0]
        assert old_content is None

    def test_wrapper_to_openai_format(self, tmp_path: Path) -> None:
        """to_openai_format 委托给原始工具。"""
        tool = _MockTool()
        tracker = UndoTracker()
        wrapper = TrackedToolWrapper(tool, tracker, str(tmp_path))
        fmt = wrapper.to_openai_format()
        assert fmt["type"] == "function"
        assert fmt["function"]["name"] == "mock_tool"

    def test_wrapper_to_anthropic_format(self, tmp_path: Path) -> None:
        """to_anthropic_format 委托给原始工具。"""
        tool = _MockTool()
        tracker = UndoTracker()
        wrapper = TrackedToolWrapper(tool, tracker, str(tmp_path))
        fmt = wrapper.to_anthropic_format()
        assert fmt["name"] == "mock_tool"

    async def test_wrapper_execute_absolute_path(self, tmp_path: Path) -> None:
        """execute 处理绝对路径。"""
        file_path = tmp_path / "abs_target.txt"
        file_path.write_text("abs content", encoding="utf-8")
        tool = _MockTool()
        tracker = UndoTracker()
        wrapper = TrackedToolWrapper(tool, tracker, str(tmp_path))
        await wrapper.execute({"path": str(file_path)})
        assert len(tracker._stack) == 1
        abs_path, old_content = tracker._stack[0]
        assert old_content == "abs content"

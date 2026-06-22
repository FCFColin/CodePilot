"""app 模块单元测试。

覆盖：App 组件组装、create_app 工厂、slash 命令处理、run_single、
UndoTracker 撤销逻辑、TrackedToolWrapper 工具包装器。
使用 mock 隔离 provider 网络调用，tmp_path 隔离文件系统。
"""

from __future__ import annotations

import builtins
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from codepilot.app import App, TrackedToolWrapper, UndoTracker, create_app
from codepilot.config import (
    Config,
    ProviderConfig,
    SecurityConfig,
)
from codepilot.exceptions import CodePilotError
from codepilot.providers.anthropic import AnthropicProvider
from codepilot.providers.openai_compat import OpenAICompatProvider
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
    provider: str = "xunfei",
) -> Config:
    """构造测试用 Config，workspace_root 指向 tmp_path。"""
    _clear_codepilot_env(pytest.MonkeyPatch())
    return Config(
        provider=provider,
        providers={
            "xunfei": ProviderConfig(
                api_key=SecretStr("sk-test-xunfei"),
                base_url="https://maas-coding-api.cn-huabei-1.xf-yun.com/v2",
                model="astron-code-latest",
                temperature=1.0,
            ),
            "deepseek": ProviderConfig(
                api_key=SecretStr("sk-test-deepseek"),
                base_url="https://api.deepseek.com",
                model="deepseek-reasoner",
            ),
            "anthropic": ProviderConfig(
                type="anthropic",
                api_key=SecretStr("sk-test-anthropic"),
                base_url="https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic",
                model="astron-code-latest",
            ),
        },
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

    def test_app_creates_xunfei_provider(self, tmp_path: Path) -> None:
        """App 使用 xunfei provider 时创建 OpenAICompatProvider。"""
        config = _make_config(tmp_path, provider="xunfei")
        app = App(config)
        assert isinstance(app.provider, OpenAICompatProvider)

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

    def test_app_repo_mapper_initialized(self, tmp_path: Path) -> None:
        """App 初始化 repo_mapper 属性并注入 AgentLoop。"""
        config = _make_config(tmp_path)
        app = App(config)
        # repo_mapper 属性存在（tree-sitter 可用时为 RepoMapper，否则 None）
        assert hasattr(app, "repo_mapper")
        # AgentLoop 持有相同的 repo_mapper 引用
        assert app.agent_loop.repo_mapper is app.repo_mapper

    def test_app_repo_mapper_disabled_returns_none(self, tmp_path: Path) -> None:
        """repomap.enabled 为 False 时 repo_mapper 为 None。"""
        config = _make_config(tmp_path)
        config.repomap.enabled = False
        app = App(config)
        assert app.repo_mapper is None
        assert app.agent_loop.repo_mapper is None

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

    async def test_slash_compact_error(self, tmp_path: Path) -> None:
        """/compact 命令压缩失败时显示错误。"""
        config = _make_config(tmp_path)
        app = App(config)
        app.context_manager.force_compress = AsyncMock(
            side_effect=CodePilotError("compression failed")
        )
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
        original_model = config.providers["xunfei"].model
        result = await app._handle_slash_command("/model new-model-name")
        assert result is False
        assert config.providers["xunfei"].model == "new-model-name"
        assert config.providers["xunfei"].model != original_model

    async def test_slash_model_with_arg_anthropic(self, tmp_path: Path) -> None:
        """/model 带参数时切换 anthropic 模型。"""
        config = _make_config(tmp_path, provider="anthropic")
        app = App(config)
        result = await app._handle_slash_command("/model claude-3-opus")
        assert result is False
        assert config.providers["anthropic"].model == "claude-3-opus"

    async def test_slash_provider_no_arg(self, tmp_path: Path) -> None:
        """/provider 无参数时显示当前 provider 并返回 False。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/provider")
        assert result is False

    async def test_slash_provider_with_arg(self, tmp_path: Path) -> None:
        """/provider 带参数时切换 provider 并返回 False。"""
        config = _make_config(tmp_path, provider="xunfei")
        app = App(config)
        result = await app._handle_slash_command("/provider deepseek")
        assert result is False
        assert config.provider == "deepseek"
        assert app.display.provider_name == "deepseek"

    async def test_slash_provider_invalid_arg(self, tmp_path: Path) -> None:
        """/provider 带无效参数时不切换并返回 False。"""
        config = _make_config(tmp_path, provider="xunfei")
        app = App(config)
        result = await app._handle_slash_command("/provider invalid")
        assert result is False
        assert config.provider == "xunfei"

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

    async def test_slash_approve_toggle_restores_approval_list(self, tmp_path: Path) -> None:
        """/approve 关闭 YOLO 时恢复默认审批列表。"""
        config = _make_config(tmp_path)
        app = App(config)
        # 先清空审批列表，然后开启 YOLO
        app.approval.require_approval_for = set()
        await app._handle_slash_command("/approve")
        assert app.approval._yolo_mode is True
        # 关闭 YOLO，此时审批列表为空，应恢复默认
        await app._handle_slash_command("/approve")
        assert app.approval._yolo_mode is False
        assert "file_write" in app.approval.require_approval_for

    async def test_slash_undo_empty(self, tmp_path: Path) -> None:
        """/undo 命令在空栈时返回 False。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/undo")
        assert result is False

    async def test_slash_undo_git_codepilot_commit(self, tmp_path: Path) -> None:
        """/undo 在 git 仓库中优先撤销 codepilot 提交。"""
        # 初始化 git 仓库
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "test"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        # 创建文件并通过 GitManager 自动提交
        file_path = tmp_path / "undo_target.py"
        file_path.write_text("x = 1\n", encoding="utf-8")
        app = App(_make_config(tmp_path))
        app.git_manager.auto_commit("add undo_target.py", [file_path])

        # /undo 应撤销 git 提交
        result = await app._handle_slash_command("/undo")
        assert result is False
        # 验证 git log 中已无 codepilot 提交
        log_result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert "[codepilot]" not in log_result.stdout

    async def test_slash_undo_git_fallback_to_memory(self, tmp_path: Path) -> None:
        """/undo 在 git 仓库中非 codepilot 提交时回退到内存撤销。"""
        # 初始化 git 仓库
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "test"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        # 手动提交（非 codepilot）
        file_path = tmp_path / "manual.py"
        file_path.write_text("manual = True\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "manual.py"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "manual commit"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )

        config = _make_config(tmp_path)
        app = App(config)
        # /undo 应回退到内存撤销（git 撤销失败因非 codepilot 提交）
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

    def test_undo_os_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """撤销时遇到 OSError 返回失败。"""
        tracker = UndoTracker()
        file_path = tmp_path / "file.txt"
        file_path.write_text("content", encoding="utf-8")
        tracker._stack.append((str(file_path), "original"))

        original_open = builtins.open

        def _mock_open(*args: Any, **kwargs: Any) -> Any:
            if len(args) > 0 and isinstance(args[0], str) and "file.txt" in args[0]:
                raise OSError("permission denied")
            return original_open(*args, **kwargs)

        monkeypatch.setattr("builtins.open", _mock_open)
        success, message = tracker.undo()
        assert success is False
        assert "撤销失败" in message

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

    def test_read_file_os_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_read_file 遇到 OSError 返回 None。"""
        tracker = UndoTracker()
        file_path = tmp_path / "file.txt"
        file_path.write_text("content", encoding="utf-8")

        original_open = builtins.open

        def _mock_open(*args: Any, **kwargs: Any) -> Any:
            if len(args) > 0 and isinstance(args[0], str) and "file.txt" in args[0]:
                raise OSError("permission denied")
            return original_open(*args, **kwargs)

        monkeypatch.setattr("builtins.open", _mock_open)
        result = tracker._read_file(str(file_path))
        assert result is None

    def test_mark_turn_start(self) -> None:
        """mark_turn_start 记录当前栈长度到轮次边界。"""
        tracker = UndoTracker()
        assert tracker._turn_boundaries == []
        tracker.mark_turn_start()  # 第 1 轮开始，栈长度=0
        assert tracker._turn_boundaries == [0]
        tracker._stack.append(("file1", None))
        tracker.mark_turn_start()  # 第 2 轮开始，栈长度=1
        assert tracker._turn_boundaries == [0, 1]
        tracker._stack.append(("file2", "old"))
        tracker.mark_turn_start()  # 第 3 轮开始，栈长度=2
        assert tracker._turn_boundaries == [0, 1, 2]

    def test_undo_to_turn_basic(self, tmp_path: Path) -> None:
        """undo_to_turn 撤销目标轮次之后的所有文件操作。"""
        tracker = UndoTracker()
        # 第 1 轮
        tracker.mark_turn_start()
        file1 = tmp_path / "file1.txt"
        file1.write_text("new1", encoding="utf-8")
        tracker._stack.append((str(file1), None))
        # 第 2 轮
        tracker.mark_turn_start()
        file2 = tmp_path / "file2.txt"
        file2.write_text("new2", encoding="utf-8")
        tracker._stack.append((str(file2), "old2"))
        # 第 3 轮
        tracker.mark_turn_start()
        file3 = tmp_path / "file3.txt"
        file3.write_text("new3", encoding="utf-8")
        tracker._stack.append((str(file3), None))

        # 回退到第 1 轮：撤销第 2、3 轮的文件操作
        undone, failed = tracker.undo_to_turn(1)
        assert undone == 2
        assert failed == 0
        assert not file3.exists()
        assert file2.read_text(encoding="utf-8") == "old2"
        assert file1.exists()  # 第 1 轮的文件保留
        assert len(tracker._stack) == 1
        assert tracker._turn_boundaries == [0]

    def test_undo_to_turn_no_boundaries(self) -> None:
        """undo_to_turn 无轮次边界时返回 (0, 0)。"""
        tracker = UndoTracker()
        undone, failed = tracker.undo_to_turn(1)
        assert undone == 0
        assert failed == 0

    def test_undo_to_turn_invalid_turn(self) -> None:
        """undo_to_turn 轮次号超出范围时返回 (0, 0)。"""
        tracker = UndoTracker()
        tracker.mark_turn_start()
        undone, failed = tracker.undo_to_turn(0)  # 太小
        assert undone == 0
        undone, failed = tracker.undo_to_turn(2)  # 太大
        assert undone == 0

    def test_undo_to_turn_os_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """undo_to_turn 遇到 OSError 时计入 failed_count。"""
        tracker = UndoTracker()
        tracker.mark_turn_start()
        file1 = tmp_path / "file1.txt"
        file1.write_text("new1", encoding="utf-8")
        tracker._stack.append((str(file1), "old1"))
        tracker.mark_turn_start()
        file2 = tmp_path / "file2.txt"
        file2.write_text("new2", encoding="utf-8")
        tracker._stack.append((str(file2), "old2"))

        original_open = builtins.open

        def _mock_open(*args: Any, **kwargs: Any) -> Any:
            if len(args) > 0 and isinstance(args[0], str) and "file2.txt" in args[0]:
                raise OSError("permission denied")
            return original_open(*args, **kwargs)

        monkeypatch.setattr("builtins.open", _mock_open)
        # 回退到第 1 轮：撤销第 2 轮的 file2（会失败）
        undone, failed = tracker.undo_to_turn(1)
        assert undone == 0  # file2 恢复失败
        assert failed == 1


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


# ============================================================================
# TestSessionIntegration
# ============================================================================


class TestSessionIntegration:
    """Session 与 App 集成测试（/sessions、/export、resume_from_history）。"""

    async def test_slash_sessions_empty(self, tmp_path: Path) -> None:
        """/sessions 命令在无历史会话时返回 False。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/sessions")
        assert result is False

    async def test_slash_sessions_with_data(self, tmp_path: Path) -> None:
        """/sessions 命令显示最近会话列表。"""
        config = _make_config(tmp_path)
        app = App(config)
        # 通过 session_manager 添加消息并保存
        app.session_manager.add_message("user", "test message")
        app.session_manager.save()
        result = await app._handle_slash_command("/sessions")
        assert result is False

    async def test_slash_export_markdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/export markdown 导出会话到 .md 文件。"""
        monkeypatch.chdir(tmp_path)
        config = _make_config(tmp_path)
        app = App(config)
        app.session_manager.add_message("user", "export test")
        result = await app._handle_slash_command("/export markdown")
        assert result is False
        # 验证文件存在
        exported_files = list(tmp_path.glob("codepilot-session-*.md"))
        assert len(exported_files) == 1

    async def test_slash_export_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/export json 导出会话到 .json 文件。"""
        monkeypatch.chdir(tmp_path)
        config = _make_config(tmp_path)
        app = App(config)
        app.session_manager.add_message("user", "json export test")
        result = await app._handle_slash_command("/export json")
        assert result is False
        exported_files = list(tmp_path.glob("codepilot-session-*.json"))
        assert len(exported_files) == 1

    async def test_slash_export_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/export 无参数默认导出 markdown。"""
        monkeypatch.chdir(tmp_path)
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/export")
        assert result is False
        exported_files = list(tmp_path.glob("codepilot-session-*.md"))
        assert len(exported_files) == 1

    async def test_slash_export_invalid_format(self, tmp_path: Path) -> None:
        """/export 无效格式返回 False 且不导出。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/export xml")
        assert result is False

    async def test_slash_export_get_record_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/export 获取会话记录失败时显示错误。"""
        monkeypatch.chdir(tmp_path)
        config = _make_config(tmp_path)
        app = App(config)
        app.session_manager.get_record = MagicMock(
            side_effect=RuntimeError("record error")
        )
        result = await app._handle_slash_command("/export")
        assert result is False

    async def test_slash_export_os_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/export 写文件失败时显示错误。"""
        monkeypatch.chdir(tmp_path)
        config = _make_config(tmp_path)
        app = App(config)
        app.session_manager.add_message("user", "test")

        # Mock Path.write_text to raise OSError
        def _mock_write_text(self_path: Any, *args: Any, **kwargs: Any) -> Any:
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", _mock_write_text)
        result = await app._handle_slash_command("/export")
        assert result is False

    async def test_resume_from_history_no_sessions(self, tmp_path: Path) -> None:
        """无历史会话时 resume_from_history 返回 False。"""
        # 使用空目录的 storage
        config = _make_config(tmp_path)
        app = App(config)
        # 替换为空 storage（覆盖默认 storage 中的数据）
        from codepilot.session import SessionStorage

        app.session_storage = SessionStorage(sessions_dir=tmp_path / "empty_sessions")
        result = await app.resume_from_history()
        assert result is False

    async def test_resume_from_history_with_session_id(self, tmp_path: Path) -> None:
        """resume_from_history 加载指定会话历史。"""
        config = _make_config(tmp_path)
        app = App(config)
        # 保存一条历史
        app.session_manager.add_message("user", "历史问题")
        app.session_manager.add_message("assistant", "历史回答")
        app.session_manager.save()
        session_id = app.session_manager.get_record()["session_id"]

        # 清空 context_manager 后恢复
        await app.context_manager.clear()
        result = await app.resume_from_history(session_id)
        assert result is True
        # 验证 context_manager 包含历史消息
        assert len(app.context_manager.messages) == 2

    async def test_resume_from_history_latest(self, tmp_path: Path) -> None:
        """resume_from_history 无参数加载最近会话。"""
        config = _make_config(tmp_path)
        app = App(config)
        app.session_manager.add_message("user", "最近会话消息")
        app.session_manager.save()

        await app.context_manager.clear()
        result = await app.resume_from_history()
        assert result is True
        assert len(app.context_manager.messages) == 1

    async def test_resume_from_history_not_found(self, tmp_path: Path) -> None:
        """resume_from_history 加载不存在的会话返回 False。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app.resume_from_history("nonexistent-id-9999")
        assert result is False

    async def test_resume_from_history_empty_messages(self, tmp_path: Path) -> None:
        """resume_from_history 加载无消息的会话返回 False。"""
        config = _make_config(tmp_path)
        app = App(config)
        # 保存一个空会话
        app.session_manager.save()
        session_id = app.session_manager.get_record()["session_id"]

        result = await app.resume_from_history(session_id)
        assert result is False


# ============================================================================
# TestHookRegistry
# ============================================================================


class TestHookRegistry:
    """_create_hook_registry 覆盖测试。"""

    def test_hook_registry_with_auto_lint(self, tmp_path: Path) -> None:
        """auto_lint=True 时注册 LintHook。"""
        config = _make_config(tmp_path)
        config.hooks.auto_lint = True
        config.hooks.auto_git_commit = False
        app = App(config)
        # hook_registry 应包含一个 LintHook
        assert len(app.hook_registry._hooks) >= 1
        hook_names = [h.name() for h in app.hook_registry._hooks]
        assert "auto_lint" in hook_names

    def test_hook_registry_with_auto_git_commit(self, tmp_path: Path) -> None:
        """auto_git_commit=True 且 git.auto_commit=True 时注册 GitCommitHook。"""
        config = _make_config(tmp_path)
        config.hooks.auto_lint = False
        config.hooks.auto_git_commit = True
        config.git.auto_commit = True
        app = App(config)
        hook_names = [h.name() for h in app.hook_registry._hooks]
        assert "auto_git_commit" in hook_names

    def test_hook_registry_no_git_commit_when_disabled(self, tmp_path: Path) -> None:
        """auto_git_commit=True 但 git.auto_commit=False 时不注册 GitCommitHook。"""
        config = _make_config(tmp_path)
        config.hooks.auto_lint = False
        config.hooks.auto_git_commit = True
        config.git.auto_commit = False
        app = App(config)
        hook_names = [h.name() for h in app.hook_registry._hooks]
        assert "auto_git_commit" not in hook_names

    def test_hook_registry_both_disabled(self, tmp_path: Path) -> None:
        """auto_lint=False 且 auto_git_commit=False 时无 Hook 注册。"""
        config = _make_config(tmp_path)
        config.hooks.auto_lint = False
        config.hooks.auto_git_commit = False
        app = App(config)
        assert len(app.hook_registry._hooks) == 0

    def test_hook_registry_both_enabled(self, tmp_path: Path) -> None:
        """auto_lint=True 且 auto_git_commit=True + git.auto_commit=True 时两个 Hook 都注册。"""
        config = _make_config(tmp_path)
        config.hooks.auto_lint = True
        config.hooks.auto_git_commit = True
        config.git.auto_commit = True
        app = App(config)
        hook_names = [h.name() for h in app.hook_registry._hooks]
        assert "auto_lint" in hook_names
        assert "auto_git_commit" in hook_names


# ============================================================================
# TestRepoMapperCreation
# ============================================================================


class TestRepoMapperCreation:
    """_create_repo_mapper 覆盖测试。"""

    def test_repo_mapper_exception_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_create_repo_mapper 在 RepoMapper 构造异常时返回 None。"""
        config = _make_config(tmp_path)
        config.repomap.enabled = True

        from codepilot import repomap as repomap_module

        def _raise_error(**kwargs: Any) -> None:
            raise RuntimeError("tree-sitter not available")

        monkeypatch.setattr(repomap_module.RepoMapper, "__init__", _raise_error)
        app = App(config)
        assert app.repo_mapper is None

    def test_repo_mapper_not_available_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_create_repo_mapper 在 is_available() 返回 False 时返回 None。"""
        config = _make_config(tmp_path)
        config.repomap.enabled = True

        from codepilot import repomap as repomap_module

        original_init = repomap_module.RepoMapper.__init__

        def _mock_init(self: Any, **kwargs: Any) -> None:
            original_init(self, **kwargs)

        monkeypatch.setattr(repomap_module.RepoMapper, "is_available", lambda self: False)
        app = App(config)
        assert app.repo_mapper is None


# ============================================================================
# TestGitIntegration
# ============================================================================


class TestGitIntegration:
    """Git 集成相关测试。"""

    def test_git_manager_initialized(self, tmp_path: Path) -> None:
        """App 初始化时创建 GitManager。"""
        config = _make_config(tmp_path)
        app = App(config)
        assert app.git_manager is not None

    def test_tracked_tool_wrapper_auto_commit(
        self, tmp_path: Path
    ) -> None:
        """TrackedToolWrapper 在 auto_commit 启用且 git 仓库中自动提交。"""
        import subprocess

        # 初始化 git 仓库
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "test"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )

        config = _make_config(tmp_path)
        config.git.auto_commit = True
        app = App(config)

        # 获取 write_file 工具（应为 TrackedToolWrapper）
        write_tool = app.tool_registry.get("write_file")
        assert isinstance(write_tool, TrackedToolWrapper)
        assert write_tool._auto_commit_enabled is True

    def test_tracked_tool_wrapper_no_auto_commit_when_disabled(
        self, tmp_path: Path
    ) -> None:
        """TrackedToolWrapper 在 auto_commit 禁用时不自动提交。"""
        config = _make_config(tmp_path)
        config.git.auto_commit = False
        app = App(config)

        write_tool = app.tool_registry.get("write_file")
        assert isinstance(write_tool, TrackedToolWrapper)
        assert write_tool._auto_commit_enabled is False


# ============================================================================
# TestSessionManagerCreation
# ============================================================================


class TestSessionManagerCreation:
    """Session 集成相关测试。"""

    def test_session_manager_initialized(self, tmp_path: Path) -> None:
        """App 初始化时创建 SessionManager 并开始新会话。"""
        config = _make_config(tmp_path)
        app = App(config)
        assert app.session_manager is not None
        assert app.session_storage is not None
        assert app.session_exporter is not None

    def test_session_manager_model_xunfei(self, tmp_path: Path) -> None:
        """xunfei provider 时 session_manager 使用 xunfei model。"""
        config = _make_config(tmp_path, provider="xunfei")
        app = App(config)
        assert app.session_manager.model == config.providers["xunfei"].model

    def test_session_manager_model_anthropic(self, tmp_path: Path) -> None:
        """anthropic provider 时 session_manager 使用 anthropic model。"""
        config = _make_config(tmp_path, provider="anthropic")
        app = App(config)
        assert app.session_manager.model == config.providers["anthropic"].model


# ============================================================================
# TestMultiProviderAppCreation
# ============================================================================


class TestMultiProviderAppCreation:
    """多 Provider 配置下 App 创建测试。"""

    def _make_multi_provider_config(
        self,
        tmp_path: Path,
        active_provider: str = "provider_a",
    ) -> Config:
        """构造带 providers 字典的测试用 Config。"""
        _clear_codepilot_env(pytest.MonkeyPatch())
        return Config(
            provider=active_provider,
            providers={
                "provider_a": ProviderConfig(
                    type="openai",
                    api_key=SecretStr("sk-test-a"),
                    base_url="https://a.example.com/v1",
                    model="model-a",
                ),
                "provider_b": ProviderConfig(
                    type="anthropic",
                    api_key=SecretStr("sk-test-b"),
                    base_url="https://b.example.com",
                    model="model-b",
                ),
            },
            security=SecurityConfig(
                workspace_root=str(tmp_path),
                blocked_paths=[],
            ),
        )

    def test_app_creates_openai_compat_from_provider_config(
        self, tmp_path: Path
    ) -> None:
        """providers 中 type=openai 时创建 OpenAICompatProvider。"""
        config = self._make_multi_provider_config(tmp_path, "provider_a")
        app = App(config)
        assert isinstance(app.provider, OpenAICompatProvider)

    def test_app_creates_anthropic_from_provider_config(
        self, tmp_path: Path
    ) -> None:
        """providers 中 type=anthropic 时创建 AnthropicProvider。"""
        config = self._make_multi_provider_config(tmp_path, "provider_b")
        app = App(config)
        assert isinstance(app.provider, AnthropicProvider)

    def test_session_manager_model_from_providers(
        self, tmp_path: Path
    ) -> None:
        """providers 格式时 session_manager 使用正确的模型名。"""
        config = self._make_multi_provider_config(tmp_path, "provider_a")
        app = App(config)
        assert app.session_manager.model == "model-a"

    async def test_slash_provider_shows_available_providers(
        self, tmp_path: Path
    ) -> None:
        """/provider 无参数时显示可用 provider 列表。"""
        config = self._make_multi_provider_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/provider")
        assert result is False

    async def test_slash_provider_switch_to_other_provider(
        self, tmp_path: Path
    ) -> None:
        """/provider 切换到 providers 中的其他 provider。"""
        config = self._make_multi_provider_config(tmp_path, "provider_a")
        app = App(config)
        result = await app._handle_slash_command("/provider provider_b")
        assert result is False
        assert config.provider == "provider_b"

    async def test_slash_model_updates_providers_dict(self, tmp_path: Path) -> None:
        """/model 更新 providers 字典中的活跃 provider 模型。"""
        config = self._make_multi_provider_config(tmp_path, "provider_a")
        app = App(config)
        result = await app._handle_slash_command("/model new-model-a")
        assert result is False
        assert config.providers["provider_a"].model == "new-model-a"


# ============================================================================
# TestRollbackPlanProviders
# ============================================================================


class TestRollbackPlanProviders:
    """/rollback、/plan、/providers 命令测试。"""

    async def test_slash_rollback_invalid_arg(self, tmp_path: Path) -> None:
        """/rollback 无效参数时显示错误。"""
        config = _make_config(tmp_path)
        app = App(config)
        # 非数字参数
        result = await app._handle_slash_command("/rollback abc")
        assert result is False
        # 无参数
        result = await app._handle_slash_command("/rollback")
        assert result is False

    async def test_slash_rollback_out_of_range(self, tmp_path: Path) -> None:
        """/rollback 轮次号超出范围时显示错误。"""
        config = _make_config(tmp_path)
        app = App(config)
        # 添加 2 轮对话
        await app.context_manager.add_message("user", "问题1")
        await app.context_manager.add_message("assistant", "回答1")
        await app.context_manager.add_message("user", "问题2")
        await app.context_manager.add_message("assistant", "回答2")
        # 轮次号 0（太小）
        result = await app._handle_slash_command("/rollback 0")
        assert result is False
        # 轮次号 5（超出范围）
        result = await app._handle_slash_command("/rollback 5")
        assert result is False
        # 轮次号 -1（负数）
        result = await app._handle_slash_command("/rollback -1")
        assert result is False

    async def test_slash_rollback_valid(self, tmp_path: Path) -> None:
        """/rollback 有效轮次号时删除后续消息。"""
        config = _make_config(tmp_path)
        app = App(config)
        # 添加 3 轮对话
        await app.context_manager.add_message("user", "问题1")
        await app.context_manager.add_message("assistant", "回答1")
        await app.context_manager.add_message("user", "问题2")
        await app.context_manager.add_message("assistant", "回答2")
        await app.context_manager.add_message("user", "问题3")
        await app.context_manager.add_message("assistant", "回答3")
        assert len(app.context_manager.messages) == 6
        # 回退到第 1 轮
        result = await app._handle_slash_command("/rollback 1")
        assert result is False
        assert len(app.context_manager.messages) == 2

    async def test_slash_rollback_no_need(self, tmp_path: Path) -> None:
        """/rollback 目标轮次等于当前轮次时无需回退。"""
        config = _make_config(tmp_path)
        app = App(config)
        # 添加 2 轮对话
        await app.context_manager.add_message("user", "问题1")
        await app.context_manager.add_message("assistant", "回答1")
        await app.context_manager.add_message("user", "问题2")
        await app.context_manager.add_message("assistant", "回答2")
        # 回退到第 2 轮（等于当前轮次）
        result = await app._handle_slash_command("/rollback 2")
        assert result is False
        # 消息数不变
        assert len(app.context_manager.messages) == 4

    async def test_slash_plan_no_plan(self, tmp_path: Path) -> None:
        """/plan 无活跃计划时显示提示。"""
        from codepilot.tools.plan_tool import PlanTool

        PlanTool.clear_plan()
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/plan")
        assert result is False

    async def test_slash_plan_with_plan(self, tmp_path: Path) -> None:
        """/plan 有活跃计划时显示计划状态。"""
        from codepilot.tools.plan_tool import PlanTool

        PlanTool.clear_plan()
        # 创建一个计划
        tool = PlanTool()
        tool._create_plan({
            "title": "测试计划",
            "steps": [
                {"id": "s1", "description": "步骤1"},
                {"id": "s2", "description": "步骤2"},
            ],
        })
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/plan")
        assert result is False
        # 清理
        PlanTool.clear_plan()

    async def test_slash_providers(self, tmp_path: Path) -> None:
        """/providers 显示所有已配置的 provider。"""
        config = _make_config(tmp_path)
        app = App(config)
        result = await app._handle_slash_command("/providers")
        assert result is False

"""UI 模块单元测试。

覆盖：DisplayManager 所有 UICallback 回调方法与 slash 命令显示方法、
show_banner 启动 banner、render_diff / render_new_file diff 着色。
使用 StringIO + rich Console 捕获输出，mock 隔离 context_manager。
"""

from __future__ import annotations

from io import StringIO
from typing import Any

from pydantic import SecretStr
from rich.console import Console

from codepilot.config import (
    AnthropicConfig,
    Config,
    DeepSeekConfig,
    UIConfig,
)
from codepilot.providers.base import Message
from codepilot.ui.banner import (
    _get_context_display,
    _get_provider_display,
    _get_security_display,
    show_banner,
)
from codepilot.ui.diff_view import render_diff, render_new_file
from codepilot.ui.display import DisplayManager

# ============================================================================
# 辅助函数
# ============================================================================


def _make_config(provider: str = "deepseek") -> Config:
    """构造测试用 Config。"""
    return Config(
        provider=provider,
        deepseek=DeepSeekConfig(api_key=SecretStr("sk-test-deepseek")),
        anthropic=AnthropicConfig(api_key=SecretStr("sk-test-anthropic")),
    )


def _make_display(
    provider_name: str = "deepseek",
    context_manager: Any = None,
    **ui_kwargs: Any,
) -> tuple[DisplayManager, StringIO]:
    """创建 DisplayManager 并替换 console 为 StringIO-based Console。"""
    config = UIConfig(**ui_kwargs)
    display = DisplayManager(
        config=config,
        provider_name=provider_name,
        context_manager=context_manager,
    )
    output = StringIO()
    display.console = Console(
        file=output, force_terminal=False, no_color=True, width=120
    )
    return display, output


def _make_context_manager_mock(
    total_tokens: int = 1000,
    max_tokens: int = 10000,
    utilization: float = 0.1,
) -> Any:
    """创建 mock context_manager。"""

    class _MockCM:
        def get_stats(self) -> dict[str, Any]:
            return {
                "total_tokens": total_tokens,
                "max_tokens": max_tokens,
                "utilization": utilization,
                "message_count": 5,
                "compression_count": 0,
            }

    return _MockCM()


# ============================================================================
# TestDisplayManager
# ============================================================================


class TestDisplayManager:
    """DisplayManager 回调与显示方法测试。"""

    async def test_on_text_delta(self) -> None:
        """on_text_delta 累积文本到 _current_text。"""
        display, _ = _make_display()
        await display.on_text_delta("Hello")
        assert display._current_text == "Hello"
        await display.on_text_delta(" World")
        assert display._current_text == "Hello World"
        await display.on_turn_end()

    async def test_on_thinking_delta(self) -> None:
        """on_thinking_delta 输出思考面板。"""
        display, output = _make_display(show_thinking=True)
        await display.on_thinking_delta("思考中...")
        result = output.getvalue()
        assert "思考中..." in result
        assert "Thinking" in result

    async def test_on_thinking_delta_disabled(self) -> None:
        """show_thinking=False 时不输出思考面板。"""
        display, output = _make_display(show_thinking=False)
        await display.on_thinking_delta("思考中...")
        result = output.getvalue()
        assert "思考中..." not in result

    async def test_on_tool_call(self) -> None:
        """on_tool_call 输出工具调用面板。"""
        display, output = _make_display(show_tool_calls=True)
        await display.on_tool_call("read_file", {"path": "test.py"})
        result = output.getvalue()
        assert "read_file" in result
        assert "test.py" in result
        assert "工具调用" in result

    async def test_on_tool_call_disabled(self) -> None:
        """show_tool_calls=False 时不输出工具调用面板。"""
        display, output = _make_display(show_tool_calls=False)
        await display.on_tool_call("read_file", {"path": "test.py"})
        result = output.getvalue()
        assert "read_file" not in result

    async def test_on_tool_result(self) -> None:
        """on_tool_result 输出工具结果面板。"""
        display, output = _make_display(show_tool_calls=True)
        await display.on_tool_result("read_file", "file content here", True)
        result = output.getvalue()
        assert "read_file" in result
        assert "file content here" in result

    async def test_on_tool_result_truncated(self) -> None:
        """on_tool_result 截断超长结果。"""
        display, _ = _make_display(show_tool_calls=True, max_diff_lines=2)
        long_result = "\n".join(f"line {i}" for i in range(10))
        await display.on_tool_result("read_file", long_result, True)
        # 不抛异常即可
        await display.on_turn_end()

    async def test_on_usage(self) -> None:
        """on_usage 输出 token 用量面板。"""
        cm = _make_context_manager_mock()
        display, output = _make_display(
            context_manager=cm,
            show_token_usage=True,
            show_cost_estimate=True,
        )
        await display.on_usage(100, 50)
        result = output.getvalue()
        assert "Input" in result
        assert "100" in result
        assert "Output" in result

    async def test_on_usage_no_context_manager(self) -> None:
        """无 context_manager 时 on_usage 仍正常输出。"""
        display, output = _make_display(
            context_manager=None,
            show_token_usage=True,
        )
        await display.on_usage(100, 50)
        result = output.getvalue()
        assert "Input" in result

    async def test_on_usage_disabled(self) -> None:
        """show_token_usage=False 时不输出用量面板。"""
        display, output = _make_display(show_token_usage=False)
        await display.on_usage(100, 50)
        result = output.getvalue()
        assert "Input" not in result

    async def test_on_error(self) -> None:
        """on_error 输出错误面板。"""
        display, output = _make_display()
        await display.on_error("something went wrong")
        result = output.getvalue()
        assert "something went wrong" in result
        assert "Error" in result

    async def test_on_turn_end(self) -> None:
        """on_turn_end 停止 Live 并清理状态。"""
        display, _ = _make_display()
        await display.on_text_delta("text")
        assert display._live is not None
        await display.on_turn_end()
        assert display._live is None

    def test_show_help(self) -> None:
        """show_help 输出帮助信息。"""
        display, output = _make_display()
        display.show_help()
        result = output.getvalue()
        assert "可用命令" in result
        assert "/help" in result
        assert "/config" in result
        assert "/quit" in result

    def test_show_config_deepseek(self) -> None:
        """show_config 输出 deepseek 配置。"""
        config = _make_config(provider="deepseek")
        display, output = _make_display(provider_name="deepseek")
        display.show_config(config)
        result = output.getvalue()
        assert "deepseek" in result
        assert "Config" in result

    def test_show_config_anthropic(self) -> None:
        """show_config 输出 anthropic 配置。"""
        config = _make_config(provider="anthropic")
        display, output = _make_display(provider_name="anthropic")
        display.show_config(config)
        result = output.getvalue()
        assert "anthropic" in result
        assert "Config" in result

    def test_show_stats(self) -> None:
        """show_stats 输出统计信息。"""
        display, output = _make_display(provider_name="deepseek")
        stats = {
            "total_tokens": 5000,
            "max_tokens": 120000,
            "utilization": 0.0417,
            "message_count": 10,
            "compression_count": 2,
        }
        display.show_stats(stats)  # type: ignore[arg-type]
        result = output.getvalue()
        assert "Stats" in result
        assert "5,000" in result

    def test_show_history_empty(self) -> None:
        """show_history 无消息时输出空提示。"""
        display, output = _make_display()
        display.show_history([])
        result = output.getvalue()
        assert "无对话历史" in result

    def test_show_sessions_empty(self) -> None:
        """show_sessions 无会话时输出空提示。"""
        display, output = _make_display()
        display.show_sessions([])
        result = output.getvalue()
        assert "无历史会话" in result

    def test_show_sessions_with_data(self) -> None:
        """show_sessions 有会话时输出会话列表。"""
        display, output = _make_display()
        sessions: list[dict[str, Any]] = [
            {
                "session_id": "abc12345-1700000000",
                "start_time": "2026-01-01T10:00:00",
                "end_time": None,
                "workspace_root": "/tmp",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ],
                "tool_calls": [],
                "token_usage": {"input_tokens": 10, "output_tokens": 5, "total": 15},
                "provider": "deepseek",
                "model": "test-model",
            }
        ]
        display.show_sessions(sessions)
        result = output.getvalue()
        assert "abc12345-1700000000" in result
        assert "2026-01-01T10:00:00" in result

    def test_show_history_with_messages(self) -> None:
        """show_history 有消息时输出历史概要。"""
        display, output = _make_display()
        messages = [
            Message(role="user", content="你好"),
            Message(role="assistant", content="你好，有什么可以帮你？"),
        ]
        display.show_history(messages)
        result = output.getvalue()
        assert "你好" in result
        assert "user" in result
        assert "assistant" in result

    def test_show_history_with_list_content(self) -> None:
        """show_history 处理 list 类型 content（Anthropic blocks）。"""
        display, output = _make_display()
        messages = [
            Message(
                role="assistant",
                content=[{"type": "text", "text": "block text"}],
            ),
        ]
        display.show_history(messages)
        result = output.getvalue()
        assert "block text" in result

    def test_on_user_input(self) -> None:
        """on_user_input 输出用户输入面板。"""
        display, output = _make_display()
        display.on_user_input("hello agent")
        result = output.getvalue()
        assert "hello agent" in result
        assert "用户输入" in result

    def test_on_security_block(self) -> None:
        """on_security_block 输出安全拒绝面板。"""
        display, output = _make_display()
        display.on_security_block("file_write", "path escapes workspace")
        result = output.getvalue()
        assert "file_write" in result
        assert "path escapes workspace" in result
        assert "SECURITY BLOCK" in result

    def test_on_compression(self) -> None:
        """on_compression 输出压缩通知面板。"""
        display, output = _make_display()
        display.on_compression(
            {
                "before_tokens": 10000,
                "after_tokens": 3000,
                "strategy": "summary",
                "messages_compressed": 20,
            }
        )
        result = output.getvalue()
        assert "Compression" in result
        assert "summary" in result
        assert "10,000" in result

    def test_on_compression_zero_before(self) -> None:
        """on_compression 处理 before_tokens=0 的情况。"""
        display, output = _make_display()
        display.on_compression(
            {
                "before_tokens": 0,
                "after_tokens": 0,
                "strategy": "none",
                "messages_compressed": 0,
            }
        )
        result = output.getvalue()
        assert "none" in result


# ============================================================================
# TestBanner
# ============================================================================


class TestBanner:
    """show_banner 及辅助函数测试。"""

    def test_show_banner_default(self) -> None:
        """show_banner 输出包含版本号和 provider 信息。"""
        config = _make_config(provider="deepseek")
        output = StringIO()
        console = Console(file=output, force_terminal=False, no_color=True, width=120)
        show_banner(config, console)
        result = output.getvalue()
        assert "AI Coding Agent CLI" in result
        assert "DeepSeek" in result
        assert "Commands" in result

    def test_show_banner_anthropic(self) -> None:
        """show_banner 输出 anthropic provider 信息。"""
        config = _make_config(provider="anthropic")
        output = StringIO()
        console = Console(file=output, force_terminal=False, no_color=True, width=120)
        show_banner(config, console)
        result = output.getvalue()
        assert "Anthropic" in result

    def test_show_banner_auto_console(self) -> None:
        """show_banner 未传 console 时自动创建（不抛异常）。"""
        config = _make_config()
        show_banner(config)
        # 不抛异常即可

    def test_get_provider_display_deepseek(self) -> None:
        """_get_provider_display 返回 DeepSeek 显示文本。"""
        config = _make_config(provider="deepseek")
        result = _get_provider_display(config)
        assert "DeepSeek" in result
        assert config.deepseek.model in result

    def test_get_provider_display_anthropic(self) -> None:
        """_get_provider_display 返回 Anthropic 显示文本。"""
        config = _make_config(provider="anthropic")
        result = _get_provider_display(config)
        assert "Anthropic" in result
        assert config.anthropic.model in result

    def test_get_security_display_with_approval(self) -> None:
        """_get_security_display 有审批列表时 Approval ON。"""
        config = _make_config()
        config.security.require_approval_for = ["file_write"]
        result = _get_security_display(config)
        assert "Sandbox ON" in result
        assert "Approval ON" in result

    def test_get_security_display_no_approval(self) -> None:
        """_get_security_display 无审批列表时 Approval OFF。"""
        config = _make_config()
        config.security.require_approval_for = []
        result = _get_security_display(config)
        assert "Sandbox ON" in result
        assert "Approval OFF" in result

    def test_get_context_display(self) -> None:
        """_get_context_display 返回上下文配置文本。"""
        config = _make_config()
        config.context.max_tokens = 120000
        config.context.compression_threshold = 0.70
        result = _get_context_display(config)
        assert "120K" in result
        assert "70%" in result


# ============================================================================
# TestDiffView
# ============================================================================


class TestDiffView:
    """render_diff 和 render_new_file 测试。"""

    def test_render_diff_with_changes(self) -> None:
        """render_diff 生成差异面板。"""
        panel = render_diff("old line\n", "new line\n")
        output = StringIO()
        console = Console(file=output, force_terminal=False, no_color=True, width=120)
        console.print(panel)
        result = output.getvalue()
        assert "Diff" in result
        assert "old line" in result
        assert "new line" in result

    def test_render_diff_no_changes(self) -> None:
        """render_diff 无差异时输出提示。"""
        panel = render_diff("same\n", "same\n")
        output = StringIO()
        console = Console(file=output, force_terminal=False, no_color=True, width=120)
        console.print(panel)
        result = output.getvalue()
        assert "无差异" in result

    def test_render_diff_truncated(self) -> None:
        """render_diff 截断超长 diff。"""
        old_lines = "\n".join(f"line {i}" for i in range(100))
        new_lines = "\n".join(f"line {i} modified" for i in range(100))
        panel = render_diff(old_lines, new_lines, max_lines=5)
        output = StringIO()
        console = Console(file=output, force_terminal=False, no_color=True, width=120)
        console.print(panel)
        result = output.getvalue()
        assert "omitted" in result

    def test_render_new_file(self) -> None:
        """render_new_file 渲染新文件内容。"""
        panel = render_new_file("line1\nline2\nline3\n")
        output = StringIO()
        console = Console(file=output, force_terminal=False, no_color=True, width=120)
        console.print(panel)
        result = output.getvalue()
        assert "New File" in result
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result

    def test_render_new_file_truncated(self) -> None:
        """render_new_file 截断超长内容。"""
        content = "\n".join(f"line {i}" for i in range(100))
        panel = render_new_file(content, max_lines=5)
        output = StringIO()
        console = Console(file=output, force_terminal=False, no_color=True, width=120)
        console.print(panel)
        result = output.getvalue()
        assert "omitted" in result

    def test_render_new_file_empty(self) -> None:
        """render_new_file 渲染空文件不抛异常。"""
        panel = render_new_file("")
        output = StringIO()
        console = Console(file=output, force_terminal=False, no_color=True, width=120)
        console.print(panel)
        result = output.getvalue()
        assert "New File" in result

    def test_render_diff_empty_inputs(self) -> None:
        """render_diff 空输入不抛异常。"""
        panel = render_diff("", "")
        output = StringIO()
        console = Console(file=output, force_terminal=False, no_color=True, width=120)
        console.print(panel)
        result = output.getvalue()
        assert "Diff" in result

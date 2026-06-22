"""AgentLoop 循环检测机制与辅助方法单元测试。

覆盖 _detect_loop 方法的各种场景：
- 无调用时不检测
- 不同工具不检测
- 相同工具 + 相似参数 → 检测到循环
- 相同工具 + 不同参数 → 不检测

覆盖辅助方法：
- cancel 设置 _cancelled 标志
- _record_session_message / _record_tool_call / _save_session 静默失败
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from codepilot.agent.loop import AgentLoop
from codepilot.context.manager import ContextManager
from codepilot.providers.base import BaseProvider
from codepilot.tools.registry import ToolRegistry

# ============================================================================
# 辅助：构建最小化 AgentLoop 实例
# ============================================================================


def _make_loop() -> AgentLoop:
    """构建一个最小化的 AgentLoop 实例，仅用于测试 _detect_loop。"""
    provider = AsyncMock(spec=BaseProvider)
    ctx_mgr = AsyncMock(spec=ContextManager)
    tool_reg = ToolRegistry()
    return AgentLoop(
        provider=provider,
        context_manager=ctx_mgr,
        tool_registry=tool_reg,
    )


# ============================================================================
# 测试用例
# ============================================================================


class TestDetectLoop:
    """_detect_loop 循环检测测试。"""

    def test_detect_loop_no_calls(self) -> None:
        """无调用时不检测到循环。"""
        loop = _make_loop()
        assert loop._detect_loop() is False

    def test_detect_loop_one_call(self) -> None:
        """仅 1 次调用时不检测到循环。"""
        loop = _make_loop()
        loop._recent_tool_calls = [("read_file", "{'path': 'a.py'}")]
        assert loop._detect_loop() is False

    def test_detect_loop_two_calls(self) -> None:
        """仅 2 次调用时不检测到循环（需要 3 次）。"""
        loop = _make_loop()
        loop._recent_tool_calls = [
            ("read_file", "{'path': 'a.py'}"),
            ("read_file", "{'path': 'a.py'}"),
        ]
        assert loop._detect_loop() is False

    def test_detect_loop_different_tools(self) -> None:
        """不同工具调用不检测到循环。"""
        loop = _make_loop()
        loop._recent_tool_calls = [
            ("read_file", "{'path': 'a.py'}"),
            ("write_file", "{'path': 'a.py'}"),
            ("read_file", "{'path': 'a.py'}"),
        ]
        assert loop._detect_loop() is False

    def test_detect_loop_similar_args(self) -> None:
        """3 次相同工具 + 相似参数 → 检测到循环。"""
        loop = _make_loop()
        loop._recent_tool_calls = [
            ("read_file", "{'path': '/src/main.py'}"),
            ("read_file", "{'path': '/src/main.py'}"),
            ("read_file", "{'path': '/src/main.py'}"),
        ]
        assert loop._detect_loop() is True

    def test_detect_loop_similar_args_slight_diff(self) -> None:
        """3 次相同工具 + 高相似度参数（>80%）→ 检测到循环。"""
        loop = _make_loop()
        loop._recent_tool_calls = [
            ("read_file", "{'path': '/src/utils/helper.py'}"),
            ("read_file", "{'path': '/src/utils/helper.py'}"),
            ("read_file", "{'path': '/src/utils/helpers.py'}"),
        ]
        assert loop._detect_loop() is True

    def test_detect_loop_different_args(self) -> None:
        """3 次相同工具 + 不同参数 → 不检测到循环。"""
        loop = _make_loop()
        loop._recent_tool_calls = [
            ("read_file", "{'path': '/src/main.py'}"),
            ("read_file", "{'path': '/docs/README.md'}"),
            ("read_file", "{'path': '/tests/test_foo.py'}"),
        ]
        assert loop._detect_loop() is False

    def test_detect_loop_keeps_last_five(self) -> None:
        """_recent_tool_calls 只保留最近 5 次调用。"""
        loop = _make_loop()
        loop._recent_tool_calls = [
            ("tool_a", "arg1"),
            ("tool_a", "arg2"),
            ("tool_a", "arg3"),
            ("tool_a", "arg4"),
            ("tool_a", "arg5"),
        ]
        # 模拟追加第 6 次调用后的截断逻辑
        loop._recent_tool_calls.append(("tool_a", "arg6"))
        if len(loop._recent_tool_calls) > 5:
            loop._recent_tool_calls = loop._recent_tool_calls[-5:]
        assert len(loop._recent_tool_calls) == 5
        assert loop._recent_tool_calls[0] == ("tool_a", "arg2")

    def test_detect_loop_mixed_recent_three(self) -> None:
        """最近 3 次调用中有不同工具 → 不检测到循环（即使更早的调用相同）。"""
        loop = _make_loop()
        loop._recent_tool_calls = [
            ("read_file", "{'path': 'a.py'}"),
            ("read_file", "{'path': 'a.py'}"),
            ("read_file", "{'path': 'a.py'}"),
            ("write_file", "{'path': 'b.py'}"),
            ("read_file", "{'path': 'a.py'}"),
        ]
        # 最近 3 次: write_file, read_file, read_file → 不同工具
        assert loop._detect_loop() is False

    def test_detect_loop_last_three_same(self) -> None:
        """最近 3 次调用相同工具 + 相似参数 → 检测到循环（即使更早调用不同）。"""
        loop = _make_loop()
        loop._recent_tool_calls = [
            ("write_file", "{'path': 'b.py'}"),
            ("read_file", "{'path': '/src/main.py'}"),
            ("read_file", "{'path': '/src/main.py'}"),
            ("read_file", "{'path': '/src/main.py'}"),
        ]
        # 最近 3 次: read_file × 3，参数相同
        assert loop._detect_loop() is True


class TestCancelAndHelpers:
    """AgentLoop 辅助方法测试。"""

    def test_cancel_sets_flag(self) -> None:
        """cancel 设置 _cancelled 标志为 True。"""
        loop = _make_loop()
        assert loop._cancelled is False
        loop.cancel()
        assert loop._cancelled is True

    def test_record_session_message_no_session(self) -> None:
        """_record_session_message 无 session_manager 时不抛异常。"""
        loop = _make_loop()
        loop.session_manager = None
        loop._record_session_message("user", "hello")  # 不抛异常

    def test_record_session_message_with_session(self) -> None:
        """_record_session_message 有 session_manager 时调用 add_message。"""
        loop = _make_loop()
        mock_session = MagicMock()
        loop.session_manager = mock_session
        loop._record_session_message("user", "hello")
        mock_session.add_message.assert_called_once_with("user", "hello")

    def test_record_session_message_failure_silent(self) -> None:
        """_record_session_message 失败时静默处理。"""
        loop = _make_loop()
        mock_session = MagicMock()
        mock_session.add_message.side_effect = RuntimeError("boom")
        loop.session_manager = mock_session
        loop._record_session_message("user", "hello")  # 不抛异常

    def test_record_tool_call_no_session(self) -> None:
        """_record_tool_call 无 session_manager 时不抛异常。"""
        loop = _make_loop()
        loop.session_manager = None
        loop._record_tool_call("read_file", {"path": "a.py"}, "result", 100)

    def test_record_tool_call_with_session(self) -> None:
        """_record_tool_call 有 session_manager 时调用 record_tool_call。"""
        loop = _make_loop()
        mock_session = MagicMock()
        loop.session_manager = mock_session
        loop._record_tool_call("read_file", {"path": "a.py"}, "result", 100)
        mock_session.record_tool_call.assert_called_once()

    def test_save_session_no_session(self) -> None:
        """_save_session 无 session_manager 时不抛异常。"""
        loop = _make_loop()
        loop.session_manager = None
        loop._save_session()  # 不抛异常

    def test_save_session_failure_silent(self) -> None:
        """_save_session 失败时静默处理。"""
        loop = _make_loop()
        mock_session = MagicMock()
        mock_session.save.side_effect = RuntimeError("boom")
        loop.session_manager = mock_session
        loop._save_session()  # 不抛异常

    def test_build_effective_system_prompt_no_mapper(self) -> None:
        """_build_effective_system_prompt 无 repo_mapper 时返回原始提示。"""
        loop = _make_loop()
        loop.repo_mapper = None
        result = loop._build_effective_system_prompt("hello")
        assert result == loop.system_prompt

    def test_build_effective_system_prompt_with_mapper(self) -> None:
        """_build_effective_system_prompt 有 repo_mapper 时追加摘要。"""
        loop = _make_loop()
        mock_mapper = MagicMock()
        mock_mapper.build_for_query.return_value = "repo summary"
        loop.repo_mapper = mock_mapper
        result = loop._build_effective_system_prompt("hello")
        assert "repo summary" in result
        assert loop.system_prompt in result

    def test_build_effective_system_prompt_mapper_empty(self) -> None:
        """_build_effective_system_prompt mapper 返回空时返回原始提示。"""
        loop = _make_loop()
        mock_mapper = MagicMock()
        mock_mapper.build_for_query.return_value = ""
        loop.repo_mapper = mock_mapper
        result = loop._build_effective_system_prompt("hello")
        assert result == loop.system_prompt

    def test_build_effective_system_prompt_mapper_failure(self) -> None:
        """_build_effective_system_prompt mapper 异常时返回原始提示。"""
        loop = _make_loop()
        mock_mapper = MagicMock()
        mock_mapper.build_for_query.side_effect = RuntimeError("boom")
        loop.repo_mapper = mock_mapper
        result = loop._build_effective_system_prompt("hello")
        assert result == loop.system_prompt

    def test_get_tools_format_openai(self) -> None:
        """_get_tools_format 默认返回 OpenAI 格式。"""
        loop = _make_loop()
        result = loop._get_tools_format()
        assert isinstance(result, list)

    def test_get_tool_result_role_openai(self) -> None:
        """_get_tool_result_role 默认返回 'tool'。"""
        loop = _make_loop()
        assert loop._get_tool_result_role() == "tool"

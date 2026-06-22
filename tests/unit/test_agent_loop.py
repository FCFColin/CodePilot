"""AgentLoop 循环检测机制与辅助方法单元测试。

覆盖 LoopDetector 的各种场景：
- 无调用时不检测
- 不同工具不检测
- 相同工具 + 相同参数 → 检测到循环
- 相同工具 + 不同参数 → 不检测

覆盖辅助方法：
- cancel 设置 _cancelled 标志
- _record_session_message / _record_tool_call / _save_session 静默失败
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from codepilot.agent.loop import AgentLoop, LoopDetector
from codepilot.context.manager import ContextManager
from codepilot.providers.base import BaseProvider
from codepilot.tools.registry import ToolRegistry

# ============================================================================
# 辅助：构建最小化 AgentLoop 实例
# ============================================================================


def _make_loop() -> AgentLoop:
    """构建一个最小化的 AgentLoop 实例，仅用于测试 LoopDetector。"""
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
    """LoopDetector 循环检测测试。"""

    def test_detect_loop_no_calls(self) -> None:
        """无调用时不检测到循环。"""
        detector = LoopDetector()
        # 无调用，直接检查无循环
        assert len(detector._call_hashes) == 0

    def test_detect_loop_one_call(self) -> None:
        """仅 1 次调用时不检测到循环。"""
        detector = LoopDetector()
        assert detector.record_call("read_file", {"path": "a.py"}) is False

    def test_detect_loop_two_calls(self) -> None:
        """仅 2 次调用时不检测到循环（需要 3 次）。"""
        detector = LoopDetector()
        args = {"path": "a.py"}
        assert detector.record_call("read_file", args) is False
        assert detector.record_call("read_file", args) is False

    def test_detect_loop_different_tools(self) -> None:
        """不同工具调用不检测到循环。"""
        detector = LoopDetector()
        args = {"path": "a.py"}
        assert detector.record_call("read_file", args) is False
        assert detector.record_call("write_file", args) is False
        assert detector.record_call("read_file", args) is False

    def test_detect_loop_similar_args(self) -> None:
        """3 次相同工具 + 相同参数 → 检测到循环。"""
        detector = LoopDetector()
        args = {"path": "/src/main.py"}
        assert detector.record_call("read_file", args) is False
        assert detector.record_call("read_file", args) is False
        assert detector.record_call("read_file", args) is True

    def test_detect_loop_exact_same_args(self) -> None:
        """3 次相同工具 + 完全相同参数 → 检测到循环。"""
        detector = LoopDetector()
        args = {"path": "/src/utils/helper.py"}
        assert detector.record_call("read_file", args) is False
        assert detector.record_call("read_file", args) is False
        assert detector.record_call("read_file", args) is True

    def test_detect_loop_different_args(self) -> None:
        """3 次相同工具 + 不同参数 → 不检测到循环。"""
        detector = LoopDetector()
        assert detector.record_call("read_file", {"path": "/src/main.py"}) is False
        assert detector.record_call("read_file", {"path": "/docs/README.md"}) is False
        assert detector.record_call("read_file", {"path": "/tests/test_foo.py"}) is False

    def test_detect_loop_deque_maxlen(self) -> None:
        """LoopDetector 内部 deque 有 maxlen 限制。"""
        detector = LoopDetector(window_size=5, max_repeats=3)
        assert detector._call_hashes.maxlen == 5 * 3

    def test_detect_loop_mixed_recent_calls(self) -> None:
        """混合调用中，窗口内累计 3 次相同调用 → 检测到循环。"""
        detector = LoopDetector()
        args = {"path": "a.py"}
        # 2 次 read_file
        detector.record_call("read_file", args)
        detector.record_call("read_file", args)
        # 1 次 write_file 打断（不同哈希）
        detector.record_call("write_file", {"path": "b.py"})
        # 第 3 次 read_file（窗口内已有 2 次 read_file + 1 write_file，再加 1 = 3 次相同哈希）
        assert detector.record_call("read_file", args) is True

    def test_detect_loop_last_three_same(self) -> None:
        """最近 3 次调用相同工具 + 相同参数 → 检测到循环（即使更早调用不同）。"""
        detector = LoopDetector()
        args = {"path": "/src/main.py"}
        # 1 次 write_file
        detector.record_call("write_file", {"path": "b.py"})
        # 3 次 read_file
        assert detector.record_call("read_file", args) is False
        assert detector.record_call("read_file", args) is False
        assert detector.record_call("read_file", args) is True


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

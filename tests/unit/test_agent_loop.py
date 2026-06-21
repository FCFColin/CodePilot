"""AgentLoop 循环检测机制单元测试。

覆盖 _detect_loop 方法的各种场景：
- 无调用时不检测
- 不同工具不检测
- 相同工具 + 相似参数 → 检测到循环
- 相同工具 + 不同参数 → 不检测
"""

from __future__ import annotations

from unittest.mock import AsyncMock

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

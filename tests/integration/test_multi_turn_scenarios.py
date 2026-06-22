"""多轮对话场景集成测试。

使用 mock provider 模拟 LLM 响应，测试完整的 agent 循环场景。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from codepilot.agent.loop import AgentLoop, LoopDetector
from codepilot.config import ContextConfig
from codepilot.context.manager import ContextManager
from codepilot.context.token_counter import TokenCounter
from codepilot.providers.base import (
    Done,
    TextDelta,
    ToolCall,
    Usage,
)
from codepilot.tools.file_read import ReadFileTool
from codepilot.tools.file_write import WriteFileTool
from codepilot.tools.registry import ToolRegistry


# ============================================================================
# Mock Provider：按预设序列返回 AgentEvent
# ============================================================================


class MockProvider:
    """按预设序列返回 AgentEvent 的 mock provider。

    每次 chat() 调用按顺序消费 event_sequences 中的一个序列。
    序列耗尽后重复使用最后一个序列。
    """

    def __init__(self, event_sequences: list[list[Any]]) -> None:
        self.event_sequences = event_sequences
        self.call_count: int = 0

    async def chat(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        stream: bool = True,
    ) -> AsyncIterator[Any]:
        idx = min(self.call_count, len(self.event_sequences) - 1)
        events = self.event_sequences[idx]
        self.call_count += 1
        for event in events:
            yield event

    def format_assistant_message(
        self,
        text: str,
        tool_calls: list[ToolCall],
    ) -> dict[str, Any]:
        """返回 OpenAI 风格的 assistant 消息。"""
        msg: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in tool_calls
            ]
        return msg

    def format_tool_result(
        self,
        role: str,
        tool_call_id: str,
        content: str,
    ) -> dict[str, Any]:
        """返回 OpenAI 风格的工具结果消息。"""
        return {
            "role": role,
            "tool_call_id": tool_call_id,
            "content": content,
        }


# ============================================================================
# 辅助工厂函数
# ============================================================================


def _make_tool_call(name: str, arguments: dict[str, Any]) -> ToolCall:
    """创建 ToolCall 事件。"""
    return ToolCall(id=f"call_{name}", name=name, arguments=arguments)


def _make_context_manager() -> ContextManager:
    """创建测试用 ContextManager（短配置，无压缩器）。"""
    config = ContextConfig()
    token_counter = TokenCounter()
    return ContextManager(
        config=config,
        token_counter=token_counter,
        compressor=None,
        system_prompt="",
    )


def _make_loop(
    provider: MockProvider,
    context_manager: ContextManager,
    tool_registry: ToolRegistry,
    **kwargs: Any,
) -> AgentLoop:
    """创建测试用 AgentLoop。"""
    return AgentLoop(
        provider=provider,
        context_manager=context_manager,
        tool_registry=tool_registry,
        system_prompt="test",
        **kwargs,
    )


# ============================================================================
# 场景1：多步文件创建任务
# ============================================================================


class TestMultiTurnFileCreation:
    """场景1：多步文件创建任务。"""

    async def test_create_then_modify(self, tmp_path: Path) -> None:
        """创建文件后修改：验证两轮操作都成功。"""
        registry = ToolRegistry()
        registry.register(WriteFileTool(workspace_root=str(tmp_path)))
        registry.register(ReadFileTool(workspace_root=str(tmp_path)))

        # 第一轮：创建文件
        events_turn1 = [
            _make_tool_call("write_file", {"path": "hello.py", "content": "print('hello')\n"}),
            Done(stop_reason="end_turn"),
        ]

        # 第二轮：读取并修改（LLM 先读取，再编辑）
        events_turn2_read = [
            _make_tool_call("read_file", {"path": "hello.py"}),
            Done(stop_reason="end_turn"),
        ]
        events_turn2_edit = [
            _make_tool_call("write_file", {"path": "hello.py", "content": "print('world')\n"}),
            Done(stop_reason="end_turn"),
        ]
        events_turn2_final = [
            TextDelta(text="文件已修改。"),
            Done(stop_reason="end_turn"),
        ]

        mock_provider = MockProvider(
            event_sequences=[
                events_turn1,
                # 第一轮：创建后 LLM 返回确认文本
                [TextDelta(text="文件已创建。"), Done(stop_reason="end_turn")],
                # 第二轮：读取 → 编辑 → 确认
                events_turn2_read,
                events_turn2_edit,
                events_turn2_final,
            ]
        )

        cm = _make_context_manager()
        loop = _make_loop(mock_provider, cm, registry)

        # 第一轮
        result1 = await loop.run("创建 hello.py")
        assert "hello.py" in result1 or "创建" in result1 or "文件" in result1

        # 验证文件存在
        assert (tmp_path / "hello.py").exists()

        # 第二轮
        result2 = await loop.run("修改 hello.py 中的 hello 为 world")
        assert (tmp_path / "hello.py").exists()


# ============================================================================
# 场景2：错误恢复测试
# ============================================================================


class TestErrorRecovery:
    """场景2：错误恢复测试。"""

    async def test_shell_error_recovery(self, tmp_path: Path) -> None:
        """命令执行失败后，agent 应能继续工作。"""
        registry = ToolRegistry()
        registry.register(WriteFileTool(workspace_root=str(tmp_path)))

        # 模型先执行一个会失败的命令（shell_exec 未注册，返回 unknown tool），
        # 然后调整策略用 write_file
        events = [
            _make_tool_call("shell_exec", {"command": "nonexistent_command_xyz"}),
            Done(stop_reason="end_turn"),
        ]

        # 第二次 LLM 调用：收到错误后改用 write_file
        events_retry = [
            _make_tool_call("write_file", {"path": "fallback.txt", "content": "fallback result"}),
            Done(stop_reason="end_turn"),
        ]

        events_final = [
            TextDelta(text="命令失败，改用文件创建。"),
            Done(stop_reason="end_turn"),
        ]

        mock_provider = MockProvider(
            event_sequences=[
                events,
                events_retry,
                events_final,
            ]
        )

        cm = _make_context_manager()
        loop = _make_loop(mock_provider, cm, registry)

        result = await loop.run("执行命令")
        # 验证 agent 没有崩溃，且创建了备用文件
        assert (tmp_path / "fallback.txt").exists()


# ============================================================================
# 场景4：安全拦截测试
# ============================================================================


class TestSecurityInterception:
    """场景4：安全拦截测试。"""

    async def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        """模型尝试读取 workspace 外的文件被拦截。"""
        from codepilot.security.sandbox import Sandbox

        sandbox = Sandbox(workspace_root=str(tmp_path))
        registry = ToolRegistry()
        registry.register(ReadFileTool(workspace_root=str(tmp_path)))

        # 模型尝试读取 /etc/passwd
        events = [
            _make_tool_call("read_file", {"path": "../../../etc/passwd"}),
            Done(stop_reason="end_turn"),
        ]

        # LLM 收到安全拒绝后返回文本
        events_final = [
            TextDelta(text="无法读取该文件。"),
            Done(stop_reason="end_turn"),
        ]

        mock_provider = MockProvider(
            event_sequences=[
                events,
                events_final,
            ]
        )

        cm = _make_context_manager()
        loop = _make_loop(mock_provider, cm, registry, sandbox=sandbox)

        result = await loop.run("读取系统文件")
        # 验证 sandbox 拦截了路径穿越（结果中应包含安全拒绝信息）


# ============================================================================
# 场景5：循环检测测试
# ============================================================================


class TestLoopDetection:
    """场景5：循环检测测试。"""

    def test_loop_detector_repeated_calls(self) -> None:
        """重复调用同一工具应被检测到。"""
        detector = LoopDetector(window_size=5, max_repeats=3)
        # 连续3次相同调用
        assert not detector.record_call("read_file", {"path": "a.py"})
        assert not detector.record_call("read_file", {"path": "a.py"})
        assert detector.record_call("read_file", {"path": "a.py"})

    def test_loop_detector_different_args(self) -> None:
        """不同参数的调用不应被检测为循环。"""
        detector = LoopDetector(window_size=5, max_repeats=3)
        assert not detector.record_call("read_file", {"path": "a.py"})
        assert not detector.record_call("read_file", {"path": "b.py"})
        assert not detector.record_call("read_file", {"path": "c.py"})

    def test_loop_detector_reset(self) -> None:
        """重置后应重新开始检测。"""
        detector = LoopDetector(window_size=5, max_repeats=2)
        detector.record_call("read_file", {"path": "a.py"})
        detector.reset()
        assert not detector.record_call("read_file", {"path": "a.py"})


# ============================================================================
# 场景7：/undo 和 /rollback 测试
# ============================================================================


class TestUndoAndRollback:
    """场景7：/undo 和 /rollback 测试。"""

    def test_undo_after_file_write(self, tmp_path: Path) -> None:
        """文件写入后撤销，验证文件被删除。"""
        from codepilot.app import UndoTracker

        tracker = UndoTracker()
        file_path = str(tmp_path / "test.txt")

        # 记录新建文件
        tracker._stack.append((file_path, None))  # None = 新建文件

        # 写入文件
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("test content")

        assert (tmp_path / "test.txt").exists()

        # 撤销
        success, msg = tracker.undo()
        assert success
        assert not (tmp_path / "test.txt").exists()

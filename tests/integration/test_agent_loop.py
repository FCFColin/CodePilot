"""Agent 循环集成测试。

使用 mock provider 和真实工具注册表，验证 AgentLoop 的完整循环逻辑：
- 完整 tool-use 循环（用户输入→工具调用→结果回传→最终响应）
- max_tool_calls_per_turn 上限
- cancel() 中断
- 未知工具名/sandbox 拒绝/approval 拒绝的优雅处理
- ProviderError 异常处理
- 多轮上下文累积
- 思考过程/用量事件传递
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from codepilot.agent.loop import AgentLoop, UICallback
from codepilot.config import ContextConfig
from codepilot.context.manager import ContextManager
from codepilot.context.token_counter import TokenCounter
from codepilot.exceptions import ProviderError
from codepilot.providers.base import (
    Done,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    Usage,
)
from codepilot.tools.file_read import ReadFileTool
from codepilot.tools.file_write import WriteFileTool
from codepilot.tools.registry import ToolRegistry

# ============================================================================
# Mock UI 回调：收集事件用于断言
# ============================================================================


class MockUICallback:
    """收集 UI 事件用于断言。"""

    def __init__(self) -> None:
        self.text_deltas: list[str] = []
        self.thinking_deltas: list[str] = []
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.tool_results: list[tuple[str, str, bool]] = []
        self.errors: list[str] = []
        self.usage_events: list[tuple[int, int]] = []
        self.turn_ends: int = 0

    async def on_text_delta(self, text: str) -> None:
        self.text_deltas.append(text)

    async def on_thinking_delta(self, text: str) -> None:
        self.thinking_deltas.append(text)

    async def on_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self.tool_calls.append((name, arguments))

    async def on_tool_result(self, name: str, result: str, success: bool) -> None:
        self.tool_results.append((name, result, success))

    async def on_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.usage_events.append((input_tokens, output_tokens))

    async def on_error(self, error: str) -> None:
        self.errors.append(error)

    async def on_turn_end(self) -> None:
        self.turn_ends += 1


class CancellingCallback:
    """首次 on_tool_call 时触发 cancel 的 UI 回调。"""

    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.tool_results: list[tuple[str, str, bool]] = []
        self.turn_ends: int = 0

    async def on_text_delta(self, text: str) -> None:
        pass

    async def on_thinking_delta(self, text: str) -> None:
        pass

    async def on_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self.tool_calls.append((name, arguments))
        self.loop.cancel()

    async def on_tool_result(self, name: str, result: str, success: bool) -> None:
        self.tool_results.append((name, result, success))

    async def on_usage(self, input_tokens: int, output_tokens: int) -> None:
        pass

    async def on_error(self, error: str) -> None:
        pass

    async def on_turn_end(self) -> None:
        self.turn_ends += 1


# ============================================================================
# Mock Provider：按预设序列返回 AgentEvent
# ============================================================================


class MockProvider:
    """按预设序列返回 AgentEvent 的 mock provider。

    每次 chat() 调用按顺序消费 event_sequences 中的一个序列。
    序列耗尽后重复使用最后一个序列。可选在指定调用次数时抛出 ProviderError。
    """

    def __init__(
        self,
        event_sequences: list[list[Any]],
        raise_on_call: int | None = None,
    ) -> None:
        self.event_sequences = event_sequences
        self.raise_on_call = raise_on_call
        self.call_count: int = 0

    async def chat(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        stream: bool = True,
    ) -> AsyncIterator[Any]:
        if self.raise_on_call is not None and self.call_count == self.raise_on_call:
            self.call_count += 1
            raise ProviderError("mock provider error")
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
# Mock 安全组件
# ============================================================================


class MockSandbox:
    """模拟沙箱，按路径黑名单拒绝。"""

    def __init__(self, reject_paths: list[str] | None = None) -> None:
        self.reject_paths = reject_paths or []

    def validate_path(self, path: str, operation: str = "read") -> tuple[bool, str]:
        for pattern in self.reject_paths:
            if pattern in path:
                return False, f"path matches blocked pattern: {pattern}"
        return True, ""

    def validate_command(self, command: str) -> tuple[bool, str]:
        return True, ""


class MockApproval:
    """模拟审批器，按预设返回审批结果。"""

    def __init__(self, approved: bool = True) -> None:
        self.approved = approved

    async def request_approval(
        self,
        operation: str,
        details: dict[str, Any],
    ) -> bool:
        return self.approved


# ============================================================================
# 辅助工厂函数
# ============================================================================


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
    ui_callback: UICallback | None = None,
    sandbox: Any = None,
    approval: Any = None,
    max_tool_calls_per_turn: int = 25,
) -> AgentLoop:
    """创建测试用 AgentLoop。"""
    return AgentLoop(
        provider=provider,
        context_manager=context_manager,
        tool_registry=tool_registry,
        sandbox=sandbox,
        approval=approval,
        ui_callback=ui_callback,
        max_tool_calls_per_turn=max_tool_calls_per_turn,
        system_prompt="test",
    )


# ============================================================================
# 测试用例
# ============================================================================


class TestAgentLoop:
    """AgentLoop 集成测试。"""

    async def test_complete_tool_use_cycle(self, tmp_path: Path) -> None:
        """完整循环：用户输入→LLM 返回 tool_call→工具执行→结果回传→LLM 最终响应。"""
        # 准备测试文件
        (tmp_path / "test.txt").write_text("hello world", encoding="utf-8")

        # MockProvider 序列：第一次返回工具调用，第二次返回最终文本
        provider = MockProvider(
            event_sequences=[
                [
                    ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "test.txt"},
                    ),
                    Done(stop_reason="end_turn"),
                ],
                [
                    TextDelta(text="The file contains hello world"),
                    Done(stop_reason="end_turn"),
                ],
            ]
        )

        registry = ToolRegistry()
        registry.register(ReadFileTool(workspace_root=str(tmp_path)))

        cm = _make_context_manager()
        ui = MockUICallback()
        loop = _make_loop(provider, cm, registry, ui_callback=ui)

        result = await loop.run("Read the file")

        # 验证工具调用
        assert len(ui.tool_calls) == 1
        assert ui.tool_calls[0] == ("read_file", {"path": "test.txt"})

        # 验证工具结果（成功）
        assert len(ui.tool_results) == 1
        name, res, success = ui.tool_results[0]
        assert name == "read_file"
        assert success is True
        assert "hello world" in res

        # 验证最终文本
        assert "The file contains hello world" in result
        assert "The file contains hello world" in "".join(ui.text_deltas)

        # 验证 turn_end 被调用
        assert ui.turn_ends == 1

    async def test_max_tool_calls_limit(self, tmp_path: Path) -> None:
        """max_tool_calls_per_turn 上限触发。"""
        (tmp_path / "test.txt").write_text("content", encoding="utf-8")

        # MockProvider 始终返回工具调用
        provider = MockProvider(
            event_sequences=[
                [
                    ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "test.txt"},
                    ),
                    Done(stop_reason="end_turn"),
                ],
            ]
        )

        registry = ToolRegistry()
        registry.register(ReadFileTool(workspace_root=str(tmp_path)))

        cm = _make_context_manager()
        ui = MockUICallback()
        loop = _make_loop(
            provider,
            cm,
            registry,
            ui_callback=ui,
            max_tool_calls_per_turn=2,
        )

        result = await loop.run("keep reading")

        # 前两次工具调用正常执行
        assert len(ui.tool_calls) == 2
        assert len(ui.tool_results) == 2

        # 第三次触发上限，错误通知
        assert len(ui.errors) == 1
        assert "上限" in ui.errors[0]
        assert "上限" in result

    async def test_cancel_interrupt(self, tmp_path: Path) -> None:
        """cancel() 中断：首次工具调用时触发中断。"""
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / "b.txt").write_text("b", encoding="utf-8")

        # MockProvider 返回两个工具调用
        provider = MockProvider(
            event_sequences=[
                [
                    ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ToolCall(
                        id="call_2",
                        name="read_file",
                        arguments={"path": "b.txt"},
                    ),
                    Done(stop_reason="end_turn"),
                ],
            ]
        )

        registry = ToolRegistry()
        registry.register(ReadFileTool(workspace_root=str(tmp_path)))

        cm = _make_context_manager()
        loop = _make_loop(provider, cm, registry, ui_callback=None)

        # 注入取消回调
        cancelling_cb = CancellingCallback(loop)
        loop.ui_callback = cancelling_cb

        await loop.run("read both files")

        # 第一个工具调用触发 cancel，第二个被跳过
        assert len(cancelling_cb.tool_calls) == 1
        assert cancelling_cb.tool_calls[0][0] == "read_file"
        # 只调用了 provider.chat 一次（中断后未继续）
        assert provider.call_count == 1
        # turn_end 仍被调用
        assert cancelling_cb.turn_ends == 1

    async def test_unknown_tool_name(self, tmp_path: Path) -> None:
        """未知工具名优雅处理。"""
        provider = MockProvider(
            event_sequences=[
                [
                    ToolCall(
                        id="call_1",
                        name="nonexistent_tool",
                        arguments={},
                    ),
                    Done(stop_reason="end_turn"),
                ],
                [
                    TextDelta(text="ok"),
                    Done(stop_reason="end_turn"),
                ],
            ]
        )

        # 空注册表（无工具）
        registry = ToolRegistry()
        cm = _make_context_manager()
        ui = MockUICallback()
        loop = _make_loop(provider, cm, registry, ui_callback=ui)

        result = await loop.run("call unknown tool")

        # 工具结果标记为失败
        assert len(ui.tool_results) == 1
        name, res, success = ui.tool_results[0]
        assert name == "nonexistent_tool"
        assert success is False
        assert "unknown tool" in res

        # LLM 收到错误后继续正常响应
        assert "ok" in result

    async def test_sandbox_rejection(self, tmp_path: Path) -> None:
        """sandbox 拒绝时错误消息传递。"""
        (tmp_path / "test.txt").write_text("data", encoding="utf-8")

        provider = MockProvider(
            event_sequences=[
                [
                    ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "test.txt"},
                    ),
                    Done(stop_reason="end_turn"),
                ],
                [
                    TextDelta(text="understood"),
                    Done(stop_reason="end_turn"),
                ],
            ]
        )

        registry = ToolRegistry()
        registry.register(ReadFileTool(workspace_root=str(tmp_path)))

        # sandbox 拒绝 test.txt 路径
        sandbox = MockSandbox(reject_paths=["test.txt"])

        cm = _make_context_manager()
        ui = MockUICallback()
        loop = _make_loop(provider, cm, registry, ui_callback=ui, sandbox=sandbox)

        await loop.run("read file")

        assert len(ui.tool_results) == 1
        _, res, success = ui.tool_results[0]
        assert success is False
        assert "validation failed" in res

    async def test_approval_rejection(self, tmp_path: Path) -> None:
        """approval 拒绝时的行为。"""
        provider = MockProvider(
            event_sequences=[
                [
                    ToolCall(
                        id="call_1",
                        name="write_file",
                        arguments={
                            "path": "out.txt",
                            "content": "data",
                        },
                    ),
                    Done(stop_reason="end_turn"),
                ],
                [
                    TextDelta(text="ok"),
                    Done(stop_reason="end_turn"),
                ],
            ]
        )

        registry = ToolRegistry()
        registry.register(WriteFileTool(workspace_root=str(tmp_path)))

        # approval 拒绝所有操作
        approval = MockApproval(approved=False)

        cm = _make_context_manager()
        ui = MockUICallback()
        loop = _make_loop(provider, cm, registry, ui_callback=ui, approval=approval)

        await loop.run("write file")

        assert len(ui.tool_results) == 1
        _, res, success = ui.tool_results[0]
        assert success is False
        assert "not approved" in res

        # 文件未被创建
        assert not (tmp_path / "out.txt").exists()

    async def test_multi_turn_context_accumulation(self, tmp_path: Path) -> None:
        """多轮对话上下文累积。"""
        (tmp_path / "test.txt").write_text("content", encoding="utf-8")

        provider = MockProvider(
            event_sequences=[
                # 第一轮第一次调用：工具调用
                [
                    ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "test.txt"},
                    ),
                    Done(stop_reason="end_turn"),
                ],
                # 第一轮第二次调用：最终文本
                [
                    TextDelta(text="done1"),
                    Done(stop_reason="end_turn"),
                ],
                # 第二轮调用：最终文本
                [
                    TextDelta(text="done2"),
                    Done(stop_reason="end_turn"),
                ],
            ]
        )

        registry = ToolRegistry()
        registry.register(ReadFileTool(workspace_root=str(tmp_path)))

        cm = _make_context_manager()
        ui = MockUICallback()
        loop = _make_loop(provider, cm, registry, ui_callback=ui)

        # 第一轮：user → assistant(tool_call) → tool_result → assistant(text)
        result1 = await loop.run("first")
        assert "done1" in result1
        # 4 条消息：user, assistant(含tool_calls), tool_result, assistant(文本)
        assert len(cm.messages) == 4

        # 第二轮：user → assistant(text)
        result2 = await loop.run("second")
        assert "done2" in result2
        # 新增 2 条消息：user, assistant(文本)
        assert len(cm.messages) == 6

        # turn_end 被调用两次
        assert ui.turn_ends == 2

    async def test_provider_error_handling(self) -> None:
        """ProviderError 异常优雅处理。"""
        provider = MockProvider(
            event_sequences=[],
            raise_on_call=0,
        )

        registry = ToolRegistry()
        cm = _make_context_manager()
        ui = MockUICallback()
        loop = _make_loop(provider, cm, registry, ui_callback=ui)

        result = await loop.run("trigger error")

        # 错误通知
        assert len(ui.errors) == 1
        assert "LLM 调用失败" in ui.errors[0]
        assert "LLM 调用失败" in result

        # turn_end 仍被调用
        assert ui.turn_ends == 1

    async def test_no_tool_calls_direct_response(self) -> None:
        """无工具调用时直接返回文本响应。"""
        provider = MockProvider(
            event_sequences=[
                [
                    TextDelta(text="Hello!"),
                    Done(stop_reason="end_turn"),
                ],
            ]
        )

        registry = ToolRegistry()
        cm = _make_context_manager()
        ui = MockUICallback()
        loop = _make_loop(provider, cm, registry, ui_callback=ui)

        result = await loop.run("hi")

        assert ui.text_deltas == ["Hello!"]
        assert ui.tool_calls == []
        assert result == "Hello!"
        assert ui.turn_ends == 1

    async def test_thinking_delta_emitted(self) -> None:
        """思考过程片段传递给 UI 回调。"""
        provider = MockProvider(
            event_sequences=[
                [
                    ThinkingDelta(text="Let me think..."),
                    TextDelta(text="Answer"),
                    Done(stop_reason="end_turn"),
                ],
            ]
        )

        registry = ToolRegistry()
        cm = _make_context_manager()
        ui = MockUICallback()
        loop = _make_loop(provider, cm, registry, ui_callback=ui)

        await loop.run("question")

        assert ui.thinking_deltas == ["Let me think..."]
        assert ui.text_deltas == ["Answer"]

    async def test_usage_event_emitted(self) -> None:
        """Usage 事件传递给 UI 回调和上下文管理器。"""
        provider = MockProvider(
            event_sequences=[
                [
                    Usage(input_tokens=100, output_tokens=50),
                    TextDelta(text="ok"),
                    Done(stop_reason="end_turn"),
                ],
            ]
        )

        registry = ToolRegistry()
        cm = _make_context_manager()
        ui = MockUICallback()
        loop = _make_loop(provider, cm, registry, ui_callback=ui)

        await loop.run("check usage")

        assert len(ui.usage_events) == 1
        assert ui.usage_events[0] == (100, 50)

    async def test_stop_reason_error(self) -> None:
        """stop_reason 以 error 开头时通知 UI。"""
        provider = MockProvider(
            event_sequences=[
                [
                    TextDelta(text="partial"),
                    Done(stop_reason="error: rate limited"),
                ],
            ]
        )

        registry = ToolRegistry()
        cm = _make_context_manager()
        ui = MockUICallback()
        loop = _make_loop(provider, cm, registry, ui_callback=ui)

        result = await loop.run("trigger stop error")

        assert len(ui.errors) == 1
        assert "LLM 调用失败" in ui.errors[0]
        assert "error: rate limited" in result
        assert ui.turn_ends == 1

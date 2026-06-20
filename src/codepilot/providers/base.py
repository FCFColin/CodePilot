"""Provider 抽象接口定义。

定义 AgentEvent 事件类型、Message 消息结构、BaseProvider 抽象基类，
以及 OpenAI / Anthropic 两种后端的消息 TypedDict，统一交互接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, TypedDict

# ============================================================================
# AgentEvent 事件类型定义
# ============================================================================


@dataclass
class TextDelta:
    """文本片段事件。"""

    text: str


@dataclass
class ThinkingDelta:
    """思考过程片段事件。"""

    text: str


@dataclass
class ToolCall:
    """工具调用请求事件。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    """token 用量事件。"""

    input_tokens: int
    output_tokens: int


@dataclass
class Done:
    """结束事件。"""

    stop_reason: str


# AgentEvent 联合类型：所有可能的流式事件
AgentEvent = TextDelta | ThinkingDelta | ToolCall | Usage | Done


# ============================================================================
# 消息与工具结果类型
# ============================================================================


@dataclass
class Message:
    """通用消息结构。

    content 可为 str（纯文本）或 list（Anthropic content blocks /
    OpenAI 多模态内容），也可为 dict（provider-native 完整消息，
    由 format_assistant_message / format_tool_result 产出）。
    """

    role: str
    content: Any


@dataclass
class ToolCallResult:
    """工具调用结果，用于回传给 LLM。"""

    tool_call_id: str
    content: str
    is_error: bool = False


# ============================================================================
# OpenAI 消息 TypedDict
# ============================================================================


class OpenAIToolCallFunction(TypedDict):
    """OpenAI 工具调用中的 function 子结构。"""

    name: str
    arguments: str  # JSON 字符串


class OpenAIToolCall(TypedDict):
    """OpenAI 工具调用结构。"""

    id: str
    type: str  # "function"
    function: OpenAIToolCallFunction


class OpenAIToolMessage(TypedDict):
    """OpenAI tool 角色消息（工具执行结果回传）。"""

    role: str  # "tool"
    tool_call_id: str
    content: str


class OpenAIAssistantMessage(TypedDict, total=False):
    """OpenAI assistant 角色消息。

    total=False 以兼容无 tool_calls 的场景；role 与 content 在实际产出时始终存在。
    """

    role: str  # "assistant"
    content: str
    tool_calls: list[OpenAIToolCall]


# ============================================================================
# Anthropic 消息 TypedDict
# ============================================================================


class AnthropicToolResultBlock(TypedDict):
    """Anthropic tool_result content block。"""

    type: str  # "tool_result"
    tool_use_id: str
    content: str


class AnthropicToolResultMessage(TypedDict):
    """Anthropic 工具结果消息（role 为 user，content 为 tool_result block 列表）。"""

    role: str  # "user"
    content: list[AnthropicToolResultBlock]


class AnthropicTextBlock(TypedDict):
    """Anthropic text content block。"""

    type: str  # "text"
    text: str


class AnthropicToolUseBlock(TypedDict):
    """Anthropic tool_use content block。"""

    type: str  # "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class AnthropicAssistantMessage(TypedDict):
    """Anthropic assistant 角色消息。"""

    role: str  # "assistant"
    content: list[AnthropicTextBlock | AnthropicToolUseBlock]


# ============================================================================
# BaseProvider 抽象基类
# ============================================================================


class BaseProvider(ABC):
    """Provider 抽象基类。

    统一不同 LLM 后端（DeepSeek、Anthropic）的交互接口，
    通过 async chat 方法返回 AgentEvent 流。
    """

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        stream: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        """发送消息并获取响应，统一返回 AgentEvent 流。

        此方法为 async generator，实现中通过 yield 产出 AgentEvent。

        Args:
            messages: 消息历史列表（Message 对象或 provider-native dict）。
            tools: 可选的工具定义列表（provider 原生格式）。
            system_prompt: 系统提示词。
            stream: 是否流式返回。

        Yields:
            AgentEvent 事件流。
        """
        ...

    @abstractmethod
    def format_tool_result(
        self,
        role: str,
        tool_call_id: str,
        content: str,
    ) -> OpenAIToolMessage | AnthropicToolResultMessage:
        """将工具执行结果格式化为 provider 原生消息。

        Args:
            role: 消息角色（OpenAI 为 "tool"，Anthropic 为 "user"）。
            tool_call_id: 对应的工具调用 ID。
            content: 工具执行结果文本。

        Returns:
            provider 原生格式的工具结果消息（TypedDict）。
        """
        ...

    @abstractmethod
    def format_assistant_message(
        self,
        text: str,
        tool_calls: list[ToolCall],
    ) -> OpenAIAssistantMessage | AnthropicAssistantMessage:
        """将 assistant 文本与工具调用格式化为 provider 原生消息。

        Args:
            text: assistant 文本内容（可能为空字符串）。
            tool_calls: ToolCall 列表（可能为空）。

        Returns:
            provider 原生格式的 assistant 消息（TypedDict）。
        """
        ...


__all__ = [
    "TextDelta",
    "ThinkingDelta",
    "ToolCall",
    "Usage",
    "Done",
    "AgentEvent",
    "Message",
    "ToolCallResult",
    "BaseProvider",
    "OpenAIToolCallFunction",
    "OpenAIToolCall",
    "OpenAIToolMessage",
    "OpenAIAssistantMessage",
    "AnthropicToolResultBlock",
    "AnthropicToolResultMessage",
    "AnthropicTextBlock",
    "AnthropicToolUseBlock",
    "AnthropicAssistantMessage",
]

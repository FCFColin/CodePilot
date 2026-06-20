"""Provider 抽象接口定义。

定义 AgentEvent 事件类型、Message 消息结构、BaseProvider 抽象基类，
统一 DeepSeek 与 Anthropic 两种后端的交互接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Union


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
    arguments: dict


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
AgentEvent = Union[TextDelta, ThinkingDelta, ToolCall, Usage, Done]


# ============================================================================
# 消息与工具结果类型
# ============================================================================

@dataclass
class Message:
    """通用消息结构。

    content 可为 str（纯文本）或 list（Anthropic content blocks / OpenAI 多模态内容）。
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
# BaseProvider 抽象基类
# ============================================================================

class BaseProvider(ABC):
    """Provider 抽象基类。

    统一不同 LLM 后端（DeepSeek、Anthropic）的交互接口，
    通过 async chat 方法返回 AgentEvent 流。
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        system_prompt: str = "",
        stream: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        """发送消息并获取响应，统一返回 AgentEvent 流。

        Args:
            messages: 消息历史列表（Message 对象或 provider-native dict）。
            tools: 可选的工具定义列表（provider 原生格式）。
            system_prompt: 系统提示词。
            stream: 是否流式返回。

        Yields:
            AgentEvent 事件流。
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
]

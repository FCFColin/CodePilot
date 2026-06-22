"""Anthropic Provider 实现。

基于 anthropic Python SDK 原生 Messages API。
支持流式响应、工具调用、思考过程。

特性：
- tenacity 重试（API 调用失败自动重试，指数退避）
- structlog 结构化日志（API Key 不入日志）
- 完整类型注解，异常包装为 ProviderError
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

import anthropic
import structlog
from anthropic import AsyncAnthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from codepilot.config import ProviderConfig
from codepilot.exceptions import ProviderError
from codepilot.providers.base import (
    AgentEvent,
    AnthropicAssistantMessage,
    AnthropicTextBlock,
    AnthropicToolResultBlock,
    AnthropicToolResultMessage,
    AnthropicToolUseBlock,
    BaseProvider,
    Done,
    Message,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    Usage,
)

logger = structlog.get_logger(__name__)


def _repair_json(s: str) -> str:
    """尝试修复常见的 JSON 格式错误。"""
    # 补全未闭合的括号
    open_braces = s.count('{') - s.count('}')
    s += '}' * max(0, open_braces)
    open_brackets = s.count('[') - s.count(']')
    s += ']' * max(0, open_brackets)
    # 删除尾随逗号（在补全括号之后，确保能匹配到逗号+闭合括号）
    s = re.sub(r',\s*([}\]])', r'\1', s)
    return s


class AnthropicProvider(BaseProvider):
    """Anthropic Provider，基于 anthropic SDK 原生 Messages API。"""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        # 创建异步 Anthropic 客户端
        # max_retries=0 禁用 SDK 内置重试，由 tenacity 统一管理重试
        self.client = AsyncAnthropic(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            max_retries=0,
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        stream: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        """发送消息并获取响应，返回 AgentEvent 事件流。

        Raises:
            ProviderError: API 调用或流式解析失败时抛出。
        """
        # 构建请求消息列表（Anthropic 的 system 是顶层参数，不在 messages 中）
        request_messages = self._convert_messages(messages)

        # 构建请求参数
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": request_messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": stream,
        }

        # system_prompt 单独传递（Anthropic 顶层参数）
        if system_prompt:
            kwargs["system"] = system_prompt

        # 工具定义（Anthropic 原生格式：
        # {"name": ..., "description": ..., "input_schema": {...}}）
        if tools:
            kwargs["tools"] = tools

        logger.debug(
            "发起 Anthropic 请求",
            model=self.config.model,
            message_count=len(request_messages),
            stream=stream,
            has_tools=bool(tools),
        )

        # 发起请求（带重试），失败包装为 ProviderError
        try:
            response = await self._create_message(**kwargs)
        except anthropic.APIError as e:
            logger.error("Anthropic API 调用失败", error=str(e))
            raise ProviderError(f"Anthropic API 调用失败: {e}") from e

        # 解析响应
        if stream:
            async for event in self._iter_stream(response):
                yield event
        else:
            async for event in self._iter_non_stream(response):
                yield event

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((anthropic.APIError,)),
        reraise=True,
    )
    async def _create_message(self, **kwargs: Any) -> Any:
        """发起 messages 请求（带 tenacity 重试）。

        重试策略：最多 3 次，指数退避（1~10 秒），仅对 anthropic.APIError 重试。
        """
        return await self.client.messages.create(**kwargs)

    async def _iter_stream(self, response: Any) -> AsyncIterator[AgentEvent]:
        """解析流式响应事件为 AgentEvent。

        事件类型：
        - message_start → Usage（input_tokens）
        - content_block_start + text → 开始文本块
        - content_block_start + tool_use → 开始工具调用（记录 id, name）
        - content_block_delta + text_delta → TextDelta
        - content_block_delta + thinking_delta → ThinkingDelta
        - content_block_delta + input_json_delta → 累积工具参数 JSON
        - content_block_stop → 若是 tool_use 块，发出 ToolCall
        - message_delta → Usage（output_tokens）+ stop_reason
        - message_stop → Done
        """
        # 累积工具调用的中间状态：
        # {block_index: {"id": ..., "name": ..., "arguments": ""}}
        pending_tool_calls: dict[int, dict[str, str]] = {}
        # 当前 content block 的类型和 index
        current_block_type: str | None = None
        current_block_index: int | None = None
        # stop_reason 在 message_delta 中传递，message_stop 时发出
        stop_reason = ""

        try:
            async for event in response:
                if event.type == "message_start":
                    # 初始 usage（input_tokens）
                    msg = event.message
                    if msg.usage:
                        yield Usage(
                            input_tokens=msg.usage.input_tokens,
                            output_tokens=msg.usage.output_tokens,
                        )

                elif event.type == "content_block_start":
                    block = event.content_block
                    current_block_type = block.type
                    current_block_index = event.index
                    # 工具调用开始：记录 id 和 name
                    if block.type == "tool_use":
                        pending_tool_calls[event.index] = {
                            "id": block.id,
                            "name": block.name,
                            "arguments": "",
                        }

                elif event.type == "content_block_delta":
                    delta = event.delta
                    # 文本片段
                    if delta.type == "text_delta":
                        yield TextDelta(text=delta.text)
                    # 思考过程片段
                    elif delta.type == "thinking_delta":
                        yield ThinkingDelta(text=delta.thinking)
                    # 工具调用参数 JSON 片段（累积）
                    elif (
                        delta.type == "input_json_delta"
                        and current_block_index is not None
                        and current_block_index in pending_tool_calls
                    ):
                        pending_tool_calls[current_block_index]["arguments"] += (
                            delta.partial_json
                        )

                elif event.type == "content_block_stop":
                    # 工具调用块结束：解析累积的 arguments 并发出 ToolCall
                    if (
                        current_block_type == "tool_use"
                        and current_block_index is not None
                    ):
                        tc_data = pending_tool_calls.get(current_block_index)
                        if tc_data:
                            arguments_str = tc_data["arguments"]
                            if arguments_str:
                                try:
                                    arguments = json.loads(arguments_str)
                                except json.JSONDecodeError:
                                    try:
                                        arguments = json.loads(
                                            _repair_json(arguments_str)
                                        )
                                        logger.warning(
                                            "工具调用参数 JSON 修复成功",
                                            raw=arguments_str[:100],
                                        )
                                    except json.JSONDecodeError:
                                        logger.error(
                                            "工具调用参数 JSON 无法修复",
                                            raw=arguments_str[:200],
                                        )
                                        arguments = {
                                            "_error": "invalid JSON",
                                            "_raw": arguments_str[:500],
                                        }
                            else:
                                arguments = {}
                            yield ToolCall(
                                id=tc_data["id"],
                                name=tc_data["name"],
                                arguments=arguments,
                            )
                    current_block_type = None
                    current_block_index = None

                elif event.type == "message_delta":
                    # stop_reason 和 output_tokens
                    delta = event.delta
                    if delta and delta.stop_reason:
                        stop_reason = delta.stop_reason
                    if event.usage:
                        yield Usage(
                            input_tokens=0,
                            output_tokens=event.usage.output_tokens,
                        )

                elif event.type == "message_stop":
                    yield Done(stop_reason=stop_reason or "stop")

        except anthropic.APIError as e:
            logger.error("Anthropic 流式解析失败", error=str(e))
            raise ProviderError(f"Anthropic 流式解析失败: {e}") from e

    async def _iter_non_stream(self, response: Any) -> AsyncIterator[AgentEvent]:
        """解析非流式响应为 AgentEvent。"""
        # usage
        if response.usage:
            yield Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

        # content blocks
        for block in response.content:
            if block.type == "text":
                yield TextDelta(text=block.text)
            elif block.type == "thinking":
                yield ThinkingDelta(text=getattr(block, "thinking", ""))
            elif block.type == "tool_use":
                # input 已是 dict
                arguments = block.input if isinstance(block.input, dict) else {}
                # 若 input 不是有效 dict（罕见），尝试修复
                if not arguments and hasattr(block, "input"):
                    input_str = str(block.input)
                    try:
                        arguments = json.loads(input_str)
                    except (json.JSONDecodeError, TypeError):
                        try:
                            arguments = json.loads(_repair_json(input_str))
                        except (json.JSONDecodeError, TypeError):
                            arguments = {}
                yield ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=arguments,
                )

        yield Done(stop_reason=response.stop_reason or "stop")

    def _convert_messages(
        self, messages: Sequence[Message | dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """将 Message 列表转换为 Anthropic 消息格式。

        - str content → {"role", "content"} 格式
        - list content → 透传（Anthropic content blocks，含 thinking blocks）
        - 原始 dict → 透传（如 format_tool_result 的输出）
        - 嵌套 dict content（含 role 键）→ 提取内部 dict
          （format 方法输出存为 content 时的还原）

        注意：assistant 消息中的 thinking blocks 必须被原样保留传回 API
        （Anthropic 要求），因此 list content 直接透传不做过滤。
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, dict):
                # 已是 provider-native 格式（如工具结果），检查是否需要解嵌套
                content = msg.get("content")
                if isinstance(content, dict) and "role" in content:
                    # content 是完整的消息 dict（来自 format 方法）
                    result.append(content)
                else:
                    result.append(msg)
            elif isinstance(msg.content, dict) and "role" in msg.content:
                # content 是完整的消息 dict（来自 format 方法）
                result.append(msg.content)
            else:
                # list content 直接透传（保留 thinking blocks）；
                # str content 转为标准格式
                result.append({"role": msg.role, "content": msg.content})
        return result

    def format_tool_result(
        self,
        role: str,
        tool_call_id: str,
        content: str,
    ) -> AnthropicToolResultMessage:
        """将工具结果格式化为 Anthropic 的 tool_result 消息格式。

        Args:
            role: 消息角色（Anthropic 工具结果为 "user"）。
            tool_call_id: 对应的工具调用 ID（Anthropic 中为 tool_use_id）。
            content: 工具执行结果文本。

        Returns:
            Anthropic 格式的 tool_result 消息（TypedDict）：
            {"role": "user", "content": [{"type": "tool_result",
             "tool_use_id": "...", "content": "...", "is_error": bool?}]}
        """
        block: AnthropicToolResultBlock = {
            "type": "tool_result",
            "tool_use_id": tool_call_id,
            "content": content,
        }
        if content.startswith("Error"):
            block["is_error"] = True
        return {
            "role": role,
            "content": [block],
        }

    def format_assistant_message(
        self,
        text: str,
        tool_calls: list[ToolCall],
    ) -> AnthropicAssistantMessage:
        """将 assistant 文本与工具调用格式化为 Anthropic assistant 消息。

        Args:
            text: assistant 文本内容（可能为空字符串）。
            tool_calls: ToolCall 列表（可能为空）。

        Returns:
            Anthropic 格式的 assistant 消息（TypedDict）：
            {"role": "assistant", "content": [
                {"type": "text", "text": text}?,  # text 非空时
                {"type": "tool_use", "id", "name", "input": dict}*
            ]}
        """
        content: list[AnthropicTextBlock | AnthropicToolUseBlock] = []
        if text:
            text_block: AnthropicTextBlock = {"type": "text", "text": text}
            content.append(text_block)
        for tc in tool_calls:
            tool_block: AnthropicToolUseBlock = {
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.arguments,
            }
            content.append(tool_block)
        return {"role": "assistant", "content": content}


__all__ = ["AnthropicProvider"]

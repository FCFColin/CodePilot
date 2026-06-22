"""OpenAI 兼容 Provider 实现。

基于 openai Python SDK，通过 OpenAI 兼容接口调用任意 OpenAI 兼容 API。
支持流式响应、工具调用、深度思考模式。

特性：
- tenacity 重试（API 调用失败自动重试，指数退避）
- structlog 结构化日志（API Key 不入日志）
- 完整类型注解，异常包装为 ProviderError
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import openai
import structlog
from openai import AsyncOpenAI
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
    BaseProvider,
    Done,
    Message,
    OpenAIAssistantMessage,
    OpenAIToolCall,
    OpenAIToolMessage,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    Usage,
)

logger = structlog.get_logger(__name__)


class OpenAICompatProvider(BaseProvider):
    """OpenAI 兼容 Provider，基于 openai SDK 的 OpenAI 兼容接口。"""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        # 创建异步 OpenAI 客户端，base_url 指向兼容端点
        # max_retries=0 禁用 SDK 内置重试，由 tenacity 统一管理重试
        logger.debug(
            "OpenAI 兼容 Provider 初始化",
            base_url=config.base_url,
            model=config.model,
        )
        self.client = AsyncOpenAI(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            max_retries=0,
            timeout=60.0,
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
        # 构建请求消息列表（system_prompt 作为 system 消息）
        request_messages: list[dict[str, Any]] = []
        if system_prompt:
            request_messages.append({"role": "system", "content": system_prompt})
        request_messages.extend(self._convert_messages(messages))

        # 构建请求参数
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": request_messages,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": self.config.max_tokens,
            "stream": stream,
        }

        # 工具定义（OpenAI function 格式：{"type": "function", "function": {...}}）
        if tools:
            kwargs["tools"] = tools

        # 流式响应启用 usage 上报
        # 注意：部分兼容端点不支持 stream_options，若请求失败则不带此参数重试
        if stream:
            kwargs["stream_options"] = {"include_usage": True}

        # 深度思考模式：通过 extra_body 传递
        if self.config.thinking.enabled:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        logger.debug(
            "发起 OpenAI 兼容请求",
            model=self.config.model,
            message_count=len(request_messages),
            stream=stream,
            has_tools=bool(tools),
        )

        # 发起请求（带重试），失败包装为 ProviderError
        # 部分兼容端点不支持 stream_options，若失败则去掉此参数重试
        try:
            response = await self._create_completion(**kwargs)
        except openai.APIError as e:
            # 若含 stream_options 且失败，尝试不带 stream_options 重试
            if "stream_options" in kwargs and stream:
                logger.warning(
                    "stream_options 不被支持，去掉后重试",
                    error=str(e),
                )
                kwargs_fallback = {
                    k: v for k, v in kwargs.items() if k != "stream_options"
                }
                try:
                    response = await self._create_completion(**kwargs_fallback)
                except openai.APIError as e2:
                    logger.error("OpenAI 兼容 API 调用失败", error=str(e2))
                    raise ProviderError(f"OpenAI 兼容 API 调用失败: {e2}") from e2
            else:
                logger.error("OpenAI 兼容 API 调用失败", error=str(e))
                raise ProviderError(f"OpenAI 兼容 API 调用失败: {e}") from e

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
        retry=retry_if_exception_type((openai.APIError,)),
        reraise=True,
    )
    async def _create_completion(self, **kwargs: Any) -> Any:
        """发起 chat completion 请求（带 tenacity 重试）。

        重试策略：最多 3 次，指数退避（1~10 秒），仅对 openai.APIError 重试。
        """
        return await self.client.chat.completions.create(**kwargs)

    async def _iter_stream(self, response: Any) -> AsyncIterator[AgentEvent]:
        """解析流式响应 chunk 为 AgentEvent。

        - delta.content → TextDelta
        - delta.reasoning_content → ThinkingDelta（thinking 模式）
        - delta.tool_calls → 累积 tool_call，finish_reason="tool_calls" 时发出 ToolCall
        - chunk.usage → Usage
        - choice.finish_reason → Done
        """
        # 累积工具调用的中间状态：{index: {"id": ..., "name": ..., "arguments": ...}}
        # 流式响应中 tool_calls 分多个 chunk 传入，需按 index 累积
        pending_tool_calls: dict[int, dict[str, str]] = {}

        try:
            async for chunk in response:
                # 处理 usage（开启 include_usage 后最后一个 chunk 携带）
                if chunk.usage is not None:
                    yield Usage(
                        input_tokens=chunk.usage.prompt_tokens,
                        output_tokens=chunk.usage.completion_tokens,
                    )

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                # 文本内容
                if delta and delta.content:
                    yield TextDelta(text=delta.content)

                # 思考内容（thinking 模式，扩展字段）
                if delta:
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield ThinkingDelta(text=reasoning)

                # 工具调用累积（分多个 chunk 传入，按 index 累积 id/name/arguments）
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index if tc.index is not None else 0
                        if idx not in pending_tool_calls:
                            pending_tool_calls[idx] = {
                                "id": "",
                                "name": "",
                                "arguments": "",
                            }
                        if tc.id:
                            pending_tool_calls[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            pending_tool_calls[idx]["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            pending_tool_calls[idx]["arguments"] += (
                                tc.function.arguments
                            )

                # 结束原因
                if choice.finish_reason:
                    # 工具调用结束：发出完整的 ToolCall 事件
                    if choice.finish_reason == "tool_calls":
                        for idx in sorted(pending_tool_calls.keys()):
                            tc_data = pending_tool_calls[idx]
                            # arguments 是 JSON 字符串，需 parse 为 dict
                            try:
                                arguments = (
                                    json.loads(tc_data["arguments"])
                                    if tc_data["arguments"]
                                    else {}
                                )
                            except json.JSONDecodeError:
                                arguments = {}
                            yield ToolCall(
                                id=tc_data["id"],
                                name=tc_data["name"],
                                arguments=arguments,
                            )
                    yield Done(stop_reason=choice.finish_reason)
        except openai.APIError as e:
            logger.error("OpenAI 兼容流式解析失败", error=str(e))
            raise ProviderError(f"OpenAI 兼容流式解析失败: {e}") from e

    async def _iter_non_stream(self, response: Any) -> AsyncIterator[AgentEvent]:
        """解析非流式响应为 AgentEvent。"""
        # usage
        if response.usage:
            yield Usage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
            )

        for choice in response.choices:
            msg = choice.message

            # 文本内容
            if msg.content:
                yield TextDelta(text=msg.content)

            # 思考内容
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning:
                yield ThinkingDelta(text=reasoning)

            # 工具调用
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    try:
                        arguments = (
                            json.loads(tc.function.arguments)
                            if tc.function.arguments
                            else {}
                        )
                    except json.JSONDecodeError:
                        arguments = {}
                    yield ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=arguments,
                    )

            yield Done(stop_reason=choice.finish_reason or "stop")

    def _convert_messages(
        self, messages: Sequence[Message | dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """将 Message 列表转换为 OpenAI 消息格式。

        - str content → 标准 {"role", "content"} 格式
        - list/dict content → 透传（已为 provider-native 结构）
        - 原始 dict → 透传（如 format_tool_result 的输出）
        - 嵌套 dict content（含 role 键）→ 提取内部 dict
          （format 方法输出存为 content 时的还原）
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
            elif isinstance(msg.content, (list, dict)):
                result.append({"role": msg.role, "content": msg.content})
            else:
                result.append({"role": msg.role, "content": str(msg.content)})
        return result

    def format_tool_result(
        self,
        role: str,
        tool_call_id: str,
        content: str,
    ) -> OpenAIToolMessage:
        """将工具结果格式化为 OpenAI 的 tool 消息格式。

        Args:
            role: 消息角色（OpenAI 工具结果为 "tool"）。
            tool_call_id: 对应的工具调用 ID。
            content: 工具执行结果文本。

        Returns:
            OpenAI 格式的 tool 消息（TypedDict）：
            {"role": "tool", "tool_call_id": "...", "content": "..."}
        """
        return {
            "role": role,
            "tool_call_id": tool_call_id,
            "content": content,
        }

    def format_assistant_message(
        self,
        text: str,
        tool_calls: list[ToolCall],
    ) -> OpenAIAssistantMessage:
        """将 assistant 文本与工具调用格式化为 OpenAI assistant 消息。

        Args:
            text: assistant 文本内容（可能为空字符串）。
            tool_calls: ToolCall 列表（可能为空）。

        Returns:
            OpenAI 格式的 assistant 消息（TypedDict）：
            - 无工具调用：{"role": "assistant", "content": text}
            - 有工具调用：{"role": "assistant", "content": text,
                        "tool_calls": [{"id", "type": "function",
                        "function": {"name", "arguments": json字符串}}]}
        """
        if not tool_calls:
            return {"role": "assistant", "content": text}
        tool_call_dicts: list[OpenAIToolCall] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in tool_calls
        ]
        return {
            "role": "assistant",
            "content": text,
            "tool_calls": tool_call_dicts,
        }


# 向后兼容别名
DeepSeekProvider = OpenAICompatProvider

__all__ = ["OpenAICompatProvider", "DeepSeekProvider"]

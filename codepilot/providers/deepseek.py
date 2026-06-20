"""DeepSeek Provider 实现。

基于 openai Python SDK，通过 OpenAI 兼容接口调用 DeepSeek API。
支持流式响应、工具调用、深度思考模式。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import openai

from codepilot.config import DeepSeekConfig
from codepilot.providers.base import (
    AgentEvent,
    BaseProvider,
    Done,
    Message,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    Usage,
)


class DeepSeekProvider(BaseProvider):
    """DeepSeek Provider，基于 openai SDK 的 OpenAI 兼容接口。"""

    def __init__(self, config: DeepSeekConfig) -> None:
        self.config = config
        # 创建异步 OpenAI 客户端，base_url 指向 DeepSeek
        self.client = openai.AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        system_prompt: str = "",
        stream: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        """发送消息并获取响应，返回 AgentEvent 事件流。"""
        # 构建请求消息列表（system_prompt 作为 system 消息）
        request_messages: list[dict] = []
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
        if stream:
            kwargs["stream_options"] = {"include_usage": True}

        # 深度思考模式：通过 extra_body 传递
        if self.config.thinking.enabled:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        try:
            response = await self.client.chat.completions.create(**kwargs)
        except openai.OpenAIError as e:
            # API 错误：发出 Done 事件含错误信息
            yield Done(stop_reason=f"error: {e}")
            return

        if stream:
            async for event in self._iter_stream(response):
                yield event
        else:
            async for event in self._iter_non_stream(response):
                yield event

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

                # 思考内容（thinking 模式，DeepSeek 扩展字段）
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
                            pending_tool_calls[idx]["arguments"] += tc.function.arguments

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
        except openai.OpenAIError as e:
            yield Done(stop_reason=f"error: {e}")

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

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """将 Message 列表转换为 OpenAI 消息格式。

        - str content → 标准 {"role", "content"} 格式
        - list/dict content → 透传（已为 provider-native 结构）
        - 原始 dict → 透传（如 format_tool_result 的输出）
        - 嵌套 dict content（含 role 键）→ 提取内部 dict
          （format_assistant_message/format_tool_result 输出存为 content 时的还原）
        """
        result: list[dict] = []
        for msg in messages:
            if isinstance(msg, dict):
                # 已是 provider-native 格式（如工具结果），检查是否需要解嵌套
                content = msg.get("content")
                if isinstance(content, dict) and "role" in content:
                    # content 是完整的消息 dict（来自 format_assistant_message/format_tool_result）
                    result.append(content)
                else:
                    result.append(msg)
            elif isinstance(msg.content, dict) and "role" in msg.content:
                # content 是完整的消息 dict（来自 format_assistant_message/format_tool_result）
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
    ) -> dict:
        """将工具结果格式化为 OpenAI 的 tool 消息格式。

        Args:
            role: 消息角色（OpenAI 工具结果为 "tool"）。
            tool_call_id: 对应的工具调用 ID。
            content: 工具执行结果文本。

        Returns:
            OpenAI 格式的 tool 消息字典：
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
    ) -> dict:
        """将 assistant 文本与工具调用格式化为 OpenAI assistant 消息。

        Args:
            text: assistant 文本内容（可能为空字符串）。
            tool_calls: ToolCall 列表（可能为空）。

        Returns:
            OpenAI 格式的 assistant 消息字典：
            - 无工具调用：{"role": "assistant", "content": text}
            - 有工具调用：{"role": "assistant", "content": text,
                        "tool_calls": [{"id", "type": "function",
                        "function": {"name", "arguments": json字符串}}]}
        """
        if not tool_calls:
            return {"role": "assistant", "content": text}
        return {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ],
        }


__all__ = ["DeepSeekProvider"]

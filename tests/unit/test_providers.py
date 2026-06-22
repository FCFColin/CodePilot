"""Provider 单元测试。

覆盖：
- format_tool_result / format_assistant_message 格式正确性
- _convert_messages 对 Message 对象和嵌套 dict 的还原
- 流式响应解析（respx mock HTTP，验证 AgentEvent 序列）
- 异常包装为 ProviderError
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import openai
import pytest
import respx
from pydantic import SecretStr

from codepilot.config import ProviderConfig
from codepilot.exceptions import ProviderError
from codepilot.providers.anthropic import AnthropicProvider
from codepilot.providers.base import (
    Done,
    Message,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    Usage,
)
from codepilot.providers.openai_compat import OpenAICompatProvider

# ============================================================================
# 常量与辅助函数
# ============================================================================

_DEEPSEEK_URL = "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2/chat/completions"
_ANTHROPIC_URL = "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic/v1/messages"


def _deepseek_config() -> ProviderConfig:
    """构造测试用 DeepSeek（OpenAI 兼容）ProviderConfig。"""
    return ProviderConfig(
        type="openai",
        api_key=SecretStr("sk-test-deepseek"),
        base_url="https://maas-coding-api.cn-huabei-1.xf-yun.com/v2",
        model="astron-code-latest",
        temperature=1.0,
        top_p=1.0,
    )


def _provider_config() -> ProviderConfig:
    """构造测试用 OpenAI 兼容 ProviderConfig。"""
    return _deepseek_config()


def _anthropic_config() -> ProviderConfig:
    """构造测试用 Anthropic ProviderConfig。"""
    return ProviderConfig(
        type="anthropic",
        api_key=SecretStr("sk-test-anthropic"),
        base_url="https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic",
        model="claude-3",
    )


def _openai_sse(chunks: list[dict[str, Any]]) -> bytes:
    """构建 OpenAI SSE 流式响应体。"""
    parts: list[str] = []
    for chunk in chunks:
        parts.append(f"data: {json.dumps(chunk)}\n\n")
    parts.append("data: [DONE]\n\n")
    return "".join(parts).encode("utf-8")


def _anthropic_sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    """构建 Anthropic SSE 流式响应体。"""
    parts: list[str] = []
    for event_type, data in events:
        parts.append(f"event: {event_type}\n")
        parts.append(f"data: {json.dumps(data)}\n\n")
    return "".join(parts).encode("utf-8")


async def _collect(gen: Any) -> list[Any]:
    """收集异步生成器产出的所有事件。"""
    results: list[Any] = []
    async for item in gen:
        results.append(item)
    return results


def _sse_response(content: bytes) -> httpx.Response:
    """构造 SSE 流式 httpx 响应。"""
    return httpx.Response(
        200,
        content=content,
        headers={"content-type": "text/event-stream"},
    )


def _json_response(data: dict[str, Any]) -> httpx.Response:
    """构造 JSON httpx 响应。"""
    return httpx.Response(
        200,
        json=data,
        headers={"content-type": "application/json"},
    )


class _MockStreamChunk:
    """模拟 OpenAI 流式 chunk 对象。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @property
    def usage(self) -> Any:
        return self._data.get("usage")

    @property
    def choices(self) -> list[Any]:
        return self._data.get("choices", [])

    def __getattr__(self, name: str) -> Any:
        return self._data.get(name)


class _MockStreamDelta:
    """模拟 delta 对象。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @property
    def content(self) -> str | None:
        return self._data.get("content")

    def __getattr__(self, name: str) -> Any:
        return self._data.get(name)


class _MockStreamChoice:
    """模拟 choice 对象。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        delta_data = data.get("delta", {})
        self.delta = _MockStreamDelta(delta_data)

    @property
    def finish_reason(self) -> str | None:
        return self._data.get("finish_reason")


class _MockStreamResponse:
    """模拟流式响应异步迭代器。"""

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> _MockStreamResponse:
        return self

    async def __anext__(self) -> _MockStreamChunk:
        if not self._chunks:
            raise StopAsyncIteration
        chunk_data = self._chunks.pop(0)
        chunk = _MockStreamChunk(chunk_data)
        # 替换 choices 中的 dict 为 _MockStreamChoice
        raw_choices = chunk_data.get("choices", [])
        chunk._data["choices"] = [_MockStreamChoice(c) for c in raw_choices]
        return chunk


def _mock_stream_response(chunks: list[dict[str, Any]]) -> _MockStreamResponse:
    """构造模拟的流式响应对象。"""
    return _MockStreamResponse(chunks)


class _MockNonStreamMessage:
    """模拟非流式响应中的 message 对象。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.content = data.get("content", "")
        self.tool_calls = data.get("tool_calls")
        self.role = data.get("role", "assistant")

    def __getattr__(self, name: str) -> Any:
        return self._data.get(name)


class _MockNonStreamChoice:
    """模拟非流式响应中的 choice 对象。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.message = _MockNonStreamMessage(data.get("message", {}))
        self.finish_reason = data.get("finish_reason")

    def __getattr__(self, name: str) -> Any:
        return self._data.get(name)


class _MockUsage:
    """模拟 usage 对象。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self.prompt_tokens = data.get("prompt_tokens", 0)
        self.completion_tokens = data.get("completion_tokens", 0)
        self.total_tokens = data.get("total_tokens", 0)


class _MockNonStreamResponse:
    """模拟非流式响应对象。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.usage = _MockUsage(data.get("usage", {})) if "usage" in data else None
        raw_choices = data.get("choices", [])
        self.choices = [_MockNonStreamChoice(c) for c in raw_choices]
        self.id = data.get("id")
        self.object = data.get("object")


# ============================================================================
# TestFormatToolResult
# ============================================================================


class TestFormatToolResult:
    """format_tool_result 格式正确性测试。"""

    def test_deepseek_format_tool_result(self) -> None:
        """DeepSeek 工具结果为 OpenAI tool 消息格式。"""
        provider = OpenAICompatProvider(_provider_config())
        result = provider.format_tool_result(
            role="tool",
            tool_call_id="call_123",
            content="文件内容",
        )
        assert result["role"] == "tool"
        assert result["tool_call_id"] == "call_123"
        assert result["content"] == "文件内容"
        # 确认 TypedDict 键集
        assert set(result.keys()) == {"role", "tool_call_id", "content"}

    def test_anthropic_format_tool_result(self) -> None:
        """Anthropic 工具结果为 user 角色 + tool_result block。"""
        provider = AnthropicProvider(_anthropic_config())
        result = provider.format_tool_result(
            role="user",
            tool_call_id="toolu_456",
            content="执行成功",
        )
        assert result["role"] == "user"
        assert isinstance(result["content"], list)
        assert len(result["content"]) == 1
        block = result["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "toolu_456"
        assert block["content"] == "执行成功"


# ============================================================================
# TestFormatAssistantMessage
# ============================================================================


class TestFormatAssistantMessage:
    """format_assistant_message 格式正确性测试。"""

    def test_deepseek_no_tool_calls(self) -> None:
        """DeepSeek 无工具调用时仅含 role 和 content。"""
        provider = OpenAICompatProvider(_provider_config())
        result = provider.format_assistant_message("你好", [])
        assert result["role"] == "assistant"
        assert result["content"] == "你好"
        assert "tool_calls" not in result

    def test_deepseek_with_tool_calls(self) -> None:
        """DeepSeek 有工具调用时包含 tool_calls 列表。"""
        provider = OpenAICompatProvider(_provider_config())
        calls = [
            ToolCall(id="call_1", name="get_weather", arguments={"city": "SF"}),
            ToolCall(id="call_2", name="get_time", arguments={}),
        ]
        result = provider.format_assistant_message("", calls)
        assert result["role"] == "assistant"
        assert result["content"] == ""
        tc_list = result["tool_calls"]
        assert len(tc_list) == 2
        assert tc_list[0]["id"] == "call_1"
        assert tc_list[0]["type"] == "function"
        assert tc_list[0]["function"]["name"] == "get_weather"
        # arguments 为 JSON 字符串
        assert json.loads(tc_list[0]["function"]["arguments"]) == {"city": "SF"}
        assert json.loads(tc_list[1]["function"]["arguments"]) == {}

    def test_anthropic_no_tool_calls(self) -> None:
        """Anthropic 无工具调用时 content 仅含 text block。"""
        provider = AnthropicProvider(_anthropic_config())
        result = provider.format_assistant_message("你好", [])
        assert result["role"] == "assistant"
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "你好"

    def test_anthropic_with_tool_calls(self) -> None:
        """Anthropic 有工具调用时 content 含 text 和 tool_use block。"""
        provider = AnthropicProvider(_anthropic_config())
        calls = [
            ToolCall(id="toolu_1", name="search", arguments={"q": "test"}),
        ]
        result = provider.format_assistant_message("正在搜索", calls)
        assert result["role"] == "assistant"
        assert len(result["content"]) == 2
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "正在搜索"
        block = result["content"][1]
        assert block["type"] == "tool_use"
        assert block["id"] == "toolu_1"
        assert block["name"] == "search"
        assert block["input"] == {"q": "test"}

    def test_anthropic_empty_text_with_tool_calls(self) -> None:
        """Anthropic 空文本 + 工具调用时 content 不含 text block。"""
        provider = AnthropicProvider(_anthropic_config())
        calls = [ToolCall(id="toolu_2", name="run", arguments={})]
        result = provider.format_assistant_message("", calls)
        assert result["role"] == "assistant"
        # 空文本不产生 text block
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "tool_use"


# ============================================================================
# TestConvertMessages
# ============================================================================


class TestConvertMessages:
    """_convert_messages 消息转换测试。"""

    def test_deepseek_convert_str_content(self) -> None:
        """DeepSeek 字符串 content 转为标准格式。"""
        provider = OpenAICompatProvider(_provider_config())
        messages = [
            Message(role="user", content="你好"),
            Message(role="assistant", content="你好，有什么可以帮你？"),
        ]
        result = provider._convert_messages(messages)
        assert len(result) == 2
        assert result[0] == {"role": "user", "content": "你好"}
        assert result[1] == {
            "role": "assistant",
            "content": "你好，有什么可以帮你？",
        }

    def test_deepseek_convert_nested_dict_content(self) -> None:
        """DeepSeek 嵌套 dict content（含 role 键）被提取。"""
        provider = OpenAICompatProvider(_provider_config())
        # 模拟 format_assistant_message 输出存为 content
        inner = {"role": "assistant", "content": "回复", "tool_calls": []}
        messages = [Message(role="assistant", content=inner)]
        result = provider._convert_messages(messages)
        assert result[0] == inner

    def test_deepseek_convert_raw_dict(self) -> None:
        """DeepSeek 原始 dict 透传，嵌套 dict content 被提取。"""
        provider = OpenAICompatProvider(_provider_config())
        # 原始 dict（如 format_tool_result 输出）
        tool_msg = {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "结果",
        }
        # 嵌套 dict content
        nested = {
            "role": "user",
            "content": {"role": "assistant", "content": "内层"},
        }
        result = provider._convert_messages([tool_msg, nested])
        assert result[0] == tool_msg
        assert result[1] == {"role": "assistant", "content": "内层"}

    def test_anthropic_convert_str_content(self) -> None:
        """Anthropic 字符串 content 转为标准格式。"""
        provider = AnthropicProvider(_anthropic_config())
        messages = [Message(role="user", content="你好")]
        result = provider._convert_messages(messages)
        assert result == [{"role": "user", "content": "你好"}]

    def test_anthropic_convert_nested_dict_content(self) -> None:
        """Anthropic 嵌套 dict content（含 role 键）被提取。"""
        provider = AnthropicProvider(_anthropic_config())
        inner = {
            "role": "assistant",
            "content": [{"type": "text", "text": "回复"}],
        }
        messages = [Message(role="assistant", content=inner)]
        result = provider._convert_messages(messages)
        assert result[0] == inner

    def test_anthropic_convert_raw_dict(self) -> None:
        """Anthropic 原始 dict 透传。"""
        provider = AnthropicProvider(_anthropic_config())
        tool_msg = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        }
        result = provider._convert_messages([tool_msg])
        assert result[0] == tool_msg


# ============================================================================
# TestStreamingParse
# ============================================================================


class TestStreamingParse:
    """流式响应解析测试（respx mock HTTP）。"""

    async def test_deepseek_streaming_text_usage_done(self) -> None:
        """DeepSeek 流式：TextDelta + Usage + Done。"""
        chunks = [
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}
                ],
            },
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"content": " world"}, "finish_reason": None}
                ],
            },
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        ]
        with respx.mock:
            respx.post(_DEEPSEEK_URL).mock(
                return_value=_sse_response(_openai_sse(chunks))
            )
            provider = OpenAICompatProvider(_provider_config())
            events = await _collect(provider.chat([Message(role="user", content="hi")]))
        # 验证事件序列
        assert isinstance(events[0], TextDelta)
        assert events[0].text == "Hello"
        assert isinstance(events[1], TextDelta)
        assert events[1].text == " world"
        assert isinstance(events[2], Usage)
        assert events[2].input_tokens == 10
        assert events[2].output_tokens == 5
        assert isinstance(events[3], Done)
        assert events[3].stop_reason == "stop"

    async def test_deepseek_streaming_thinking(self) -> None:
        """DeepSeek 流式：ThinkingDelta（reasoning_content）。"""
        chunks = [
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": "思考中..."},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"content": "答案"}, "finish_reason": "stop"}
                ],
            },
        ]
        with respx.mock:
            respx.post(_DEEPSEEK_URL).mock(
                return_value=_sse_response(_openai_sse(chunks))
            )
            provider = OpenAICompatProvider(_provider_config())
            events = await _collect(provider.chat([Message(role="user", content="hi")]))
        assert isinstance(events[0], ThinkingDelta)
        assert events[0].text == "思考中..."
        assert isinstance(events[1], TextDelta)
        assert events[1].text == "答案"
        assert isinstance(events[-1], Done)

    async def test_deepseek_streaming_tool_call(self) -> None:
        """DeepSeek 流式：ToolCall（分片累积 arguments）。"""
        chunks = [
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"city": "SF"}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        with respx.mock:
            respx.post(_DEEPSEEK_URL).mock(
                return_value=_sse_response(_openai_sse(chunks))
            )
            provider = OpenAICompatProvider(_provider_config())
            events = await _collect(
                provider.chat([Message(role="user", content="天气?")])
            )
        # 应产出 ToolCall + Done
        tool_calls = [e for e in events if isinstance(e, ToolCall)]
        assert len(tool_calls) == 1
        assert tool_calls[0].id == "call_1"
        assert tool_calls[0].name == "get_weather"
        assert tool_calls[0].arguments == {"city": "SF"}
        assert isinstance(events[-1], Done)
        assert events[-1].stop_reason == "tool_calls"

    async def test_deepseek_non_stream(self) -> None:
        """DeepSeek 非流式：完整 JSON 响应解析。"""
        resp = {
            "id": "c1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "完整回复"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
        }
        with respx.mock:
            respx.post(_DEEPSEEK_URL).mock(return_value=_json_response(resp))
            provider = OpenAICompatProvider(_provider_config())
            events = await _collect(
                provider.chat([Message(role="user", content="hi")], stream=False)
            )
        assert isinstance(events[0], Usage)
        assert events[0].input_tokens == 8
        assert isinstance(events[1], TextDelta)
        assert events[1].text == "完整回复"
        assert isinstance(events[-1], Done)
        assert events[-1].stop_reason == "stop"

    async def test_anthropic_streaming_text_usage_done(self) -> None:
        """Anthropic 流式：TextDelta + Usage + Done。"""
        events_data = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "claude-3",
                        "stop_reason": None,
                        "usage": {"input_tokens": 10, "output_tokens": 0},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": " world"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 5},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        with respx.mock:
            respx.post(_ANTHROPIC_URL).mock(
                return_value=_sse_response(_anthropic_sse(events_data))
            )
            provider = AnthropicProvider(_anthropic_config())
            events = await _collect(provider.chat([Message(role="user", content="hi")]))
        # message_start → Usage(input=10, output=0)
        assert isinstance(events[0], Usage)
        assert events[0].input_tokens == 10
        # text deltas
        assert isinstance(events[1], TextDelta)
        assert events[1].text == "Hello"
        assert isinstance(events[2], TextDelta)
        assert events[2].text == " world"
        # message_delta → Usage(output=5)
        usage_events = [e for e in events if isinstance(e, Usage)]
        assert len(usage_events) == 2
        assert usage_events[1].output_tokens == 5
        # message_stop → Done
        assert isinstance(events[-1], Done)
        assert events[-1].stop_reason == "end_turn"

    async def test_anthropic_streaming_thinking(self) -> None:
        """Anthropic 流式：ThinkingDelta。"""
        events_data = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "claude-3",
                        "stop_reason": None,
                        "usage": {"input_tokens": 5, "output_tokens": 0},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "让我想想..."},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 3},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        with respx.mock:
            respx.post(_ANTHROPIC_URL).mock(
                return_value=_sse_response(_anthropic_sse(events_data))
            )
            provider = AnthropicProvider(_anthropic_config())
            events = await _collect(provider.chat([Message(role="user", content="hi")]))
        thinking = [e for e in events if isinstance(e, ThinkingDelta)]
        assert len(thinking) == 1
        assert thinking[0].text == "让我想想..."
        assert isinstance(events[-1], Done)

    async def test_anthropic_streaming_tool_call(self) -> None:
        """Anthropic 流式：ToolCall（input_json_delta 累积）。"""
        events_data = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "claude-3",
                        "stop_reason": None,
                        "usage": {"input_tokens": 5, "output_tokens": 0},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {},
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"city"'},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": ': "SF"}'},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 10},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        with respx.mock:
            respx.post(_ANTHROPIC_URL).mock(
                return_value=_sse_response(_anthropic_sse(events_data))
            )
            provider = AnthropicProvider(_anthropic_config())
            events = await _collect(
                provider.chat([Message(role="user", content="天气?")])
            )
        tool_calls = [e for e in events if isinstance(e, ToolCall)]
        assert len(tool_calls) == 1
        assert tool_calls[0].id == "toolu_1"
        assert tool_calls[0].name == "get_weather"
        assert tool_calls[0].arguments == {"city": "SF"}
        assert isinstance(events[-1], Done)
        assert events[-1].stop_reason == "tool_use"

    async def test_anthropic_non_stream(self) -> None:
        """Anthropic 非流式：完整 JSON 响应解析。"""
        resp = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "完整回复"}],
            "model": "claude-3",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 8, "output_tokens": 3},
        }
        with respx.mock:
            respx.post(_ANTHROPIC_URL).mock(return_value=_json_response(resp))
            provider = AnthropicProvider(_anthropic_config())
            events = await _collect(
                provider.chat([Message(role="user", content="hi")], stream=False)
            )
        assert isinstance(events[0], Usage)
        assert events[0].input_tokens == 8
        assert isinstance(events[1], TextDelta)
        assert events[1].text == "完整回复"
        assert isinstance(events[-1], Done)
        assert events[-1].stop_reason == "end_turn"


# ============================================================================
# TestErrorHandling
# ============================================================================


class TestErrorHandling:
    """异常包装测试。"""

    async def test_deepseek_api_error_raises_provider_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DeepSeek API 错误包装为 ProviderError。"""

        # 加速 tenacity 重试：零等待
        async def _instant_sleep(_s: float) -> None:
            pass

        monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

        with respx.mock:
            respx.post(_DEEPSEEK_URL).mock(
                return_value=httpx.Response(
                    500,
                    json={
                        "error": {
                            "message": "Internal error",
                            "type": "server_error",
                        }
                    },
                )
            )
            provider = OpenAICompatProvider(_provider_config())
            with pytest.raises(ProviderError, match="OpenAI 兼容"):
                await _collect(provider.chat([Message(role="user", content="hi")]))

    async def test_deepseek_stream_options_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stream_options 不被支持时回退移除该参数并重试成功。"""

        provider = OpenAICompatProvider(_provider_config())

        # 构造成功的流式响应
        chunks = [
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"content": "OK"}, "finish_reason": None}
                ],
            },
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]

        call_count = 0

        async def _mock_create_completion(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # 第一次调用：带 stream_options，抛出 APIError
                assert "stream_options" in kwargs
                raise openai.APIError(
                    message="stream_options not supported",
                    request=None,
                    body=None,
                )
            # 第二次调用：不带 stream_options，返回成功
            assert "stream_options" not in kwargs
            return _mock_stream_response(chunks)

        # 直接 mock _create_completion，绕过 tenacity 重试
        monkeypatch.setattr(
            provider, "_create_completion", _mock_create_completion
        )

        events = await _collect(
            provider.chat([Message(role="user", content="hi")])
        )
        # 验证回退成功
        assert call_count == 2
        assert isinstance(events[0], TextDelta)
        assert events[0].text == "OK"
        assert isinstance(events[-1], Done)

    async def test_deepseek_stream_options_fallback_both_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stream_options 回退后仍然失败时抛出 ProviderError。"""

        provider = OpenAICompatProvider(_provider_config())

        call_count = 0

        async def _mock_create_completion(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            raise openai.APIError(
                message=f"error on call {call_count}",
                request=None,
                body=None,
            )

        # 直接 mock _create_completion，绕过 tenacity 重试
        monkeypatch.setattr(
            provider, "_create_completion", _mock_create_completion
        )

        with pytest.raises(ProviderError, match="OpenAI 兼容"):
            await _collect(
                provider.chat([Message(role="user", content="hi")])
            )
        assert call_count == 2

    async def test_anthropic_api_error_raises_provider_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anthropic API 错误包装为 ProviderError。"""

        async def _instant_sleep(_s: float) -> None:
            pass

        monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

        with respx.mock:
            respx.post(_ANTHROPIC_URL).mock(
                return_value=httpx.Response(
                    500,
                    json={
                        "type": "error",
                        "error": {"type": "server_error", "message": "Internal error"},
                    },
                )
            )
            provider = AnthropicProvider(_anthropic_config())
            with pytest.raises(ProviderError, match="Anthropic"):
                await _collect(provider.chat([Message(role="user", content="hi")]))


# ============================================================================
# TestProviderDifferences：Provider 差异专项测试
# ============================================================================


class TestProviderDifferences:
    """Provider 差异专项测试。

    覆盖：
    - OpenAICompatProvider 与 AnthropicProvider 的工具定义格式差异
    - OpenAICompatProvider 的 stream_options 降级逻辑
    - AnthropicProvider 的 content-block 架构处理
    - 两种 Provider 对 ToolCall 的解析差异
    """

    # ------------------------------------------------------------------
    # 1. 工具定义格式差异
    # ------------------------------------------------------------------

    def test_tool_result_format_difference(self) -> None:
        """OpenAI 工具结果为扁平 tool 消息；Anthropic 为 user 角色 + content blocks。"""
        openai_provider = OpenAICompatProvider(_provider_config())
        anthropic_provider = AnthropicProvider(_anthropic_config())

        openai_result = openai_provider.format_tool_result(
            role="tool", tool_call_id="call_1", content="结果"
        )
        anthropic_result = anthropic_provider.format_tool_result(
            role="user", tool_call_id="toolu_1", content="结果"
        )

        # OpenAI：扁平结构，role=tool，顶层 tool_call_id
        assert openai_result["role"] == "tool"
        assert isinstance(openai_result["content"], str)
        assert openai_result["tool_call_id"] == "call_1"
        # 无 content blocks
        assert not isinstance(openai_result["content"], list)

        # Anthropic：role=user，content 为 content blocks 列表
        assert anthropic_result["role"] == "user"
        assert isinstance(anthropic_result["content"], list)
        block = anthropic_result["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "toolu_1"

    def test_assistant_message_format_difference(self) -> None:
        """OpenAI assistant 消息用顶层 tool_calls 列表；Anthropic 用 content blocks。"""
        openai_provider = OpenAICompatProvider(_provider_config())
        anthropic_provider = AnthropicProvider(_anthropic_config())

        calls = [ToolCall(id="id_1", name="search", arguments={"q": "test"})]

        openai_msg = openai_provider.format_assistant_message("搜索中", calls)
        anthropic_msg = anthropic_provider.format_assistant_message("搜索中", calls)

        # OpenAI：content 为字符串，tool_calls 为顶层列表
        assert isinstance(openai_msg["content"], str)
        assert openai_msg["content"] == "搜索中"
        assert "tool_calls" in openai_msg
        tc = openai_msg["tool_calls"][0]
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "search"
        # arguments 是 JSON 字符串
        assert isinstance(tc["function"]["arguments"], str)
        assert json.loads(tc["function"]["arguments"]) == {"q": "test"}

        # Anthropic：content 为 blocks 列表，含 text + tool_use
        assert isinstance(anthropic_msg["content"], list)
        assert len(anthropic_msg["content"]) == 2
        assert anthropic_msg["content"][0]["type"] == "text"
        assert anthropic_msg["content"][0]["text"] == "搜索中"
        tool_block = anthropic_msg["content"][1]
        assert tool_block["type"] == "tool_use"
        assert tool_block["name"] == "search"
        # input 是 dict，非 JSON 字符串
        assert isinstance(tool_block["input"], dict)
        assert tool_block["input"] == {"q": "test"}

    # ------------------------------------------------------------------
    # 2. stream_options 降级逻辑
    # ------------------------------------------------------------------

    async def test_stream_options_removed_on_api_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stream_options 导致 APIError 时，移除后重试成功。"""
        provider = OpenAICompatProvider(_provider_config())

        chunks = [
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"content": "降级成功"}, "finish_reason": None}
                ],
            },
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]

        call_count = 0
        captured_kwargs: list[dict[str, Any]] = []

        async def _mock_create_completion(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            captured_kwargs.append(dict(kwargs))
            if call_count == 1:
                # 第一次带 stream_options，模拟不支持
                raise openai.APIError(
                    message="stream_options is not supported",
                    request=None,
                    body=None,
                )
            # 第二次不带 stream_options，成功
            return _mock_stream_response(chunks)

        monkeypatch.setattr(provider, "_create_completion", _mock_create_completion)

        events = await _collect(
            provider.chat([Message(role="user", content="hi")])
        )

        # 第一次调用应有 stream_options
        assert "stream_options" in captured_kwargs[0]
        # 第二次调用不应有 stream_options
        assert "stream_options" not in captured_kwargs[1]
        # 最终成功返回
        assert call_count == 2
        assert isinstance(events[0], TextDelta)
        assert events[0].text == "降级成功"

    async def test_stream_options_not_removed_when_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非流式请求不添加 stream_options，无需降级。"""
        provider = OpenAICompatProvider(_provider_config())

        resp = {
            "id": "c1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "非流式回复"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }

        captured_kwargs: list[dict[str, Any]] = []

        async def _mock_create_completion(**kwargs: Any) -> Any:
            captured_kwargs.append(dict(kwargs))
            # 直接返回非流式响应对象
            return _MockNonStreamResponse(resp)

        monkeypatch.setattr(provider, "_create_completion", _mock_create_completion)

        events = await _collect(
            provider.chat([Message(role="user", content="hi")], stream=False)
        )

        # 非流式请求不应有 stream_options
        assert "stream_options" not in captured_kwargs[0]
        assert isinstance(events[1], TextDelta)
        assert events[1].text == "非流式回复"

    # ------------------------------------------------------------------
    # 3. Anthropic content-block 架构处理
    # ------------------------------------------------------------------

    def test_anthropic_text_only_content_blocks(self) -> None:
        """Anthropic 纯文本消息：content 为单个 text block 列表。"""
        provider = AnthropicProvider(_anthropic_config())
        result = provider.format_assistant_message("纯文本回复", [])
        assert result["role"] == "assistant"
        assert len(result["content"]) == 1
        assert result["content"][0] == {"type": "text", "text": "纯文本回复"}

    def test_anthropic_tool_use_only_content_blocks(self) -> None:
        """Anthropic 仅工具调用（空文本）：content 不含 text block。"""
        provider = AnthropicProvider(_anthropic_config())
        calls = [ToolCall(id="toolu_1", name="run", arguments={"cmd": "ls"})]
        result = provider.format_assistant_message("", calls)
        assert result["role"] == "assistant"
        # 空文本不产生 text block
        text_blocks = [b for b in result["content"] if b["type"] == "text"]
        assert len(text_blocks) == 0
        tool_blocks = [b for b in result["content"] if b["type"] == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0]["id"] == "toolu_1"
        assert tool_blocks[0]["input"] == {"cmd": "ls"}

    def test_anthropic_mixed_content_blocks(self) -> None:
        """Anthropic 文本 + 工具调用：content 含 text + tool_use blocks。"""
        provider = AnthropicProvider(_anthropic_config())
        calls = [
            ToolCall(id="toolu_1", name="search", arguments={"q": "a"}),
            ToolCall(id="toolu_2", name="run", arguments={"cmd": "ls"}),
        ]
        result = provider.format_assistant_message("执行两个工具", calls)
        assert result["role"] == "assistant"
        # 1 text + 2 tool_use = 3 blocks
        assert len(result["content"]) == 3
        assert result["content"][0]["type"] == "text"
        assert result["content"][1]["type"] == "tool_use"
        assert result["content"][2]["type"] == "tool_use"
        # 验证 input 是 dict（非 JSON 字符串）
        assert isinstance(result["content"][1]["input"], dict)
        assert isinstance(result["content"][2]["input"], dict)

    # ------------------------------------------------------------------
    # 4. ToolCall 解析差异
    # ------------------------------------------------------------------

    async def test_openai_tool_call_arguments_is_json_string(self) -> None:
        """OpenAI 流式 ToolCall：arguments 是 JSON 字符串，解析后为 dict。"""
        chunks = [
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "edit_file",
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "arguments": '{"path": "/tmp/f.txt", "content": "hello"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        with respx.mock:
            respx.post(_DEEPSEEK_URL).mock(
                return_value=_sse_response(_openai_sse(chunks))
            )
            provider = OpenAICompatProvider(_provider_config())
            events = await _collect(
                provider.chat([Message(role="user", content="编辑文件")])
            )
        tool_calls = [e for e in events if isinstance(e, ToolCall)]
        assert len(tool_calls) == 1
        # arguments 应被解析为 dict（原始为 JSON 字符串）
        assert tool_calls[0].arguments == {"path": "/tmp/f.txt", "content": "hello"}
        assert isinstance(tool_calls[0].arguments, dict)

    async def test_anthropic_tool_call_input_is_dict(self) -> None:
        """Anthropic 流式 ToolCall：input_json_delta 累积后解析为 dict。"""
        events_data = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "claude-3",
                        "stop_reason": None,
                        "usage": {"input_tokens": 5, "output_tokens": 0},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "edit_file",
                        "input": {},
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path": '},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '"/tmp/f.txt", "content": "hello"}',
                    },
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 10},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        with respx.mock:
            respx.post(_ANTHROPIC_URL).mock(
                return_value=_sse_response(_anthropic_sse(events_data))
            )
            provider = AnthropicProvider(_anthropic_config())
            events = await _collect(
                provider.chat([Message(role="user", content="编辑文件")])
            )
        tool_calls = [e for e in events if isinstance(e, ToolCall)]
        assert len(tool_calls) == 1
        # Anthropic 的 input 直接是 dict
        assert tool_calls[0].arguments == {"path": "/tmp/f.txt", "content": "hello"}
        assert isinstance(tool_calls[0].arguments, dict)

    async def test_tool_call_parsing_difference_side_by_side(self) -> None:
        """对比两种 Provider 对相同工具调用的解析结果一致性。

        虽然 wire 格式不同（OpenAI: JSON 字符串, Anthropic: dict），
        但最终解析出的 ToolCall.arguments 应为相同的 dict。
        """
        # OpenAI 侧：arguments 为 JSON 字符串
        openai_chunks = [
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"file": "a.py"}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        # Anthropic 侧：input_json_delta 累积
        anthropic_events = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "claude-3",
                        "stop_reason": None,
                        "usage": {"input_tokens": 5, "output_tokens": 0},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read",
                        "input": {},
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"file": "a.py"}'},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 5},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]

        with respx.mock:
            respx.post(_DEEPSEEK_URL).mock(
                return_value=_sse_response(_openai_sse(openai_chunks))
            )
            openai_provider = OpenAICompatProvider(_provider_config())
            openai_events = await _collect(
                openai_provider.chat([Message(role="user", content="读文件")])
            )

        with respx.mock:
            respx.post(_ANTHROPIC_URL).mock(
                return_value=_sse_response(_anthropic_sse(anthropic_events))
            )
            anthropic_provider = AnthropicProvider(_anthropic_config())
            anthropic_events = await _collect(
                anthropic_provider.chat([Message(role="user", content="读文件")])
            )

        openai_tc = [e for e in openai_events if isinstance(e, ToolCall)][0]
        anthropic_tc = [e for e in anthropic_events if isinstance(e, ToolCall)][0]

        # 两种 Provider 解析出的 arguments 应一致
        assert openai_tc.arguments == anthropic_tc.arguments
        assert openai_tc.arguments == {"file": "a.py"}
        # name 也应一致
        assert openai_tc.name == anthropic_tc.name == "read"


# ============================================================================
# TestLoopDetector：循环检测器测试
# ============================================================================


class TestLoopDetector:
    """LoopDetector 循环检测器测试。"""

    def test_no_loop_with_few_calls(self) -> None:
        """调用次数不足时不检测到循环。"""
        from codepilot.agent.loop import LoopDetector

        detector = LoopDetector()
        assert detector.record_call("read_file", {"path": "a.py"}) is False
        assert detector.record_call("read_file", {"path": "b.py"}) is False

    def test_detect_loop_with_repeated_calls(self) -> None:
        """连续 3 次相同调用检测到循环。"""
        from codepilot.agent.loop import LoopDetector

        detector = LoopDetector()
        args = {"path": "same.py", "content": "hello"}
        assert detector.record_call("edit_file", args) is False
        assert detector.record_call("edit_file", args) is False
        assert detector.record_call("edit_file", args) is True

    def test_no_loop_with_different_args(self) -> None:
        """相同工具但不同参数不触发循环。"""
        from codepilot.agent.loop import LoopDetector

        detector = LoopDetector()
        assert detector.record_call("read_file", {"path": "a.py"}) is False
        assert detector.record_call("read_file", {"path": "b.py"}) is False
        assert detector.record_call("read_file", {"path": "c.py"}) is False

    def test_reset_clears_state(self) -> None:
        """reset 后重新开始检测。"""
        from codepilot.agent.loop import LoopDetector

        detector = LoopDetector()
        args = {"path": "same.py"}
        detector.record_call("read_file", args)
        detector.record_call("read_file", args)
        detector.reset()
        # 重置后不应检测到循环
        assert detector.record_call("read_file", args) is False


# ============================================================================
# TestJsonRepair：JSON 修复测试
# ============================================================================


class TestJsonRepair:
    """_repair_json 函数测试。"""

    def test_repair_trailing_comma_in_object(self) -> None:
        """修复对象中的尾随逗号。"""
        from codepilot.providers.openai_compat import _repair_json

        result = _repair_json('{"key": "value",}')
        assert json.loads(result) == {"key": "value"}

    def test_repair_trailing_comma_in_array(self) -> None:
        """修复数组中的尾随逗号。"""
        from codepilot.providers.openai_compat import _repair_json

        result = _repair_json('["a", "b",]')
        assert json.loads(result) == ["a", "b"]

    def test_repair_unclosed_braces(self) -> None:
        """补全未闭合的大括号。"""
        from codepilot.providers.openai_compat import _repair_json

        result = _repair_json('{"key": "value"')
        assert json.loads(result) == {"key": "value"}

    def test_repair_unclosed_brackets(self) -> None:
        """补全未闭合的方括号。"""
        from codepilot.providers.openai_compat import _repair_json

        result = _repair_json('["a", "b"')
        assert json.loads(result) == ["a", "b"]

    def test_repair_multiple_issues(self) -> None:
        """同时修复尾随逗号和未闭合括号。"""
        from codepilot.providers.openai_compat import _repair_json

        result = _repair_json('{"key": ["a",], "b": 1,')
        parsed = json.loads(result)
        assert parsed == {"key": ["a"], "b": 1}

    def test_anthropic_repair_json_same_logic(self) -> None:
        """Anthropic 的 _repair_json 与 OpenAI 逻辑一致。"""
        from codepilot.providers.anthropic import _repair_json as anthropic_repair
        from codepilot.providers.openai_compat import _repair_json as openai_repair

        test_input = '{"key": "value",'
        assert anthropic_repair(test_input) == openai_repair(test_input)


# ============================================================================
# TestAnthropicSpecialHandling：Anthropic 特殊处理测试
# ============================================================================


class TestAnthropicSpecialHandling:
    """Anthropic Provider 特殊处理测试。"""

    def test_tool_result_is_error_on_error_prefix(self) -> None:
        """工具结果以 Error 开头时设置 is_error=True。"""
        provider = AnthropicProvider(_anthropic_config())
        result = provider.format_tool_result(
            role="user",
            tool_call_id="toolu_1",
            content="Error: file not found",
        )
        block = result["content"][0]
        assert block["is_error"] is True

    def test_tool_result_no_is_error_on_success(self) -> None:
        """工具结果不以 Error 开头时不设置 is_error。"""
        provider = AnthropicProvider(_anthropic_config())
        result = provider.format_tool_result(
            role="user",
            tool_call_id="toolu_1",
            content="文件内容读取成功",
        )
        block = result["content"][0]
        assert "is_error" not in block

    def test_thinking_blocks_preserved_in_convert_messages(self) -> None:
        """assistant 消息中的 thinking blocks 被原样保留。"""
        provider = AnthropicProvider(_anthropic_config())
        # 模拟含 thinking block 的 assistant 消息
        content_with_thinking = [
            {"type": "thinking", "thinking": "让我分析一下..."},
            {"type": "text", "text": "这是回复"},
        ]
        messages = [
            Message(role="assistant", content=content_with_thinking),
        ]
        result = provider._convert_messages(messages)
        # thinking block 应被原样保留
        assert result[0]["role"] == "assistant"
        assert isinstance(result[0]["content"], list)
        assert len(result[0]["content"]) == 2
        assert result[0]["content"][0]["type"] == "thinking"
        assert result[0]["content"][0]["thinking"] == "让我分析一下..."
        assert result[0]["content"][1]["type"] == "text"

    def test_thinking_blocks_with_tool_use_preserved(self) -> None:
        """assistant 消息中 thinking + tool_use blocks 被完整保留。"""
        provider = AnthropicProvider(_anthropic_config())
        content_with_thinking = [
            {"type": "thinking", "thinking": "需要读取文件..."},
            {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}},
        ]
        messages = [
            Message(role="assistant", content=content_with_thinking),
        ]
        result = provider._convert_messages(messages)
        assert len(result[0]["content"]) == 2
        assert result[0]["content"][0]["type"] == "thinking"
        assert result[0]["content"][1]["type"] == "tool_use"


# ============================================================================
# TestFinishReasonLength：finish_reason="length" 截断提示测试
# ============================================================================


class TestFinishReasonLength:
    """finish_reason='length' 截断提示测试。"""

    async def test_openai_streaming_length_truncation(self) -> None:
        """OpenAI 流式响应 finish_reason=length 时添加截断提示。"""
        chunks = [
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"content": "部分回复"}, "finish_reason": None}
                ],
            },
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
            },
        ]
        with respx.mock:
            respx.post(_DEEPSEEK_URL).mock(
                return_value=_sse_response(_openai_sse(chunks))
            )
            provider = OpenAICompatProvider(_provider_config())
            events = await _collect(provider.chat([Message(role="user", content="hi")]))
        # 应有 TextDelta(部分回复) + TextDelta(截断提示) + Done
        text_deltas = [e for e in events if isinstance(e, TextDelta)]
        assert len(text_deltas) == 2
        assert text_deltas[0].text == "部分回复"
        assert "truncated" in text_deltas[1].text
        done_events = [e for e in events if isinstance(e, Done)]
        assert len(done_events) == 1
        assert done_events[0].stop_reason == "length"

    async def test_openai_non_stream_length_truncation(self) -> None:
        """OpenAI 非流式响应 finish_reason=length 时添加截断提示。"""
        resp = {
            "id": "c1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "部分回复"},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        with respx.mock:
            respx.post(_DEEPSEEK_URL).mock(return_value=_json_response(resp))
            provider = OpenAICompatProvider(_provider_config())
            events = await _collect(
                provider.chat([Message(role="user", content="hi")], stream=False)
            )
        text_deltas = [e for e in events if isinstance(e, TextDelta)]
        assert len(text_deltas) == 2
        assert text_deltas[0].text == "部分回复"
        assert "truncated" in text_deltas[1].text
        done_events = [e for e in events if isinstance(e, Done)]
        assert done_events[0].stop_reason == "length"

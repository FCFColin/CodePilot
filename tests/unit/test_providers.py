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

from codepilot.config import AnthropicConfig, DeepSeekConfig
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
from codepilot.providers.deepseek import DeepSeekProvider

# ============================================================================
# 常量与辅助函数
# ============================================================================

_DEEPSEEK_URL = "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2/chat/completions"
_ANTHROPIC_URL = "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic/v1/messages"


def _deepseek_config() -> DeepSeekConfig:
    """构造测试用 DeepSeek 配置。"""
    return DeepSeekConfig(api_key=SecretStr("sk-test-deepseek"))


def _anthropic_config() -> AnthropicConfig:
    """构造测试用 Anthropic 配置。"""
    return AnthropicConfig(api_key=SecretStr("sk-test-anthropic"))


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


# ============================================================================
# TestFormatToolResult
# ============================================================================


class TestFormatToolResult:
    """format_tool_result 格式正确性测试。"""

    def test_deepseek_format_tool_result(self) -> None:
        """DeepSeek 工具结果为 OpenAI tool 消息格式。"""
        provider = DeepSeekProvider(_deepseek_config())
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
        provider = DeepSeekProvider(_deepseek_config())
        result = provider.format_assistant_message("你好", [])
        assert result["role"] == "assistant"
        assert result["content"] == "你好"
        assert "tool_calls" not in result

    def test_deepseek_with_tool_calls(self) -> None:
        """DeepSeek 有工具调用时包含 tool_calls 列表。"""
        provider = DeepSeekProvider(_deepseek_config())
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
        provider = DeepSeekProvider(_deepseek_config())
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
        provider = DeepSeekProvider(_deepseek_config())
        # 模拟 format_assistant_message 输出存为 content
        inner = {"role": "assistant", "content": "回复", "tool_calls": []}
        messages = [Message(role="assistant", content=inner)]
        result = provider._convert_messages(messages)
        assert result[0] == inner

    def test_deepseek_convert_raw_dict(self) -> None:
        """DeepSeek 原始 dict 透传，嵌套 dict content 被提取。"""
        provider = DeepSeekProvider(_deepseek_config())
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
            provider = DeepSeekProvider(_deepseek_config())
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
            provider = DeepSeekProvider(_deepseek_config())
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
            provider = DeepSeekProvider(_deepseek_config())
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
            provider = DeepSeekProvider(_deepseek_config())
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
            provider = DeepSeekProvider(_deepseek_config())
            with pytest.raises(ProviderError, match="DeepSeek"):
                await _collect(provider.chat([Message(role="user", content="hi")]))

    async def test_deepseek_stream_options_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stream_options 不被支持时回退移除该参数并重试成功。"""

        provider = DeepSeekProvider(_deepseek_config())

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

        provider = DeepSeekProvider(_deepseek_config())

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

        with pytest.raises(ProviderError, match="DeepSeek"):
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

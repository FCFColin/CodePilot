"""上下文管理单元测试。

覆盖：
- TokenCounter：tiktoken 精确计数、回退估算、LRU 缓存、各种 content 类型
- ContextManager：消息添加、压缩触发、强制压缩、上下文格式、清空、并发、统计
- ContextCompressor：truncate/summary/hybrid 策略、历史文件写入
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from codepilot.config import ContextConfig
from codepilot.context.compressor import ContextCompressor
from codepilot.context.manager import ContextManager
from codepilot.context.token_counter import TokenCounter
from codepilot.providers.base import (
    AgentEvent,
    BaseProvider,
    Done,
    Message,
    TextDelta,
)

# ============================================================================
# 辅助类与函数
# ============================================================================


class MockProvider(BaseProvider):
    """测试用 mock provider，返回预设响应。"""

    def __init__(self, response: str = "压缩摘要") -> None:
        self.response = response
        self.call_count = 0

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        stream: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        self.call_count += 1
        yield TextDelta(text=self.response)
        yield Done(stop_reason="end_turn")

    def format_tool_result(
        self,
        role: str,
        tool_call_id: str,
        content: str,
    ) -> dict[str, Any]:
        return {"role": role, "tool_call_id": tool_call_id, "content": content}

    def format_assistant_message(
        self,
        text: str,
        tool_calls: list[Any],
    ) -> dict[str, Any]:
        return {"role": "assistant", "content": text}


def _make_config(
    max_tokens: int = 1000,
    compression_threshold: float = 0.70,
    critical_threshold: float = 0.85,
    preserve_recent_turns: int = 2,
) -> ContextConfig:
    """构造测试用 ContextConfig。"""
    return ContextConfig(
        max_tokens=max_tokens,
        compression_threshold=compression_threshold,
        critical_threshold=critical_threshold,
        preserve_recent_turns=preserve_recent_turns,
    )


async def _add_turns(manager: ContextManager, n_turns: int) -> None:
    """向 manager 添加 n 轮对话（user + assistant）。"""
    for i in range(n_turns):
        await manager.add_message("user", f"用户消息 {i} " * 20)
        await manager.add_message("assistant", f"助手回复 {i} " * 20)


# ============================================================================
# TestTokenCounter
# ============================================================================


class TestTokenCounter:
    """TokenCounter 测试。"""

    def test_tiktoken_exact_count(self) -> None:
        """tiktoken 可用时精确计数。"""
        counter = TokenCounter()
        # tiktoken 应该已安装
        assert counter._encoder is not None
        # "hello world" 的 tiktoken 计数应为 2
        count = counter.count_text("hello world")
        assert count == 2

    def test_fallback_estimation(self) -> None:
        """tiktoken 不可用时回退到字符数估算。"""
        counter = TokenCounter()
        # 模拟 tiktoken 不可用
        counter._encoder = None
        text = "This is a sample English text for testing."
        estimated = counter.count_text(text)
        # 回退估算应返回正整数
        assert isinstance(estimated, int)
        assert estimated > 0

    def test_fallback_error_within_30_percent(self) -> None:
        """回退模式误差不超过 30%。"""
        # 用普通英文短词文本（3.5 字符/token 系数对此类文本较准确）
        text = (
            "the cat sat on the mat and the dog ran to the park. "
            "The quick brown fox jumps over the lazy dog. "
            "It is a good day for testing."
        )
        precise_counter = TokenCounter()
        assert precise_counter._encoder is not None
        precise = precise_counter.count_text(text)

        fallback_counter = TokenCounter()
        fallback_counter._encoder = None
        estimated = fallback_counter.count_text(text)

        # 误差不超过 30%
        error_ratio = abs(estimated - precise) / precise
        assert error_ratio <= 0.30, (
            f"回退误差 {error_ratio:.2%} 超过 30%（精确={precise}, 估算={estimated}）"
        )

    def test_cache_hit_rate(self) -> None:
        """相同文本第二次计数应命中缓存。"""
        counter = TokenCounter()
        text = "缓存测试文本内容 " * 10
        count1 = counter.count_text(text)
        # 验证缓存已写入
        assert len(counter._cache) == 1
        # 再次计数应命中缓存
        count2 = counter.count_text(text)
        assert count1 == count2
        # 缓存大小不变
        assert len(counter._cache) == 1

    def test_string_content(self) -> None:
        """count_tokens 支持 str 内容。"""
        counter = TokenCounter()
        count = counter.count_tokens("你好世界 hello")
        # 应与 count_text 结果一致
        assert count == counter.count_text("你好世界 hello")
        assert count > 0

    def test_list_content(self) -> None:
        """count_tokens 支持 list 内容（content blocks）。"""
        counter = TokenCounter()
        content: list[Any] = [
            {"type": "text", "text": "第一段文本"},
            {"type": "text", "text": "第二段文本"},
        ]
        count = counter.count_tokens(content)
        assert count > 0
        # 应大于单段文本的计数
        single_count = counter.count_tokens([{"type": "text", "text": "第一段文本"}])
        assert count > single_count

    def test_dict_content(self) -> None:
        """count_tokens 支持 dict 内容（消息）。"""
        counter = TokenCounter()
        content: dict[str, Any] = {
            "role": "user",
            "content": "测试 dict 消息",
        }
        count = counter.count_tokens(content)
        assert count > 0
        # 应与 count_message 结果一致
        assert count == counter.count_message(content)

    def test_empty_text(self) -> None:
        """空文本计数为 0。"""
        counter = TokenCounter()
        assert counter.count_text("") == 0

    def test_count_messages_list(self) -> None:
        """消息列表总计数等于各消息计数之和。"""
        counter = TokenCounter()
        messages: list[Message] = [
            Message(role="user", content="你好"),
            Message(role="assistant", content="你好，有什么可以帮你？"),
        ]
        total = counter.count_messages(messages)
        expected = sum(counter.count_message(m) for m in messages)
        assert total == expected


# ============================================================================
# TestContextManager
# ============================================================================


class TestContextManager:
    """ContextManager 测试。"""

    async def test_token_count_after_add(self) -> None:
        """添加消息后 total_tokens 应更新。"""
        counter = TokenCounter()
        config = _make_config()
        manager = ContextManager(config, counter, system_prompt="系统提示")
        initial_tokens = manager.total_tokens
        await manager.add_message("user", "用户消息内容")
        assert manager.total_tokens > initial_tokens
        assert len(manager.messages) == 1

    async def test_maybe_compress_triggers_threshold(self) -> None:
        """达到压缩阈值时触发压缩。"""
        counter = TokenCounter()
        # 设置较小的 max_tokens 以便快速达到阈值
        config = _make_config(
            max_tokens=100,
            compression_threshold=0.5,
            critical_threshold=0.9,
            preserve_recent_turns=1,
        )
        manager = ContextManager(config, counter)
        # 添加足够多的消息以触发压缩
        await _add_turns(manager, 5)
        # 手动触发压缩检查
        stats = await manager.maybe_compress()
        assert stats is not None
        assert stats["messages_compressed"] > 0
        # 压缩次数应递增
        assert manager.get_stats()["compression_count"] >= 1

    async def test_force_compress_without_provider_fallback_truncate(self) -> None:
        """compressor 为 None 时强制压缩使用 truncate 回退。"""
        counter = TokenCounter()
        config = _make_config(preserve_recent_turns=1)
        manager = ContextManager(config, counter, compressor=None)
        await _add_turns(manager, 3)
        stats = await manager.force_compress()
        assert stats["strategy"] == "truncate"
        assert stats["messages_compressed"] > 0
        # 压缩后应有 summary
        assert manager.compressed_summary != ""
        # 压缩次数应递增
        assert manager.get_stats()["compression_count"] == 1

    async def test_get_context_format(self) -> None:
        """system prompt 在首位。"""
        counter = TokenCounter()
        config = _make_config()
        manager = ContextManager(config, counter, system_prompt="系统提示")
        await manager.add_message("user", "用户消息")
        context = await manager.get_context()
        # 第一条应为 system
        assert context[0]["role"] == "system"
        assert context[0]["content"] == "系统提示"
        # 第二条应为 user
        assert context[1]["role"] == "user"

    async def test_clear_preserves_system_prompt(self) -> None:
        """clear 后 system_prompt 保留，消息清空。"""
        counter = TokenCounter()
        config = _make_config()
        manager = ContextManager(config, counter, system_prompt="系统提示")
        await manager.add_message("user", "用户消息")
        await manager.clear()
        assert len(manager.messages) == 0
        assert manager.compressed_summary == ""
        # system_prompt 保留
        assert manager.system_prompt == "系统提示"
        # total_tokens 应只含 system_prompt
        assert manager.total_tokens == counter.count_text("系统提示")

    async def test_concurrent_add_thread_safety(self) -> None:
        """并发添加消息。"""
        counter = TokenCounter()
        config = _make_config(max_tokens=100000)
        manager = ContextManager(config, counter)
        # 并发添加 10 条消息
        tasks = [manager.add_message("user", f"消息 {i}") for i in range(10)]
        await asyncio.gather(*tasks)
        assert len(manager.messages) == 10

    async def test_get_stats_returns_typeddict(self) -> None:
        """get_stats 返回 ContextStats TypedDict。"""
        counter = TokenCounter()
        config = _make_config(max_tokens=1000)
        manager = ContextManager(config, counter)
        await manager.add_message("user", "测试消息")
        stats = manager.get_stats()
        # 验证 TypedDict 所有必需字段存在且类型正确
        assert isinstance(stats["total_tokens"], int)
        assert isinstance(stats["max_tokens"], int)
        assert isinstance(stats["utilization"], float)
        assert isinstance(stats["message_count"], int)
        assert isinstance(stats["compression_count"], int)
        # 验证值
        assert stats["total_tokens"] > 0
        assert stats["max_tokens"] == 1000
        assert 0.0 <= stats["utilization"] <= 1.0
        assert stats["message_count"] == 1
        assert stats["compression_count"] == 0

    async def test_critical_threshold_forced_compress(self) -> None:
        """达到 critical_threshold 时强制压缩（保留轮数减半后恢复）。"""
        counter = TokenCounter()
        config = _make_config(
            max_tokens=100,
            compression_threshold=0.5,
            critical_threshold=0.7,
            preserve_recent_turns=4,
        )
        manager = ContextManager(config, counter)
        await _add_turns(manager, 6)
        original_preserve = config.preserve_recent_turns
        stats = await manager.maybe_compress()
        assert stats is not None
        # 强制压缩后保留轮数应恢复原值
        assert config.preserve_recent_turns == original_preserve

    async def test_update_usage_accumulates(self) -> None:
        """update_usage 累计 input/output token。"""
        counter = TokenCounter()
        config = _make_config()
        manager = ContextManager(config, counter)
        await manager.update_usage(10, 20)
        await manager.update_usage(5, 15)
        # update_usage 保留逻辑，内部状态应累计
        assert manager._usage["input"] == 15
        assert manager._usage["output"] == 35


# ============================================================================
# TestContextCompressor
# ============================================================================


class TestContextCompressor:
    """ContextCompressor 测试。"""

    async def test_truncate_preserves_recent_n_turns(self) -> None:
        """truncate 策略保留最近 N 轮对话。"""
        counter = TokenCounter()
        compressor = ContextCompressor(
            token_counter=counter,
            strategy="truncate",
            save_full_history=False,
        )
        messages: list[Message] = [
            Message(role="user", content="问题 1"),
            Message(role="assistant", content="回答 1"),
            Message(role="user", content="问题 2"),
            Message(role="assistant", content="回答 2"),
            Message(role="user", content="问题 3"),
            Message(role="assistant", content="回答 3"),
        ]
        summary, stats, summary_msg = await compressor.compress(messages, preserve_recent_turns=1)
        # 应有 4 条消息被压缩（前 2 轮）
        assert stats["messages_compressed"] == 4
        assert stats["strategy"] == "truncate"
        assert summary != ""

    async def test_summary_without_provider_fallback(self) -> None:
        """summary 无 provider 时回退到 truncate。"""
        counter = TokenCounter()
        compressor = ContextCompressor(
            provider=None,
            token_counter=counter,
            strategy="summary",
            save_full_history=False,
        )
        messages: list[Message] = [
            Message(role="user", content="问题 1"),
            Message(role="assistant", content="回答 1"),
            Message(role="user", content="问题 2"),
            Message(role="assistant", content="回答 2"),
        ]
        summary, stats, summary_msg = await compressor.compress(messages, preserve_recent_turns=1)
        # 无 provider 时回退到 truncate
        assert "truncated" in summary.lower()
        assert stats["messages_compressed"] > 0

    async def test_summary_with_mock_provider(self) -> None:
        """summary 策略使用 mock provider 生成摘要。"""
        counter = TokenCounter()
        provider = MockProvider(response="这是 LLM 生成的摘要")
        compressor = ContextCompressor(
            provider=provider,
            token_counter=counter,
            strategy="summary",
            save_full_history=False,
        )
        messages: list[Message] = [
            Message(role="user", content="问题 1"),
            Message(role="assistant", content="回答 1"),
            Message(role="user", content="问题 2"),
            Message(role="assistant", content="回答 2"),
        ]
        summary, stats, summary_msg = await compressor.compress(messages, preserve_recent_turns=1)
        assert summary == "这是 LLM 生成的摘要"
        assert stats["strategy"] == "summary"
        assert provider.call_count == 1

    async def test_hybrid_strategy(self) -> None:
        """hybrid 策略：工具输出截断 + 对话 summary/truncate。"""
        counter = TokenCounter()
        compressor = ContextCompressor(
            provider=None,
            token_counter=counter,
            strategy="hybrid",
            save_full_history=False,
        )
        messages: list[Message] = [
            Message(role="user", content="问题 1"),
            Message(role="assistant", content="Result: " + "x" * 600),
            Message(role="user", content="问题 2"),
            Message(role="assistant", content="回答 2"),
            Message(role="user", content="问题 3"),
            Message(role="assistant", content="回答 3"),
        ]
        summary, stats, summary_msg = await compressor.compress(messages, preserve_recent_turns=1)
        assert stats["strategy"] == "hybrid"
        assert "Tool output truncated" in summary

    async def test_no_compressible_messages(self) -> None:
        """无可压缩消息时返回空摘要。"""
        counter = TokenCounter()
        compressor = ContextCompressor(
            token_counter=counter,
            strategy="truncate",
            save_full_history=False,
        )
        # 仅有 1 轮对话，preserve_recent_turns=2 时全部保留
        messages: list[Message] = [
            Message(role="user", content="问题 1"),
            Message(role="assistant", content="回答 1"),
        ]
        summary, stats, summary_msg = await compressor.compress(messages, preserve_recent_turns=2)
        assert summary == ""
        assert stats["messages_compressed"] == 0

    async def test_history_file_written(self, tmp_path: Path) -> None:
        """压缩时历史写入 JSONL 文件。"""
        history_file = tmp_path / "history.jsonl"
        counter = TokenCounter()
        compressor = ContextCompressor(
            token_counter=counter,
            strategy="truncate",
            save_full_history=True,
            history_file=str(history_file),
        )
        messages: list[Message] = [
            Message(role="user", content="问题 1"),
            Message(role="assistant", content="回答 1"),
            Message(role="user", content="问题 2"),
            Message(role="assistant", content="回答 2"),
        ]
        await compressor.compress(messages, preserve_recent_turns=1)
        # 文件应存在且包含 JSONL 行
        assert history_file.exists()
        lines = history_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) > 0
        # 每行应为合法 JSON
        for line in lines:
            record = json.loads(line)
            assert "timestamp" in record
            assert "role" in record
            assert "content" in record


# ============================================================================
# TestCompressionStrategies：压缩策略专项测试
# ============================================================================


class TestCompressionStrategies:
    """压缩策略专项测试。

    覆盖：
    - summary 策略的压缩触发（token 使用量超过阈值时）
    - truncate 策略的压缩（直接丢弃最早消息）
    - preserve_recent_turns 保留最近N轮
    - force_compress 手动触发压缩
    - 压缩后 token 数减少
    """

    async def test_summary_triggers_on_threshold(self) -> None:
        """summary 策略：token 使用量超过阈值时触发压缩。"""
        counter = TokenCounter()
        provider = MockProvider(response="摘要内容")
        compressor = ContextCompressor(
            provider=provider,
            token_counter=counter,
            strategy="summary",
            save_full_history=False,
        )
        config = _make_config(
            max_tokens=100,
            compression_threshold=0.5,
            critical_threshold=0.9,
            preserve_recent_turns=1,
        )
        manager = ContextManager(config, counter, compressor=compressor)
        # 添加足够多的消息以超过阈值
        await _add_turns(manager, 5)
        # 触发压缩检查
        stats = await manager.maybe_compress()
        assert stats is not None
        assert stats["strategy"] == "summary"
        assert provider.call_count >= 1
        # 压缩后应有摘要
        assert manager.compressed_summary != ""

    async def test_truncate_discards_earliest_messages(self) -> None:
        """truncate 策略：直接丢弃最早消息，仅保留最近N轮。"""
        counter = TokenCounter()
        compressor = ContextCompressor(
            token_counter=counter,
            strategy="truncate",
            save_full_history=False,
        )
        messages: list[Message] = [
            Message(role="user", content="最早问题"),
            Message(role="assistant", content="最早回答"),
            Message(role="user", content="中间问题"),
            Message(role="assistant", content="中间回答"),
            Message(role="user", content="最近问题"),
            Message(role="assistant", content="最近回答"),
        ]
        summary, stats, summary_msg = await compressor.compress(messages, preserve_recent_turns=1)
        # 应丢弃前 2 轮（4 条消息），保留最后 1 轮
        assert stats["messages_compressed"] == 4
        assert stats["strategy"] == "truncate"
        assert "truncated" in summary.lower()
        # 最早的消息内容不应出现在摘要中
        assert "最早问题" not in summary

    async def test_preserve_recent_turns_keeps_n(self) -> None:
        """preserve_recent_turns 保留最近N轮对话。"""
        counter = TokenCounter()
        compressor = ContextCompressor(
            token_counter=counter,
            strategy="truncate",
            save_full_history=False,
        )
        # 5 轮对话
        messages: list[Message] = []
        for i in range(5):
            messages.append(Message(role="user", content=f"问题 {i}"))
            messages.append(Message(role="assistant", content=f"回答 {i}"))

        # preserve_recent_turns=3 → 保留最后 3 轮（6 条），压缩前 2 轮（4 条）
        summary, stats, summary_msg = await compressor.compress(messages, preserve_recent_turns=3)
        assert stats["messages_compressed"] == 4
        assert summary != ""

        # preserve_recent_turns=5 → 全部保留，无可压缩区
        summary2, stats2, _ = await compressor.compress(messages, preserve_recent_turns=5)
        assert stats2["messages_compressed"] == 0
        assert summary2 == ""

        # preserve_recent_turns=0 → 全部可压缩
        summary3, stats3, _ = await compressor.compress(messages, preserve_recent_turns=0)
        assert stats3["messages_compressed"] == 10

    async def test_force_compress_with_provider(self) -> None:
        """force_compress 手动触发压缩（有 provider 时使用 summary 策略）。"""
        counter = TokenCounter()
        provider = MockProvider(response="手动压缩摘要")
        compressor = ContextCompressor(
            provider=provider,
            token_counter=counter,
            strategy="summary",
            save_full_history=False,
        )
        config = _make_config(preserve_recent_turns=1)
        manager = ContextManager(config, counter, compressor=compressor)
        await _add_turns(manager, 3)
        # 手动触发压缩
        stats = await manager.force_compress()
        assert stats["messages_compressed"] > 0
        assert stats["strategy"] == "summary"
        assert manager.compressed_summary != ""
        assert manager.get_stats()["compression_count"] == 1
        # provider 应被调用
        assert provider.call_count >= 1

    async def test_token_count_decreases_after_compression(self) -> None:
        """压缩后 token 数应减少。"""
        counter = TokenCounter()
        config = _make_config(
            max_tokens=100,
            compression_threshold=0.5,
            critical_threshold=0.9,
            preserve_recent_turns=1,
        )
        manager = ContextManager(config, counter, compressor=None)
        # 添加大量消息使 token 数很高
        await _add_turns(manager, 5)
        tokens_before = manager.total_tokens
        # 触发压缩
        stats = await manager.maybe_compress()
        assert stats is not None
        tokens_after = manager.total_tokens
        # 压缩后 token 数应少于压缩前
        assert tokens_after < tokens_before
        # stats 也应反映减少
        assert stats["after_tokens"] < stats["before_tokens"]


# ============================================================================
# TestTokenCounterListContent：content 为 list 时的 token 计数测试
# ============================================================================


class TestTokenCounterListContent:
    """content 为 list（content blocks）时的 token 计数测试。"""

    def test_text_block_counting(self) -> None:
        """type="text" block：计算 text 字段的 token。"""
        counter = TokenCounter()
        msg: dict = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Hello world"},
            ],
        }
        count = counter.count_message(msg)
        assert count > 0
        # 应大于纯 role 开销
        assert count > 4

    def test_tool_use_block_counting(self) -> None:
        """type="tool_use" block：计算 name + input 的 token。"""
        counter = TokenCounter()
        msg: dict = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "read_file", "input": {"path": "/tmp/a.py"}},
            ],
        }
        count = counter.count_message(msg)
        assert count > 0
        # tool_use 的 token 应大于仅含 text 的同长度消息（因为 name + input 都计算）
        text_only_msg: dict = {
            "role": "assistant",
            "content": [{"type": "text", "text": "read_file"}],
        }
        assert count > counter.count_message(text_only_msg)

    def test_tool_result_block_str_content(self) -> None:
        """type="tool_result" block：content 为 str 时计算 token。"""
        counter = TokenCounter()
        msg: dict = {
            "role": "user",
            "content": [
                {"type": "tool_result", "content": "file content here"},
            ],
        }
        count = counter.count_message(msg)
        assert count > 0

    def test_tool_result_block_list_content(self) -> None:
        """type="tool_result" block：content 为 list 时递归计算 token。"""
        counter = TokenCounter()
        msg: dict = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "content": [
                        {"type": "text", "text": "line 1 output"},
                        {"type": "text", "text": "line 2 output"},
                    ],
                },
            ],
        }
        count = counter.count_message(msg)
        assert count > 0
        # 应大于只有一行的情况
        single_msg: dict = {
            "role": "user",
            "content": [
                {"type": "tool_result", "content": [{"type": "text", "text": "line 1 output"}]},
            ],
        }
        assert count > counter.count_message(single_msg)

    def test_thinking_block_counting(self) -> None:
        """type="thinking" block：计算 thinking 字段的 token。"""
        counter = TokenCounter()
        msg: dict = {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Let me think about this problem..."},
            ],
        }
        count = counter.count_message(msg)
        assert count > 0

    def test_mixed_blocks_counting(self) -> None:
        """混合多种 content block 类型时正确计数。"""
        counter = TokenCounter()
        msg: dict = {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "思考中..."},
                {"type": "text", "text": "这是回答"},
                {"type": "tool_use", "name": "read_file", "input": {"path": "a.py"}},
            ],
        }
        count = counter.count_message(msg)
        assert count > 0
        # 应大于只含 text block 的消息
        text_only: dict = {
            "role": "assistant",
            "content": [{"type": "text", "text": "这是回答"}],
        }
        assert count > counter.count_message(text_only)

    def test_count_message_tokens_function(self) -> None:
        """模块级 count_message_tokens 函数正确处理各种 content block。"""
        from codepilot.context.token_counter import count_message_tokens

        msg: dict = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "tool_use", "name": "search", "input": {"q": "test"}},
            ],
        }
        count = count_message_tokens(msg)
        assert count > 0
        # str content 的消息也应正常工作
        str_msg: dict = {"role": "user", "content": "Hello world"}
        assert count_message_tokens(str_msg) > 0

    def test_count_messages_tokens_function(self) -> None:
        """模块级 count_messages_tokens 函数正确计算消息列表。"""
        from codepilot.context.token_counter import count_messages_tokens

        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "Hi there"}]},
        ]
        total = count_messages_tokens(messages)
        assert total > 0
        # 应等于各消息之和
        from codepilot.context.token_counter import count_message_tokens
        expected = sum(count_message_tokens(m) for m in messages)
        assert total == expected


# ============================================================================
# TestCompressorFixes：压缩器修复测试
# ============================================================================


class TestCompressorFixes:
    """压缩器修复测试：压缩标记、工具结果截断、preserve_recent_turns 按 token。"""

    async def test_compressed_summary_marker(self) -> None:
        """压缩生成的摘要消息带 _compressed: True 标记。"""
        counter = TokenCounter()
        compressor = ContextCompressor(
            token_counter=counter,
            strategy="truncate",
            save_full_history=False,
        )
        messages: list[Message] = [
            Message(role="user", content="问题 1"),
            Message(role="assistant", content="回答 1"),
            Message(role="user", content="问题 2"),
            Message(role="assistant", content="回答 2"),
        ]
        summary, stats, summary_message = await compressor.compress(
            messages, preserve_recent_turns=1
        )
        assert summary_message is not None
        assert summary_message.get("_compressed") is True
        assert "[Earlier conversation summary]" in summary_message.get("content", "")

    async def test_no_summary_no_marker(self) -> None:
        """无压缩摘要时 summary_message 为 None。"""
        counter = TokenCounter()
        compressor = ContextCompressor(
            token_counter=counter,
            strategy="truncate",
            save_full_history=False,
        )
        messages: list[Message] = [
            Message(role="user", content="问题 1"),
            Message(role="assistant", content="回答 1"),
        ]
        summary, stats, summary_message = await compressor.compress(
            messages, preserve_recent_turns=5
        )
        assert summary == ""
        assert summary_message is None

    async def test_truncate_large_tool_results(self) -> None:
        """_truncate_large_tool_results 截断超大工具结果。"""
        counter = TokenCounter()
        compressor = ContextCompressor(
            token_counter=counter,
            strategy="hybrid",
            save_full_history=False,
        )
        # 构造超大工具结果消息（>2000*4=8000 字符，>30 行）
        large_content = "\n".join(f"line {i}: " + "x" * 200 for i in range(50))
        messages: list[Message] = [
            Message(role="user", content="问题 1"),
            Message(role="assistant", content="回答 1"),
            Message(role="user", content="问题 2"),
            Message(role="assistant", content="回答 2"),
        ]
        # 直接测试 _truncate_large_tool_results 方法
        tool_msg: dict = {"role": "tool", "content": large_content}
        result = compressor._truncate_large_tool_results([tool_msg])
        assert "lines truncated" in result[0]["content"]
        # 前后内容应保留
        assert "line 0:" in result[0]["content"]
        assert "line 49:" in result[0]["content"]

    async def test_preserve_recent_turns_by_token(self) -> None:
        """preserve_recent_turns 按保留区至少占 10% token 扩展。"""
        counter = TokenCounter()
        compressor = ContextCompressor(
            token_counter=counter,
            strategy="truncate",
            save_full_history=False,
        )
        # 构造消息：前面消息很长，最后几条很短
        messages: list[Message] = []
        # 前面 4 轮，每轮内容很长
        for i in range(4):
            messages.append(Message(role="user", content=f"长问题 {i} " * 100))
            messages.append(Message(role="assistant", content=f"长回答 {i} " * 100))
        # 最后 2 轮，内容很短
        messages.append(Message(role="user", content="短问题"))
        messages.append(Message(role="assistant", content="短回答"))
        messages.append(Message(role="user", content="短问题2"))
        messages.append(Message(role="assistant", content="短回答2"))

        total_tokens = counter.count_messages(messages)
        compressible, preserved = compressor._split_messages(messages, preserve_recent_turns=1)
        # 保留区 token 应至少占总 token 的 10%
        preserved_tokens = counter.count_messages(preserved)
        assert preserved_tokens >= total_tokens * 0.10


# ============================================================================
# TestLayeredCompressor：分层压缩测试
# ============================================================================


class TestLayeredCompressor:
    """LayeredCompressor 分层压缩测试。"""

    async def test_compress_below_threshold_no_op(self) -> None:
        """使用率低于 Layer 0 阈值（40%）时不压缩。"""
        from codepilot.context.layered_compressor import LayeredCompressor

        compressor = LayeredCompressor()
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，有什么可以帮你？"},
        ]
        max_tokens = 10000
        current_tokens = 50  # 0.5% 使用率，远低于 40%

        result, stats = await compressor.compress(
            messages=messages,
            system_prompt="系统提示",
            current_tokens=current_tokens,
            max_tokens=max_tokens,
        )

        # 不应触发任何压缩层
        assert stats.layers_applied == []
        assert stats.before_tokens == current_tokens
        assert stats.after_tokens == current_tokens
        # 消息应原样返回
        assert result == messages

    async def test_layer0_observation_masking(self) -> None:
        """Layer 0：对大型工具输出做 observation masking。"""
        from codepilot.context.layered_compressor import LayeredCompressor

        compressor = LayeredCompressor()
        # 构造大型工具输出（>MAX_SINGLE_RESULT_CHARS 且行数足够多）
        large_tool_content = "\n".join(f"line {i}: " + "x" * 200 for i in range(100))
        messages = [
            {"role": "user", "content": "请运行命令"},
            {"role": "assistant", "content": "正在运行"},
            {"role": "tool", "content": large_tool_content},
            {"role": "assistant", "content": "命令已完成"},
        ]
        max_tokens = 10000
        # 设置使用率为 50%（>= 40% 触发 Layer 0）
        current_tokens = int(max_tokens * 0.50)

        result, stats = await compressor.compress(
            messages=messages,
            system_prompt="",
            current_tokens=current_tokens,
            max_tokens=max_tokens,
        )

        # 应触发 Layer 0
        assert "observation_masking" in stats.layers_applied
        # 工具输出应被折叠（包含 "lines folded" 标记）
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "lines folded" in tool_msgs[0]["content"]
        # 非 tool 消息应保持不变
        non_tool_contents = [m for m in result if m.get("role") != "tool"]
        assert any("命令已完成" in m.get("content", "") for m in non_tool_contents)

    async def test_layer1_tool_result_truncation(self) -> None:
        """Layer 1：对超大工具输出进行截断。"""
        from codepilot.context.layered_compressor import LayeredCompressor

        compressor = LayeredCompressor()
        # 直接测试 _truncate_tool_results 方法
        huge_tool_content = "\n".join(f"line {i}: " + "y" * 400 for i in range(60))
        messages = [
            {"role": "user", "content": "请运行命令"},
            {"role": "tool", "content": huge_tool_content},
            {"role": "assistant", "content": "命令已完成"},
        ]

        result = compressor._truncate_tool_results(messages)

        # 工具输出应被截断（包含 "lines truncated" 标记）
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "lines truncated" in tool_msgs[0]["content"]
        # 截断后内容应比原始短
        assert len(tool_msgs[0]["content"]) < len(huge_tool_content)
        # 非 tool 消息应保持不变
        non_tool = [m for m in result if m.get("role") != "tool"]
        assert len(non_tool) == 2

    async def test_layer3_force_truncate(self) -> None:
        """Layer 3：强制截断丢弃最早消息。"""
        from codepilot.context.layered_compressor import LayeredCompressor

        compressor = LayeredCompressor()
        # 构造大量消息
        messages = []
        for i in range(50):
            messages.append({"role": "user", "content": f"用户消息 {i} " * 50})
            messages.append({"role": "assistant", "content": f"助手回复 {i} " * 50})

        max_tokens = 10000
        # 设置使用率为 92%（>= 88% 触发 Layer 3）
        current_tokens = int(max_tokens * 0.92)

        result, stats = await compressor.compress(
            messages=messages,
            system_prompt="",
            current_tokens=current_tokens,
            max_tokens=max_tokens,
        )

        # 应触发 force_truncation
        assert "force_truncation" in stats.layers_applied
        # 结果消息数应少于原始消息数
        assert len(result) < len(messages)
        # 最早的消息应被丢弃
        assert "用户消息 0" not in str(result)

    async def test_preserve_tail_ratio(self) -> None:
        """最后 10% 的消息（保留区）应逐字保留。"""
        from codepilot.context.layered_compressor import LayeredCompressor

        compressor = LayeredCompressor()
        # 构造消息：前面有大量工具输出，最后几条是重要对话
        messages = []
        for i in range(20):
            messages.append({"role": "user", "content": f"问题 {i}"})
            messages.append({"role": "tool", "content": "x" * 500})
            messages.append({"role": "assistant", "content": f"回答 {i}"})

        # 最后几条重要消息
        messages.append({"role": "user", "content": "最终重要问题"})
        messages.append({"role": "assistant", "content": "最终重要回答"})

        max_tokens = 10000
        current_tokens = int(max_tokens * 0.50)

        result, stats = await compressor.compress(
            messages=messages,
            system_prompt="",
            current_tokens=current_tokens,
            max_tokens=max_tokens,
        )

        # 保留区的消息应逐字保留
        result_contents = [m.get("content", "") for m in result]
        # 最终重要消息应在结果中
        assert any("最终重要问题" in c for c in result_contents)
        assert any("最终重要回答" in c for c in result_contents)

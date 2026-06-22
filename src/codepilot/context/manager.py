"""上下文管理器。

维护对话历史、累计 token 用量，并在达到压缩阈值时触发压缩。
线程安全（asyncio.Lock），所有写操作都通过锁串行化。
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

import structlog

from codepilot.config import ContextConfig
from codepilot.context.compressor import CompressionStats, ContextCompressor
from codepilot.context.layered_compressor import LayeredCompressor
from codepilot.context.token_counter import TokenCounter
from codepilot.providers.base import Message

logger = structlog.get_logger(__name__)


class ContextStats(TypedDict):
    """上下文统计信息。"""

    total_tokens: int
    max_tokens: int
    utilization: float
    message_count: int
    compression_count: int


class ContextManager:
    """上下文管理器。

    职责：
    - 维护对话消息历史（self.messages）
    - 跟踪当前上下文总 token 数（self.total_tokens）
    - 在达到压缩阈值时触发压缩（maybe_compress）
    - 提供统计信息（get_stats）
    - 累计 input/output token 用量（来自 provider 的 Usage 事件）

    线程安全：所有写操作通过 asyncio.Lock 串行化。
    """

    def __init__(
        self,
        config: ContextConfig,
        token_counter: TokenCounter,
        compressor: ContextCompressor | None = None,
        system_prompt: str = "",
    ) -> None:
        self.config = config
        self.token_counter = token_counter
        self.compressor = compressor
        self.system_prompt = system_prompt
        # 当前对话历史（不含 system_prompt 和 compressed_summary）
        self.messages: list[Message] = []
        # 压缩后的历史摘要（如有）
        self.compressed_summary: str = ""
        # 当前上下文总 token（system_prompt + summary + messages）
        self.total_tokens: int = 0
        # asyncio 锁，保证写操作串行化
        self._lock = asyncio.Lock()
        # 累计 input/output token 用量（来自 provider Usage 事件）
        self._usage: dict[str, int] = {"input": 0, "output": 0}
        # 压缩次数累计
        self._compression_count: int = 0
        # 分层压缩器
        self._layered_compressor = LayeredCompressor()

        # 初始化时计算 system_prompt 的 token
        self._recalculate_tokens()
        logger.debug(
            "上下文管理器已初始化",
            max_tokens=config.max_tokens,
            initial_tokens=self.total_tokens,
        )

    # ------------------------------------------------------------------
    # 消息添加
    # ------------------------------------------------------------------

    async def add_message(self, role: str, content: Any) -> None:
        """添加消息并更新 total_tokens。"""
        message = Message(role=role, content=content)
        await self.add_message_obj(message)

    async def add_message_obj(self, message: Message) -> None:
        """添加 Message 对象并更新 total_tokens。"""
        async with self._lock:
            self.messages.append(message)
            self.total_tokens += self.token_counter.count_message(message)
            logger.debug(
                "消息已添加",
                role=message.role,
                total_tokens=self.total_tokens,
                message_count=len(self.messages),
            )

    # ------------------------------------------------------------------
    # 上下文获取
    # ------------------------------------------------------------------

    async def get_context(self) -> list[dict[str, Any]]:
        """获取当前上下文（可能触发压缩）。

        返回格式：
        [system_prompt_message?] + [compressed_summary_message?] + messages

        - 若 preserve_system_prompt 为 True 且 system_prompt 非空，
          system_prompt 作为第一条 system 消息。
        - 若有 compressed_summary，作为 assistant 消息插入。
        """
        # 先检查是否需要压缩
        await self.maybe_compress()

        async with self._lock:
            context: list[dict[str, Any]] = []
            if self.config.preserve_system_prompt and self.system_prompt:
                context.append({"role": "system", "content": self.system_prompt})
            if self.compressed_summary:
                context.append(
                    {
                        "role": "assistant",
                        "content": (
                            "[Previous conversation summary]\n"
                            + self.compressed_summary
                        ),
                    }
                )
            context.extend(
                [{"role": m.role, "content": m.content} for m in self.messages]
            )
            return context

    # ------------------------------------------------------------------
    # 压缩
    # ------------------------------------------------------------------

    async def force_compress(self) -> CompressionStats:
        """手动触发压缩，返回压缩统计。"""
        async with self._lock:
            return await self._do_compress()

    async def maybe_compress(self) -> CompressionStats | None:
        """检查是否需要压缩，需要则触发。

        触发条件：
        - total_tokens / max_tokens >= compression_threshold（0.70）：正常压缩
        - total_tokens / max_tokens >= critical_threshold（0.85）：强制压缩
          （减少保留轮数以释放更多空间）
        返回压缩统计或 None（未触发）。
        """
        async with self._lock:
            if self.config.max_tokens <= 0:
                return None
            usage_ratio = self.total_tokens / self.config.max_tokens

            # 未达到正常压缩阈值：不压缩
            if usage_ratio < self.config.compression_threshold:
                return None

            # 达到强制压缩阈值：减少保留轮数以释放更多空间
            if usage_ratio >= self.config.critical_threshold:
                original_preserve = self.config.preserve_recent_turns
                # 强制压缩时保留轮数减半（至少 1 轮）
                self.config.preserve_recent_turns = max(1, original_preserve // 2)
                logger.info(
                    "触发强制压缩",
                    usage_ratio=usage_ratio,
                    original_preserve=original_preserve,
                    reduced_preserve=self.config.preserve_recent_turns,
                )
                try:
                    return await self._do_compress()
                finally:
                    # 恢复原保留轮数配置
                    self.config.preserve_recent_turns = original_preserve

            # 正常压缩
            logger.info("触发正常压缩", usage_ratio=usage_ratio)
            return await self._do_compress()

    async def _do_compress(self) -> CompressionStats:
        """实际执行压缩（调用方已持有锁）。

        - 配置启用分层压缩时使用 LayeredCompressor
        - compressor 可用时调用 compressor.compress
        - compressor 为 None 时用 truncate 策略（内联实现）
        压缩后：将可压缩区消息替换为 summary，更新 total_tokens。
        """
        if not self.messages:
            return CompressionStats(
                before_tokens=self.total_tokens,
                after_tokens=self.total_tokens,
                messages_compressed=0,
                strategy="none",
            )

        before_tokens = self.total_tokens

        # 分层压缩路径
        if self.config.use_layered_compression:
            stats = await self._do_layered_compress(before_tokens)
            self._compression_count += 1
            return stats

        # 分区：可压缩区 + 保留区
        compressible, preserved = self._split_messages(
            self.messages, self.config.preserve_recent_turns
        )

        if not compressible:
            # 无可压缩内容
            return CompressionStats(
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                messages_compressed=0,
                strategy="none",
            )

        # 执行压缩
        if self.compressor is not None:
            summary, stats, summary_message = await self.compressor.compress(
                self.messages,
                preserve_recent_turns=self.config.preserve_recent_turns,
                max_tokens=self.config.max_tokens,
            )
        else:
            # compressor 为 None：内联 truncate 策略
            summary, stats = self._truncate_fallback(compressible)

        # 合并已有摘要与新摘要
        if self.compressed_summary and summary:
            new_summary = self.compressed_summary + "\n\n" + summary
        else:
            new_summary = summary or self.compressed_summary

        # 更新状态：保留区替换原消息，summary 更新
        self.messages = preserved
        self.compressed_summary = new_summary
        self._recalculate_tokens()

        # 补充 before_tokens（compressor 返回的 before_tokens 是 messages 的，
        # 这里用上下文整体 before_tokens 覆盖）
        stats["before_tokens"] = before_tokens
        stats["after_tokens"] = self.total_tokens
        # 压缩次数累计
        self._compression_count += 1
        logger.info(
            "压缩完成",
            before_tokens=before_tokens,
            after_tokens=self.total_tokens,
            messages_compressed=stats["messages_compressed"],
            strategy=stats["strategy"],
            compression_count=self._compression_count,
        )
        return stats

    async def _do_layered_compress(self, before_tokens: int) -> CompressionStats:
        """使用 LayeredCompressor 执行分层压缩（调用方已持有锁）。"""
        # 将 Message 对象转为 dict 格式供 LayeredCompressor 使用
        dict_messages = [
            {"role": m.role, "content": m.content} for m in self.messages
        ]

        # 获取 provider（用于 Layer 2 LLM 摘要）
        provider = None
        if self.compressor is not None and hasattr(self.compressor, "provider"):
            provider = self.compressor.provider

        compressed, layered_stats = await self._layered_compressor.compress(
            messages=dict_messages,
            system_prompt=self.system_prompt,
            current_tokens=self.total_tokens,
            max_tokens=self.config.max_tokens,
            provider=provider,
        )

        # 将 dict 消息转回 Message 对象
        self.messages = [
            Message(role=m["role"], content=m.get("content", ""))
            for m in compressed
            if not m.get("_compressed")
        ]

        # 提取压缩摘要
        summary_parts = []
        for m in compressed:
            if m.get("_compressed"):
                summary_parts.append(m.get("content", ""))

        if summary_parts:
            new_summary = "\n\n".join(summary_parts)
            if self.compressed_summary:
                self.compressed_summary = self.compressed_summary + "\n\n" + new_summary
            else:
                self.compressed_summary = new_summary

        self._recalculate_tokens()

        # 转换为 CompressionStats 格式
        stats = CompressionStats(
            before_tokens=before_tokens,
            after_tokens=self.total_tokens,
            messages_compressed=layered_stats.messages_compressed,
            strategy="layered:" + "+".join(layered_stats.layers_applied) if layered_stats.layers_applied else "layered:none",
        )

        logger.info(
            "分层压缩完成",
            before_tokens=before_tokens,
            after_tokens=self.total_tokens,
            layers_applied=layered_stats.layers_applied,
            compression_count=self._compression_count + 1,
        )
        return stats

    def _truncate_fallback(
        self, compressible: list[Message]
    ) -> tuple[str, CompressionStats]:
        """compressor 为 None 时的内联 truncate 策略。

        生成简短摘要并返回统计。
        """
        n_messages = len(compressible)
        tokens = self.token_counter.count_messages(compressible)
        summary = (
            f"[Earlier conversation truncated: {n_messages} messages, {tokens} tokens]"
        )
        stats = CompressionStats(
            before_tokens=tokens,
            after_tokens=self.token_counter.count_text(summary),
            messages_compressed=n_messages,
            strategy="truncate",
        )
        return summary, stats

    def _split_messages(
        self,
        messages: list[Message],
        preserve_recent_turns: int,
    ) -> tuple[list[Message], list[Message]]:
        """将消息列表分为（可压缩区, 保留区）。

        保留最后 preserve_recent_turns 轮对话（user+assistant 对）。
        一轮对话 = 一对 user + assistant 消息。
        """
        if preserve_recent_turns <= 0 or not messages:
            return list(messages), []

        # 从末尾向前找 preserve_recent_turns 个 user 消息的位置
        user_indices: list[int] = []
        for i, msg in enumerate(messages):
            if msg.role == "user":
                user_indices.append(i)

        # 不足 N 轮：全部保留，无可压缩区
        if len(user_indices) < preserve_recent_turns:
            return [], list(messages)

        split_idx = user_indices[-preserve_recent_turns]
        compressible = list(messages[:split_idx])
        preserved = list(messages[split_idx:])
        return compressible, preserved

    # ------------------------------------------------------------------
    # 统计与用量
    # ------------------------------------------------------------------

    def get_stats(self) -> ContextStats:
        """返回统计信息。"""
        utilization = 0.0
        if self.config.max_tokens > 0:
            utilization = self.total_tokens / self.config.max_tokens
        return ContextStats(
            total_tokens=self.total_tokens,
            max_tokens=self.config.max_tokens,
            utilization=utilization,
            message_count=len(self.messages),
            compression_count=self._compression_count,
        )

    async def update_usage(self, input_tokens: int, output_tokens: int) -> None:
        """更新累计用量（来自 provider 的 Usage 事件）。"""
        async with self._lock:
            self._usage["input"] += input_tokens
            self._usage["output"] += output_tokens

    # ------------------------------------------------------------------
    # 清空与重算
    # ------------------------------------------------------------------

    async def clear(self) -> None:
        """清空对话历史（保留 system_prompt）。"""
        async with self._lock:
            self.messages = []
            self.compressed_summary = ""
            self._recalculate_tokens()
            logger.debug("上下文已清空", total_tokens=self.total_tokens)

    def _recalculate_tokens(self) -> None:
        """重新计算 total_tokens（system_prompt + summary + messages）。"""
        total = 0
        if self.system_prompt:
            total += self.token_counter.count_text(self.system_prompt)
        if self.compressed_summary:
            # summary 作为 assistant 消息插入，含 role 开销和前缀
            summary_msg_content = (
                "[Previous conversation summary]\n" + self.compressed_summary
            )
            total += self.token_counter.count_text(summary_msg_content)
            total += 4  # role 开销
        total += self.token_counter.count_messages(self.messages)
        self.total_tokens = total


__all__ = ["ContextManager", "ContextStats"]

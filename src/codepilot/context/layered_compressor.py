"""分层上下文压缩系统。

基于学术研究的分层压缩策略：
- Layer 0（40%触发）：Observation Masking — 只压缩工具输出，保留推理和行动
- Layer 1（60%触发）：工具结果截断 — 对超大工具输出进行智能截断
- Layer 2（75%触发）：LLM 摘要 — 对历史轮次进行语义摘要
- Layer 3（88%触发）：强制截断 — 丢弃最早的消息直到满足阈值

参考：JetBrains NeurIPS 2025（observation masking）、ACON 论文
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import structlog

from codepilot.context.token_counter import count_message_tokens, count_messages_tokens

logger = structlog.get_logger(__name__)


@dataclass
class CompressionStats:
    """压缩统计信息。"""
    before_tokens: int = 0
    after_tokens: int = 0
    layers_applied: list[str] = field(default_factory=list)
    messages_removed: int = 0
    messages_compressed: int = 0


class LayeredCompressor:
    """分层上下文压缩器。"""

    LAYER_0_THRESHOLD = 0.40  # Observation masking
    LAYER_1_THRESHOLD = 0.60  # Tool result truncation
    LAYER_2_THRESHOLD = 0.75  # LLM summarization
    LAYER_3_THRESHOLD = 0.88  # Force truncation

    OBSERVATION_HEAD_LINES = 20
    OBSERVATION_TAIL_LINES = 10
    MAX_SINGLE_RESULT_CHARS = 8000  # ~2000 tokens
    PRESERVE_RECENT_TURNS = 10
    SUMMARIZE_BATCH_SIZE = 21  # JetBrains: 每次摘要21轮
    PRESERVE_TAIL_RATIO = 0.10  # 最后10%逐字保留

    async def compress(
        self,
        messages: list[dict],
        system_prompt: str,
        current_tokens: int,
        max_tokens: int,
        provider: Any = None,
    ) -> tuple[list[dict], CompressionStats]:
        """分层压缩主函数。"""
        usage_ratio = current_tokens / max_tokens if max_tokens > 0 else 0
        stats = CompressionStats(before_tokens=current_tokens)

        if usage_ratio < self.LAYER_0_THRESHOLD:
            stats.after_tokens = current_tokens
            return messages, stats

        compressed = list(messages)  # shallow copy

        # Layer 0: Observation Masking
        if usage_ratio >= self.LAYER_0_THRESHOLD:
            compressed = self._mask_observations(compressed, max_tokens)
            stats.layers_applied.append("observation_masking")
            new_tokens = count_messages_tokens(compressed)
            if new_tokens / max_tokens < self.LAYER_1_THRESHOLD:
                stats.after_tokens = new_tokens
                return compressed, stats

        # Layer 1: 工具结果截断
        if usage_ratio >= self.LAYER_1_THRESHOLD:
            compressed = self._truncate_tool_results(compressed)
            stats.layers_applied.append("tool_result_truncation")
            new_tokens = count_messages_tokens(compressed)
            if new_tokens / max_tokens < self.LAYER_2_THRESHOLD:
                stats.after_tokens = new_tokens
                return compressed, stats

        # Layer 2: LLM 摘要
        if usage_ratio >= self.LAYER_2_THRESHOLD and provider is not None:
            compressed = await self._llm_summarize(compressed, provider, system_prompt, max_tokens)
            stats.layers_applied.append("llm_summarization")
            new_tokens = count_messages_tokens(compressed)
            if new_tokens / max_tokens < self.LAYER_3_THRESHOLD:
                stats.after_tokens = new_tokens
                return compressed, stats

        # Layer 3: 强制截断
        compressed = self._force_truncate(compressed, max_tokens)
        stats.layers_applied.append("force_truncation")
        stats.after_tokens = count_messages_tokens(compressed)
        return compressed, stats

    def _mask_observations(self, messages: list[dict], max_tokens: int) -> list[dict]:
        """对大型工具结果进行 head+tail 保留，折叠中间内容。"""
        preserve_tokens = int(max_tokens * self.PRESERVE_TAIL_RATIO)

        # 从后往前计算保留区的 token 数
        preserved_count = 0
        preserved_tokens = 0
        for i in range(len(messages) - 1, -1, -1):
            msg_tokens = count_message_tokens(messages[i])
            if preserved_tokens + msg_tokens > preserve_tokens:
                break
            preserved_tokens += msg_tokens
            preserved_count += 1

        result = []
        for i, msg in enumerate(messages):
            if i >= len(messages) - preserved_count:
                # 保留区：逐字保留
                result.append(msg)
                continue

            # 非保留区：对工具结果做 observation masking
            if msg.get("role") == "tool" or msg.get("role") == "ipython":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > self.MAX_SINGLE_RESULT_CHARS:
                    lines = content.split("\n")
                    if len(lines) > self.OBSERVATION_HEAD_LINES + self.OBSERVATION_TAIL_LINES:
                        head = "\n".join(lines[:self.OBSERVATION_HEAD_LINES])
                        tail = "\n".join(lines[-self.OBSERVATION_TAIL_LINES:])
                        folded_count = len(lines) - self.OBSERVATION_HEAD_LINES - self.OBSERVATION_TAIL_LINES
                        new_msg = dict(msg)
                        new_msg["content"] = f"{head}\n[... {folded_count} lines folded ...]\n{tail}"
                        result.append(new_msg)
                        continue

            result.append(msg)
        return result

    def _truncate_tool_results(self, messages: list[dict]) -> list[dict]:
        """截断超大工具结果。"""
        result = []
        for msg in messages:
            if msg.get("role") == "tool" or msg.get("role") == "ipython":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > self.MAX_SINGLE_RESULT_CHARS * 2:
                    lines = content.split("\n")
                    if len(lines) > 30:
                        head = "\n".join(lines[:10])
                        tail = "\n".join(lines[-5:])
                        new_msg = dict(msg)
                        new_msg["content"] = f"{head}\n[... {len(lines) - 15} lines truncated ...]\n{tail}"
                        result.append(new_msg)
                        continue
            result.append(msg)
        return result

    async def _llm_summarize(
        self,
        messages: list[dict],
        provider: Any,
        system_prompt: str,
        max_tokens: int,
    ) -> list[dict]:
        """使用 LLM 对历史消息进行摘要。"""
        preserve_tokens = int(max_tokens * self.PRESERVE_TAIL_RATIO)

        # 找到保留区分界点
        preserved_count = 0
        preserved_tokens = 0
        for i in range(len(messages) - 1, -1, -1):
            msg_tokens = count_message_tokens(messages[i])
            if preserved_tokens + msg_tokens > preserve_tokens:
                break
            preserved_tokens += msg_tokens
            preserved_count += 1

        split_idx = len(messages) - preserved_count
        old_messages = messages[:split_idx]
        recent_messages = messages[split_idx:]

        if not old_messages:
            return messages

        # 分批摘要
        summary_parts = []
        batch_size = self.SUMMARIZE_BATCH_SIZE * 2  # 每批约21轮（42条消息）
        for i in range(0, len(old_messages), batch_size):
            batch = old_messages[i:i + batch_size]
            summary_text = await self._summarize_batch(batch, provider, system_prompt)
            summary_parts.append(summary_text)

        # 构建摘要消息
        full_summary = "\n\n".join(summary_parts)
        summary_message = {
            "role": "assistant",
            "content": f"[Earlier conversation summary]\n{full_summary}",
            "_compressed": True,
        }

        return [summary_message] + recent_messages

    async def _summarize_batch(
        self, messages: list[dict], provider: Any, system_prompt: str
    ) -> str:
        """对一批消息生成摘要。"""
        # 构建摘要提示
        conversation_text = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                # 处理 list content
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            parts.append(f"[Called tool: {block.get('name', '')}]")
                content = " ".join(parts)
            conversation_text.append(f"{role}: {content[:500]}")

        prompt = (
            "Summarize the following conversation history concisely. "
            "Preserve: file paths, function/class/variable names, error messages, "
            "task progress, and key decisions. Be specific, not generic.\n\n"
            + "\n".join(conversation_text)
        )

        try:
            from codepilot.providers.base import AgentEvent, Message
            summary_messages = [Message(role="user", content=prompt)]
            result_text = ""
            async for event in provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
            ):
                if hasattr(event, 'text') and event.text:
                    result_text += event.text
            return result_text.strip() if result_text.strip() else "Conversation history was compressed."
        except Exception as e:
            logger.warning("LLM摘要失败，使用简单摘要", error=str(e))
            return f"[Summary unavailable: {len(messages)} messages compressed]"

    def _force_truncate(self, messages: list[dict], max_tokens: int) -> list[dict]:
        """强制截断：丢弃最早的消息直到满足阈值。"""
        target_tokens = int(max_tokens * 0.7)  # 截断到70%
        preserve_tokens = int(max_tokens * self.PRESERVE_TAIL_RATIO)

        # 从后往前计算保留区
        preserved_count = 0
        preserved_tokens = 0
        for i in range(len(messages) - 1, -1, -1):
            msg_tokens = count_message_tokens(messages[i])
            if preserved_tokens + msg_tokens > preserve_tokens:
                break
            preserved_tokens += msg_tokens
            preserved_count += 1

        split_idx = len(messages) - preserved_count
        old_messages = messages[:split_idx]
        recent_messages = messages[split_idx:]

        # 从前往后丢弃消息直到满足目标
        total = count_messages_tokens(messages)
        while old_messages and total > target_tokens:
            removed = old_messages.pop(0)
            total -= count_message_tokens(removed)

        return old_messages + recent_messages

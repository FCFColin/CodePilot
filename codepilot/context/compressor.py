"""上下文压缩器。

提供 summary / truncate / hybrid 三种压缩策略：
- summary：调用 provider.chat 用结构化提示压缩可压缩区
- truncate：直接丢弃可压缩区，生成简短摘要
- hybrid：工具输出截断 + 对话部分 summary/truncate

压缩前将完整历史追加写入 history_file（JSONL 格式）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from codepilot.context.token_counter import TokenCounter
from codepilot.providers.base import BaseProvider, Message, TextDelta, Done


# 摘要提示模板：要求 LLM 保留关键信息并按结构化格式输出
_SUMMARY_PROMPT = """Please compress the following conversation history into a structured summary.
You MUST preserve:
- All file paths mentioned
- All function/class/variable names
- All error messages and their resolutions
- Design decisions and their rationale
- Current task progress and status
- Key tool call results

Format your summary as:
## CONTEXT
[What was being worked on]

## KEY ACTIONS TAKEN
[Tools used, code written, files modified]

## OUTCOMES
[Results achieved, errors fixed, tests passed/failed]

## CURRENT STATE
[Where we are now, what's pending]

## IMPORTANT REFERENCES
[File paths, function names, config values to remember]
"""


class ContextCompressor:
    """上下文压缩器。

    根据 strategy 选择压缩方式：
    - "summary"：用 provider 生成结构化摘要（provider 不可用时回退 truncate）
    - "truncate"：丢弃可压缩区，生成简短摘要
    - "hybrid"：工具输出截断 + 对话部分 summary/truncate
    """

    def __init__(
        self,
        provider: BaseProvider | None = None,
        token_counter: TokenCounter | None = None,
        strategy: str = "summary",
        save_full_history: bool = True,
        history_file: str = ".codepilot_history.jsonl",
    ) -> None:
        self.provider = provider
        self.token_counter = token_counter or TokenCounter()
        self.strategy = strategy
        self.save_full_history = save_full_history
        self.history_file = history_file

    async def compress(
        self,
        messages: list[Message],
        preserve_recent_turns: int = 4,
        max_tokens: int = 120000,
    ) -> tuple[str, dict]:
        """压缩消息历史，返回 (summary, stats)。

        Args:
            messages: 完整消息列表。
            preserve_recent_turns: 保留最后 N 轮对话（user+assistant 对）不压缩。
            max_tokens: 上下文上限（用于 stats 计算）。

        Returns:
            (summary, stats) 元组。stats 含：
            - before_tokens: 压缩前总 token
            - after_tokens: 压缩后 token（summary + 保留区）
            - messages_compressed: 被压缩的消息数
            - strategy: 实际使用的策略名
        """
        before_tokens = self.token_counter.count_messages(messages)

        # 分区：可压缩区 + 保留区（最后 preserve_recent_turns 轮）
        compressible, preserved = self._split_messages(
            messages, preserve_recent_turns
        )

        # 没有可压缩内容时直接返回空摘要
        if not compressible:
            after_tokens = self.token_counter.count_messages(preserved)
            return "", {
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "messages_compressed": 0,
                "strategy": self.strategy,
            }

        # 先把完整历史落盘（避免压缩后丢失）
        if self.save_full_history:
            await self._append_history(compressible + preserved)

        # 按策略压缩
        strategy = self.strategy
        if strategy == "summary":
            summary = await self._compress_summary(compressible)
        elif strategy == "truncate":
            summary = self._compress_truncate(compressible)
        elif strategy == "hybrid":
            summary = await self._compress_hybrid(compressible)
        else:
            # 未知策略回退到 truncate
            strategy = "truncate"
            summary = self._compress_truncate(compressible)

        # 计算压缩后 token：summary + 保留区
        summary_tokens = self.token_counter.count_text(summary) if summary else 0
        preserved_tokens = self.token_counter.count_messages(preserved)
        after_tokens = summary_tokens + preserved_tokens

        stats = {
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "messages_compressed": len(compressible),
            "strategy": strategy,
        }
        return summary, stats

    # ------------------------------------------------------------------
    # 策略实现
    # ------------------------------------------------------------------

    async def _compress_summary(self, compressible: list[Message]) -> str:
        """summary 策略：调用 provider 生成结构化摘要。

        provider 不可用时回退到 truncate。
        """
        if self.provider is None:
            return self._compress_truncate(compressible)

        # 构造给 LLM 的对话文本
        conversation_text = self._messages_to_text(compressible)
        prompt_content = (
            _SUMMARY_PROMPT
            + "\n\nConversation to compress:\n\n"
            + conversation_text
        )

        # 构造单轮请求消息
        request_messages = [Message(role="user", content=prompt_content)]

        # 调用 provider.chat 收集文本响应
        summary_parts: list[str] = []
        try:
            async for event in self.provider.chat(
                request_messages,
                tools=None,
                system_prompt="",
                stream=True,
            ):
                if isinstance(event, TextDelta):
                    summary_parts.append(event.text)
                elif isinstance(event, Done):
                    # 异常结束时停止
                    if event.stop_reason.startswith("error"):
                        return self._compress_truncate(compressible)
                    break
        except Exception:
            # provider 调用失败时回退到 truncate
            return self._compress_truncate(compressible)

        summary = "".join(summary_parts).strip()
        if not summary:
            # 空摘要回退到 truncate
            return self._compress_truncate(compressible)
        return summary

    def _compress_truncate(self, compressible: list[Message]) -> str:
        """truncate 策略：丢弃可压缩区，生成简短摘要。"""
        n_messages = len(compressible)
        tokens = self.token_counter.count_messages(compressible)
        return (
            f"[Earlier conversation truncated: {n_messages} messages, "
            f"{tokens} tokens]"
        )

    async def _compress_hybrid(self, compressible: list[Message]) -> str:
        """hybrid 策略：工具输出截断 + 对话部分 summary/truncate。

        优先压缩顺序：文件内容 > 命令输出 > 对话历史。
        """
        # 第一遍：对工具输出/大段命令输出做截断，得到精简后的消息列表
        truncated_messages: list[Message] = []
        truncated_summary_parts: list[str] = []
        for msg in compressible:
            content = getattr(msg, "content", msg.get("content") if isinstance(msg, dict) else "")
            if self._is_tool_output(content):
                # 工具输出：截断为简短标记
                tokens = self.token_counter.count_message(msg)
                truncated_summary_parts.append(
                    f"[Tool output truncated: {tokens} tokens]"
                )
            else:
                truncated_messages.append(msg)

        # 第二步：对剩余对话部分用 summary（provider 可用）或 truncate
        if truncated_messages:
            if self.provider is not None:
                conversation_summary = await self._compress_summary(
                    truncated_messages
                )
            else:
                conversation_summary = self._compress_truncate(
                    truncated_messages
                )
        else:
            conversation_summary = ""

        # 合并摘要
        parts: list[str] = []
        if truncated_summary_parts:
            parts.append("\n".join(truncated_summary_parts))
        if conversation_summary:
            parts.append(conversation_summary)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

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
        # user 消息视为一轮的开始
        user_indices: list[int] = []
        for i, msg in enumerate(messages):
            role = self._get_role(msg)
            if role == "user":
                user_indices.append(i)

        # 保留最后 preserve_recent_turns 轮：从倒数第 N 个 user 消息开始
        if len(user_indices) < preserve_recent_turns:
            # 不足 N 轮：全部保留，无可压缩区
            return [], list(messages)

        split_idx = user_indices[-preserve_recent_turns]
        compressible = list(messages[:split_idx])
        preserved = list(messages[split_idx:])
        return compressible, preserved

    @staticmethod
    def _get_role(msg: Any) -> str:
        """从 Message 对象或 dict 提取 role。"""
        if isinstance(msg, dict):
            return str(msg.get("role", ""))
        return str(getattr(msg, "role", ""))

    @staticmethod
    def _get_content(msg: Any) -> Any:
        """从 Message 对象或 dict 提取 content。"""
        if isinstance(msg, dict):
            return msg.get("content", "")
        return getattr(msg, "content", "")

    def _messages_to_text(self, messages: list[Message]) -> str:
        """将消息列表转为可读文本（用于喂给 LLM 压缩）。"""
        lines: list[str] = []
        for msg in messages:
            role = self._get_role(msg)
            content = self._get_content(msg)
            text = self._content_to_text(content)
            lines.append(f"[{role}]: {text}")
        return "\n\n".join(lines)

    def _content_to_text(self, content: Any) -> str:
        """将 content（str 或 list）转为纯文本。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    text = block.get("text") or block.get("content")
                    if isinstance(text, str):
                        parts.append(text)
                    elif isinstance(text, list):
                        parts.append(self._content_to_text(text))
                    elif block.get("type"):
                        parts.append(f"[{block.get('type')}]")
            return "\n".join(parts)
        if content is None:
            return ""
        return str(content)

    def _is_tool_output(self, content: Any) -> bool:
        """判断 content 是否为工具输出（含 "Result:" 或大段命令输出）。

        判定规则：
        - content 为 str 且包含 "Result:" 标记
        - content 为 list 且任一 block 含 "Result:"
        - content 为 str 且较长（> 500 字符，视为大段命令输出）
        """
        if isinstance(content, str):
            if "Result:" in content:
                return True
            if len(content) > 500:
                return True
            return False
        if isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    if "Result:" in block or len(block) > 500:
                        return True
                elif isinstance(block, dict):
                    text = block.get("text") or block.get("content")
                    if isinstance(text, str) and (
                        "Result:" in text or len(text) > 500
                    ):
                        return True
            return False
        return False

    async def _append_history(self, messages: list[Message]) -> None:
        """将完整历史追加写入 history_file（JSONL 格式）。

        用 asyncio.to_thread 包装同步文件操作。
        每行：{"timestamp": ..., "role": ..., "content": ...}
        """
        if not messages:
            return

        # 预先构造行数据（避免在子线程访问对象属性时引发问题）
        lines: list[str] = []
        now = datetime.now(timezone.utc).isoformat()
        for msg in messages:
            role = self._get_role(msg)
            content = self._get_content(msg)
            # content 序列化为字符串（list/dict 转 JSON 字符串）
            if isinstance(content, (list, dict)):
                content_str = json.dumps(content, ensure_ascii=False)
            elif content is None:
                content_str = ""
            else:
                content_str = str(content)
            record = {
                "timestamp": now,
                "role": role,
                "content": content_str,
            }
            lines.append(json.dumps(record, ensure_ascii=False))

        await asyncio.to_thread(self._write_lines, lines)

    def _write_lines(self, lines: list[str]) -> None:
        """同步写入 JSONL 文件（追加模式）。"""
        try:
            with open(self.history_file, "a", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
        except OSError:
            # 写入失败时静默忽略（不阻塞压缩主流程）
            pass


__all__ = ["ContextCompressor"]

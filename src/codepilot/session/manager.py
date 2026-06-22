"""会话管理器。

管理会话生命周期：开始会话、记录消息与工具调用、保存到存储、加载历史。
SessionManager.save() 写入失败静默处理（只 log warning），不影响主流程。

注意：不记录 API Key 的任何部分，SessionRecord 中无 SecretStr 字段。
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog

from codepilot.session.storage import SessionRecord, SessionStorage

logger = structlog.get_logger(__name__)


class SessionManager:
    """会话管理器。

    管理会话生命周期，将对话消息与工具调用记录持久化到 SessionStorage。

    Attributes:
        storage: 会话存储实例。
        provider: LLM provider 名称。
        model: 模型名称。
        workspace_root: 工作区根目录。
    """

    def __init__(
        self,
        storage: SessionStorage,
        provider: str = "",
        model: str = "",
        workspace_root: Path | None = None,
    ) -> None:
        """初始化会话管理器。

        Args:
            storage: 会话存储实例。
            provider: LLM provider 名称。
            model: 模型名称。
            workspace_root: 工作区根目录。
        """
        self.storage = storage
        self.provider = provider
        self.model = model
        self.workspace_root = workspace_root or Path.cwd()
        self._record: SessionRecord | None = None

    def start_session(self) -> str:
        """开始新会话，返回 session_id，初始化 SessionRecord。

        Returns:
            新会话的 session_id。
        """
        session_id = f"{uuid4().hex[:8]}-{int(time.time())}"
        start_time = datetime.now(UTC).isoformat()
        self._record = SessionRecord(
            session_id=session_id,
            start_time=start_time,
            end_time=None,
            workspace_root=str(self.workspace_root),
            messages=[],
            tool_calls=[],
            token_usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "total": 0,
            },
            provider=self.provider,
            model=self.model,
        )
        logger.info("会话已开始", session_id=session_id)
        return session_id

    def add_message(self, role: str, content: str) -> None:
        """追加消息并更新 token 计数。

        Args:
            role: 消息角色（user/assistant/tool 等）。
            content: 消息内容。
        """
        if self._record is None:
            logger.warning("未开始会话，add_message 被忽略")
            return
        self._record["messages"].append({"role": role, "content": content})
        # 粗略估算 token：每 4 字符约 1 token
        estimated_tokens = max(1, len(content) // 4)
        if role == "user":
            self._record["token_usage"]["input_tokens"] += estimated_tokens
        elif role == "assistant":
            self._record["token_usage"]["output_tokens"] += estimated_tokens
        self._record["token_usage"]["total"] = (
            self._record["token_usage"]["input_tokens"]
            + self._record["token_usage"]["output_tokens"]
        )

    def record_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
        duration_ms: int,
    ) -> None:
        """记录工具调用，自动添加 timestamp（ISO 8601）。

        Args:
            tool_name: 工具名称。
            arguments: 工具参数。
            result: 工具执行结果。
            duration_ms: 执行耗时（毫秒）。
        """
        if self._record is None:
            logger.warning("未开始会话，record_tool_call 被忽略")
            return
        timestamp = datetime.now(UTC).isoformat()
        self._record["tool_calls"].append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "result": result,
                "duration_ms": duration_ms,
                "timestamp": timestamp,
            }
        )

    def add_thinking(self, content: str) -> None:
        """记录思考过程内容。

        将 thinking 内容追加到当前最后一条 assistant 消息的 thinking 字段。
        如果没有 assistant 消息，创建一条。

        Args:
            content: 思考过程文本片段。
        """
        if self._record is None:
            logger.warning("未开始会话，add_thinking 被忽略")
            return
        # 找到最后一条 assistant 消息，追加 thinking
        for msg in reversed(self._record["messages"]):
            if msg.get("role") == "assistant":
                msg.setdefault("thinking", "")
                msg["thinking"] += content
                return
        # 没有 assistant 消息，创建一条
        self._record["messages"].append(
            {
                "role": "assistant",
                "content": "",
                "thinking": content,
            }
        )

    def save(self) -> None:
        """调用 storage.save()，静默失败（写入失败不影响主流程，log warning）。"""
        if self._record is None:
            logger.warning("未开始会话，save 被忽略")
            return
        try:
            self.storage.save(self._record)
        except OSError as e:
            # 写入失败静默处理，只 log warning
            logger.warning(
                "会话保存失败",
                session_id=self._record["session_id"],
                error=str(e),
            )
        # 自动导出对话日志到工作目录
        self._auto_export_log()

    def _auto_export_log(self) -> None:
        """自动导出 Markdown 格式对话日志到 ~/.codepilot/logs/ 目录。"""
        if self._record is None:
            return
        try:
            from codepilot.session.export import SessionExporter

            exporter = SessionExporter()
            md_content = exporter.to_markdown(self._record)
            log_dir = Path(os.path.expanduser("~")) / ".codepilot" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"codepilot-log-{self._record['session_id']}.md"
            log_path.write_text(md_content, encoding="utf-8")
        except Exception as e:
            logger.warning("自动导出对话日志失败", error=str(e))

    def get_record(self) -> SessionRecord:
        """返回当前会话记录。

        Returns:
            当前会话记录。

        Raises:
            SessionError: 未开始会话时抛出。
        """
        if self._record is None:
            from codepilot.session.storage import SessionError

            raise SessionError("未开始会话")
        return self._record

    def load_history(self, session_id: str) -> list[dict[str, Any]]:
        """加载指定会话的 messages 历史。

        Args:
            session_id: 会话 ID。

        Returns:
            消息列表（每条含 role/content）。
        """
        record = self.storage.load(session_id)
        return list(record.get("messages", []))


__all__ = ["SessionManager"]

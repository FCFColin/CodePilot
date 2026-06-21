"""会话持久化存储。

将 SessionRecord 序列化为 JSON 文件存储到本地目录，支持保存、加载、列举、
获取最近会话。目录权限 0o700，文件以 session_id 命名。

注意：SessionRecord 中不存储 API Key（SecretStr.get_secret_value() 不得出现
在序列化路径上）。
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any, TypedDict

import structlog

from codepilot.exceptions import CodePilotError

logger = structlog.get_logger(__name__)

# 默认会话存储目录（模块级常量，避免在函数签名默认值中调用 Path.home()）
_DEFAULT_SESSIONS_DIR = Path.home() / ".codepilot" / "sessions"


class SessionError(CodePilotError):
    """会话相关错误。"""


class SessionRecord(TypedDict):
    """会话记录结构。

    Attributes:
        session_id: 会话唯一标识（uuid4 前 8 位 + 时间戳）。
        start_time: 会话开始时间（ISO 8601）。
        end_time: 会话结束时间（ISO 8601），未结束时为 None。
        workspace_root: 工作区根目录。
        messages: 对话消息列表（每条含 role/content）。
        tool_calls: 工具调用记录列表（含 tool_name/arguments/result/
            duration_ms/timestamp）。
        token_usage: token 用量统计（含 input_tokens/output_tokens/total）。
        provider: LLM provider 名称。
        model: 模型名称。
    """

    session_id: str
    start_time: str
    end_time: str | None
    workspace_root: str
    messages: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    token_usage: dict[str, Any]
    provider: str
    model: str


class SessionStorage:
    """会话持久化存储。

    将 SessionRecord 序列化为 JSON 文件存储到 sessions_dir 目录。
    目录权限 0o700，文件以 {session_id}.json 命名。
    """

    def __init__(
        self,
        sessions_dir: Path = _DEFAULT_SESSIONS_DIR,
    ) -> None:
        """初始化存储，自动创建目录（权限 0o700）。

        Args:
            sessions_dir: 会话文件存储目录。
        """
        self.sessions_dir = sessions_dir
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        """确保目录存在且权限为 0o700。"""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        # 设置目录权限为 0o700（Unix 有效，Windows 上为 no-op）
        with contextlib.suppress(OSError):
            os.chmod(self.sessions_dir, 0o700)

    def save(self, record: SessionRecord) -> Path:
        """将 record 序列化为 JSON 写入文件，返回文件路径。

        Args:
            record: 会话记录。

        Returns:
            写入的文件路径。
        """
        file_path = self.sessions_dir / f"{record['session_id']}.json"
        content = json.dumps(record, ensure_ascii=False, indent=2)
        file_path.write_text(content, encoding="utf-8")
        logger.debug(
            "会话已保存",
            session_id=record["session_id"],
            path=str(file_path),
        )
        return file_path

    def load(self, session_id: str) -> SessionRecord:
        """读取并反序列化会话记录。

        Args:
            session_id: 会话 ID。

        Returns:
            会话记录。

        Raises:
            SessionError: 文件不存在时抛出。
        """
        file_path = self.sessions_dir / f"{session_id}.json"
        if not file_path.exists():
            raise SessionError(f"会话不存在: {session_id}")
        content = file_path.read_text(encoding="utf-8")
        record: SessionRecord = json.loads(content)
        return record

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        """列出最近 limit 个会话，按 start_time 降序排列。

        Args:
            limit: 最多返回的会话数。

        Returns:
            会话记录列表（按 start_time 降序）。
        """
        if not self.sessions_dir.exists():
            return []
        records: list[SessionRecord] = []
        for json_file in self.sessions_dir.glob("*.json"):
            try:
                content = json_file.read_text(encoding="utf-8")
                record: SessionRecord = json.loads(content)
                records.append(record)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(
                    "跳过无法读取的会话文件",
                    path=str(json_file),
                    error=str(e),
                )
        # 按 start_time 降序排列
        records.sort(key=lambda r: r.get("start_time", ""), reverse=True)
        return records[:limit]

    def get_latest(self) -> SessionRecord | None:
        """返回最近一个会话，无则返回 None。"""
        records = self.list_sessions(limit=1)
        return records[0] if records else None


__all__ = ["SessionError", "SessionRecord", "SessionStorage"]

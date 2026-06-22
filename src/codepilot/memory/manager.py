"""跨会话持久记忆系统。

存储位置：
- 项目级：<workspace>/.codepilot/memories.md
- 用户级：~/.codepilot/memories.json

在系统提示中自动附加相关记忆。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class MemoryManager:
    """跨会话记忆管理器。"""

    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root
        self._project_memories_path = Path(workspace_root) / ".codepilot" / "memories.md"
        self._global_memories_path = Path.home() / ".codepilot" / "memories.json"
        self._project_memories: list[str] = []
        self._global_memories: list[dict[str, Any]] = []
        self._load_memories()

    def _load_memories(self) -> None:
        """加载所有记忆。"""
        # 项目级记忆（Markdown 格式，每行一条）
        if self._project_memories_path.is_file():
            try:
                content = self._project_memories_path.read_text(encoding="utf-8")
                self._project_memories = [
                    line.strip().lstrip("- ").strip()
                    for line in content.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
                logger.debug("已加载项目记忆", count=len(self._project_memories))
            except OSError as e:
                logger.warning("加载项目记忆失败", error=str(e))

        # 用户级记忆（JSON 格式）
        if self._global_memories_path.is_file():
            try:
                content = self._global_memories_path.read_text(encoding="utf-8")
                data = json.loads(content)
                self._global_memories = data if isinstance(data, list) else []
                logger.debug("已加载全局记忆", count=len(self._global_memories))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("加载全局记忆失败", error=str(e))

    def save_project_memory(self, text: str) -> None:
        """保存项目级记忆。"""
        self._project_memories.append(text)
        self._save_project_memories_file()

    def save_global_memory(self, text: str, scope: str = "general") -> None:
        """保存用户级记忆。"""
        entry = {
            "text": text,
            "scope": scope,
            "created_at": datetime.now().isoformat(),
        }
        self._global_memories.append(entry)
        self._save_global_memories_file()

    def delete_project_memory(self, index: int) -> bool:
        """删除项目级记忆。"""
        if 0 <= index < len(self._project_memories):
            self._project_memories.pop(index)
            self._save_project_memories_file()
            return True
        return False

    def delete_global_memory(self, index: int) -> bool:
        """删除用户级记忆。"""
        if 0 <= index < len(self._global_memories):
            self._global_memories.pop(index)
            self._save_global_memories_file()
            return True
        return False

    def get_all_memories_text(self) -> str:
        """获取格式化的记忆文本，用于系统提示。"""
        parts = []
        if self._project_memories:
            parts.append("[Project Memory]")
            for m in self._project_memories:
                parts.append(f"- {m}")
        if self._global_memories:
            parts.append("[Global Memory]")
            for m in self._global_memories:
                parts.append(f"- {m.get('text', '')}")
        return "\n".join(parts)

    def list_memories(self) -> dict[str, Any]:
        """列出所有记忆。"""
        return {
            "project": self._project_memories,
            "global": self._global_memories,
        }

    def _save_project_memories_file(self) -> None:
        """保存项目级记忆到文件。"""
        try:
            self._project_memories_path.parent.mkdir(parents=True, exist_ok=True)
            content = "# Project Memories\n\n"
            content += "\n".join(f"- {m}" for m in self._project_memories)
            self._project_memories_path.write_text(content, encoding="utf-8")
        except OSError as e:
            logger.error("保存项目记忆失败", error=str(e))

    def _save_global_memories_file(self) -> None:
        """保存用户级记忆到文件。"""
        try:
            self._global_memories_path.parent.mkdir(parents=True, exist_ok=True)
            self._global_memories_path.write_text(
                json.dumps(self._global_memories, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error("保存全局记忆失败", error=str(e))

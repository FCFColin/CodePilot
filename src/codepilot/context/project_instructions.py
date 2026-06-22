"""项目级指令文件加载器。

从以下位置按优先级加载 CODEPILOT.md：
1. ~/.codepilot/CODEPILOT.md（全局用户指令）
2. 项目根目录 CODEPILOT.md
3. .codepilot/CODEPILOT.md（项目级覆盖）

内容追加到系统提示尾部。
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


def load_project_instructions(workspace_root: str) -> str:
    """加载所有 CODEPILOT.md 文件内容，按优先级合并。"""
    parts = []
    loaded_files = []

    # 1. 全局用户指令
    global_path = Path.home() / ".codepilot" / "CODEPILOT.md"
    if global_path.is_file():
        content = global_path.read_text(encoding="utf-8").strip()
        if content:
            parts.append(f"[Global Instructions]\n{content}")
            loaded_files.append(str(global_path))

    # 2. 项目根目录
    project_path = Path(workspace_root) / "CODEPILOT.md"
    if project_path.is_file():
        content = project_path.read_text(encoding="utf-8").strip()
        if content:
            parts.append(f"[Project Instructions]\n{content}")
            loaded_files.append(str(project_path))

    # 3. .codepilot/CODEPILOT.md
    override_path = Path(workspace_root) / ".codepilot" / "CODEPILOT.md"
    if override_path.is_file():
        content = override_path.read_text(encoding="utf-8").strip()
        if content:
            parts.append(f"[Project Override Instructions]\n{content}")
            loaded_files.append(str(override_path))

    if loaded_files:
        logger.info("已加载项目指令文件", files=loaded_files)

    return "\n\n".join(parts)


def get_loaded_instruction_files(workspace_root: str) -> list[str]:
    """返回已找到的指令文件路径列表。"""
    files = []
    for path in [
        Path.home() / ".codepilot" / "CODEPILOT.md",
        Path(workspace_root) / "CODEPILOT.md",
        Path(workspace_root) / ".codepilot" / "CODEPILOT.md",
    ]:
        if path.is_file():
            files.append(str(path))
    return files

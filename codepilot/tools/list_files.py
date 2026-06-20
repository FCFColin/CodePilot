"""目录列表工具 ListFilesTool。

递归列出目录树，支持深度限制和忽略过滤。
"""

from __future__ import annotations

import asyncio
import os

from codepilot.tools.registry import ApprovalProtocol, BaseTool, SandboxProtocol


# 默认忽略的目录名
_DEFAULT_IGNORE = ["__pycache__", ".git", "node_modules", ".venv"]
# 最大条目数
_MAX_ENTRIES = 200


class ListFilesTool(BaseTool):
    """递归列出目录树。"""

    name = "list_files"
    description = (
        "递归列出指定目录的文件树。路径相对于工作区根目录。"
        "支持深度限制和目录忽略过滤。超过 200 个条目时截断。"
    )

    def __init__(
        self,
        workspace_root: str = ".",
        require_approval_for: list[str] | None = None,
    ):
        self.workspace_root = workspace_root
        self.require_approval_for = require_approval_for if require_approval_for is not None else [
            "file_write", "file_edit", "shell_exec",
        ]

    def get_parameters(self) -> dict:
        """返回参数 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要列出的目录路径，相对于工作区根目录",
                    "default": ".",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "最大递归深度",
                    "default": 3,
                },
                "ignore": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要忽略的目录名列表",
                    "default": _DEFAULT_IGNORE,
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(
        self,
        arguments: dict,
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
    ) -> str:
        """执行目录列表。"""
        path = arguments.get("path", ".")
        max_depth = arguments.get("max_depth", 3)
        ignore = arguments.get("ignore", _DEFAULT_IGNORE)

        # sandbox 路径校验
        if sandbox is not None:
            ok, msg = sandbox.validate_path(path, "read")
            if not ok:
                return f"Error: path validation failed: {msg}"

        # 解析绝对路径
        if os.path.isabs(path):
            abs_path = os.path.realpath(path)
        else:
            abs_path = os.path.realpath(os.path.join(self.workspace_root, path))

        try:
            entries = await asyncio.to_thread(
                self._build_tree, abs_path, path, max_depth, ignore
            )
        except Exception as e:
            return f"Error listing files: {e}"

        if not entries:
            return f"Listed 0 entries in {path}:\n(empty or not a directory)"

        total = len(entries)
        # 截断到 200 条
        if total > _MAX_ENTRIES:
            displayed = entries[:_MAX_ENTRIES]
            remaining = total - _MAX_ENTRIES
            tree = "\n".join(displayed)
            tree += f"\n[... {remaining} more entries ...]"
        else:
            tree = "\n".join(entries)

        return f"Listed {total} entries in {path}:\n{tree}"

    def _build_tree(
        self, abs_path: str, rel_path: str, max_depth: int, ignore: list[str]
    ) -> list[str]:
        """同步构建目录树条目列表（由 asyncio.to_thread 调用）。"""
        if not os.path.isdir(abs_path):
            return []

        entries: list[str] = []
        self._collect_entries(abs_path, "", max_depth, 0, set(ignore), entries)
        return entries

    def _collect_entries(
        self,
        abs_dir: str,
        prefix: str,
        max_depth: int,
        current_depth: int,
        ignore: set[str],
        entries: list[str],
    ) -> None:
        """递归收集目录条目，生成树状结构。"""
        if current_depth > max_depth:
            return

        try:
            items = sorted(os.listdir(abs_dir))
        except OSError:
            return

        # 过滤忽略项
        items = [item for item in items if item not in ignore]

        for i, item in enumerate(items):
            # 达到上限停止
            if len(entries) >= _MAX_ENTRIES * 2:
                return

            is_last = i == len(items) - 1
            connector = "└── " if is_last else "├── "
            item_path = os.path.join(abs_dir, item)
            is_dir = os.path.isdir(item_path)
            display_name = item + "/" if is_dir else item
            entries.append(f"{prefix}{connector}{display_name}")

            # 递归子目录
            if is_dir and current_depth < max_depth:
                extension = "    " if is_last else "│   "
                self._collect_entries(
                    item_path,
                    prefix + extension,
                    max_depth,
                    current_depth + 1,
                    ignore,
                    entries,
                )


__all__ = ["ListFilesTool"]

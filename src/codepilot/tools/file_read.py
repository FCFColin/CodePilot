"""文件读取工具 ReadFileTool。

读取文件内容并带行号显示，自动跳过二进制文件，超过 100KB 截断。
I/O 异常包装为 ToolError，由 execute 捕获并转为错误字符串。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog

from codepilot.exceptions import ToolError
from codepilot.tools.registry import ApprovalProtocol, BaseTool, SandboxProtocol

logger = structlog.get_logger(__name__)

# 二进制检测：读取前 8KB 检查是否含 \x00
_BINARY_CHECK_SIZE = 8192
# 文件大小截断阈值：100KB
_MAX_FILE_SIZE = 100 * 1024


class ReadFileTool(BaseTool):
    """读取文件内容，带行号显示。"""

    name = "read_file"
    description = (
        "读取指定文件的内容并带行号显示。路径相对于工作区根目录。"
        "自动跳过二进制文件。超过 100KB 的文件会被截断。"
    )

    def __init__(
        self,
        workspace_root: str = ".",
        require_approval_for: list[str] | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self.require_approval_for = (
            require_approval_for
            if require_approval_for is not None
            else ["file_write", "file_edit", "shell_exec"]
        )

    def get_parameters(self) -> dict[str, Any]:
        """返回参数 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要读取的文件路径，相对于工作区根目录",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        arguments: dict[str, Any],
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
    ) -> str:
        """执行文件读取。

        Args:
            arguments: 工具参数，必须包含 path。
            sandbox: 可选沙箱校验器。
            approval: 可选审批器（读取操作默认无需审批）。

        Returns:
            带行号的文件内容字符串；出错时返回 "Error: ..."。
        """
        path = arguments.get("path", "")
        if not path:
            return "Error: path parameter is required"

        # sandbox 路径校验
        if sandbox is not None:
            ok, msg = sandbox.validate_path(path, "read")
            if not ok:
                logger.warning("路径校验失败", path=path, reason=msg)
                return f"Error: path validation failed: {msg}"

        # 解析绝对路径（相对 workspace_root）
        if os.path.isabs(path):
            abs_path = os.path.realpath(path)
        else:
            abs_path = os.path.realpath(os.path.join(self.workspace_root, path))

        try:
            return await asyncio.to_thread(self._read_file, path, abs_path)
        except ToolError as e:
            return f"Error reading file: {e}"
        except OSError as e:
            logger.error("读取文件 I/O 失败", path=path, error=str(e))
            return f"Error reading file: {e}"

    def _read_file(self, rel_path: str, abs_path: str) -> str:
        """同步读取文件内容（由 asyncio.to_thread 调用）。

        Raises:
            ToolError: 文件不存在或读取失败时抛出。
        """
        if not os.path.isfile(abs_path):
            raise ToolError(f"file not found: {rel_path}")

        try:
            file_size = os.path.getsize(abs_path)

            # 二进制检测：读取前 8KB 检查是否含 \x00
            with open(abs_path, "rb") as f:
                chunk = f.read(_BINARY_CHECK_SIZE)
            if b"\x00" in chunk:
                raise ToolError(f"binary file detected, skipped: {rel_path}")

            # 读取文本内容
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            raise ToolError(f"读取文件失败: {e}") from e

        # 超过 100KB 截断
        truncated = False
        if file_size > _MAX_FILE_SIZE:
            content = content[:_MAX_FILE_SIZE]
            truncated = True

        # 带行号显示：格式 "  1→内容"
        lines = content.split("\n")
        max_num_len = max(len(str(len(lines))), 1)
        numbered_lines = []
        for i, line in enumerate(lines, 1):
            numbered_lines.append(f"{str(i).rjust(max_num_len)}→{line}")
        result = "\n".join(numbered_lines)

        if truncated:
            result += f"\n[... file truncated, total {file_size} bytes ...]"

        logger.debug("读取文件成功", path=rel_path, size=file_size, truncated=truncated)
        return f"Read file: {rel_path}\n{result}"


__all__ = ["ReadFileTool"]

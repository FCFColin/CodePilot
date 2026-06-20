"""文件写入工具 WriteFileTool。

写入文件（UTF-8），自动创建父目录，生成 diff 预览并请求审批。
I/O 异常包装为 ToolError，由 execute 捕获并转为错误字符串。
"""

from __future__ import annotations

import asyncio
import difflib
import os
from typing import Any

import structlog

from codepilot.exceptions import ToolError
from codepilot.tools.registry import ApprovalProtocol, BaseTool, SandboxProtocol

logger = structlog.get_logger(__name__)


class WriteFileTool(BaseTool):
    """写入文件，支持创建/覆写，生成 diff 预览。"""

    name = "write_file"
    description = (
        "写入文件内容（UTF-8 编码）。路径相对于工作区根目录。"
        "自动创建父目录。若文件已存在则覆写。操作前需审批确认。"
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
                    "description": "要写入的文件路径，相对于工作区根目录",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的文件内容",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        arguments: dict[str, Any],
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
    ) -> str:
        """执行文件写入。

        Args:
            arguments: 工具参数，必须包含 path 和 content。
            sandbox: 可选沙箱校验器。
            approval: 可选审批器，file_write 在 require_approval_for 中时需审批。

        Returns:
            写入成功信息；出错或被拒绝时返回 "Error: ..."。
        """
        path = arguments.get("path", "")
        content = arguments.get("content", "")
        if not path:
            return "Error: path parameter is required"

        # sandbox 路径校验
        if sandbox is not None:
            ok, msg = sandbox.validate_path(path, "write")
            if not ok:
                logger.warning("路径校验失败", path=path, reason=msg)
                return f"Error: path validation failed: {msg}"

        # 解析绝对路径
        if os.path.isabs(path):
            abs_path = os.path.realpath(path)
        else:
            abs_path = os.path.realpath(os.path.join(self.workspace_root, path))

        # 生成 diff 预览
        old_content = await asyncio.to_thread(self._read_existing, abs_path)
        diff_text = self._generate_diff(path, old_content, content)

        # 审批检查
        if approval is not None and "file_write" in self.require_approval_for:
            approved = await approval.request_approval(
                "file_write",
                {"path": path, "content": content, "diff": diff_text},
            )
            if not approved:
                logger.info("文件写入被拒绝", path=path)
                return f"Error: file write to '{path}' was not approved"

        # 执行写入
        try:
            await asyncio.to_thread(self._write_file, abs_path, content)
        except ToolError as e:
            return f"Error writing file: {e}"
        except OSError as e:
            logger.error("写入文件 I/O 失败", path=path, error=str(e))
            return f"Error writing file: {e}"

        # 统计行数和字节数
        line_count = content.count("\n") + (1 if content else 0)
        byte_count = len(content.encode("utf-8"))
        logger.info("文件写入成功", path=path, lines=line_count, bytes=byte_count)
        return f"File written: {path} ({line_count} lines, {byte_count} bytes)"

    def _read_existing(self, abs_path: str) -> str | None:
        """读取已存在的文件内容，不存在返回 None。

        Raises:
            ToolError: 读取失败（非不存在）时抛出。
        """
        if not os.path.isfile(abs_path):
            return None
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError as e:
            raise ToolError(f"读取已有文件失败: {e}") from e

    def _generate_diff(
        self, rel_path: str, old_content: str | None, new_content: str
    ) -> str:
        """生成简单 diff。新文件全部为新增行（+ 前缀）。"""
        old_lines = (old_content or "").splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"{rel_path} (old)" if old_content is not None else "/dev/null",
            tofile=f"{rel_path} (new)",
            lineterm="",
        )
        return "".join(diff)

    def _write_file(self, abs_path: str, content: str) -> None:
        """同步写入文件（由 asyncio.to_thread 调用）。

        Raises:
            ToolError: 写入失败时抛出。
        """
        # 自动创建父目录
        parent = os.path.dirname(abs_path)
        try:
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            raise ToolError(f"写入文件失败: {e}") from e


__all__ = ["WriteFileTool"]

"""文件编辑工具 EditFileTool。

基于唯一匹配的搜索替换编辑文件，生成 diff 预览并请求审批。
"""

from __future__ import annotations

import asyncio
import difflib
import os

from codepilot.tools.registry import ApprovalProtocol, BaseTool, SandboxProtocol


class EditFileTool(BaseTool):
    """搜索替换编辑文件，要求 old_string 唯一匹配。"""

    name = "edit_file"
    description = (
        "编辑文件：将文件中唯一匹配的 old_string 替换为 new_string。"
        "路径相对于工作区根目录。要求 old_string 在文件中唯一存在。"
        "操作前需审批确认。"
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
                    "description": "要编辑的文件路径，相对于工作区根目录",
                },
                "old_string": {
                    "type": "string",
                    "description": "要替换的原始内容（必须在文件中唯一存在）",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换为的新内容",
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        arguments: dict,
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
    ) -> str:
        """执行文件编辑。"""
        path = arguments.get("path", "")
        old_string = arguments.get("old_string", "")
        new_string = arguments.get("new_string", "")
        if not path:
            return "Error: path parameter is required"
        if not old_string:
            return "Error: old_string parameter is required"
        if "new_string" not in arguments:
            return "Error: new_string parameter is required"

        # sandbox 路径校验
        if sandbox is not None:
            ok, msg = sandbox.validate_path(path, "write")
            if not ok:
                return f"Error: path validation failed: {msg}"

        # 解析绝对路径
        if os.path.isabs(path):
            abs_path = os.path.realpath(path)
        else:
            abs_path = os.path.realpath(os.path.join(self.workspace_root, path))

        try:
            # 读取原文件内容
            old_content = await asyncio.to_thread(self._read_file, abs_path)
            if old_content is None:
                return f"Error: file not found: {path}"

            # 查找 old_string 出现次数，要求唯一匹配
            count = old_content.count(old_string)
            if count == 0:
                return f"Error: old_string not found in {path}"
            if count > 1:
                return f"Error: old_string appears {count} times in {path}, expected unique match"

            # 生成新内容
            new_content = old_content.replace(old_string, new_string, 1)

            # 生成 diff 预览
            diff_text = self._generate_diff(path, old_content, new_content)

            # 审批检查
            if approval is not None and "file_edit" in self.require_approval_for:
                approved = await approval.request_approval(
                    "file_edit",
                    {
                        "path": path,
                        "old_string": old_string,
                        "new_string": new_string,
                        "diff": diff_text,
                    },
                )
                if not approved:
                    return f"Error: file edit to '{path}' was not approved"

            # 执行替换并写回
            await asyncio.to_thread(self._write_file, abs_path, new_content)
            return f"File edited: {path} (1 replacement)"

        except Exception as e:
            return f"Error editing file: {e}"

    def _read_file(self, abs_path: str) -> str | None:
        """同步读取文件，不存在返回 None。"""
        if not os.path.isfile(abs_path):
            return None
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def _write_file(self, abs_path: str, content: str) -> None:
        """同步写入文件。"""
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

    def _generate_diff(self, rel_path: str, old_content: str, new_content: str) -> str:
        """生成 unified diff 预览。"""
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"{rel_path} (old)",
            tofile=f"{rel_path} (new)",
            lineterm="",
        )
        return "".join(diff)


__all__ = ["EditFileTool"]

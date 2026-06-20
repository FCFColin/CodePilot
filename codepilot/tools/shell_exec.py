"""Shell 命令执行工具 ShellExecTool。

在工作区根目录执行 shell 命令，支持超时和输出截断，禁止交互式命令。
"""

from __future__ import annotations

import asyncio
import os
import time

from codepilot.tools.registry import ApprovalProtocol, BaseTool, SandboxProtocol


# 禁止的交互式命令（命令开头匹配）
_FORBIDDEN_INTERACTIVE = {"vim", "nano", "less", "more", "top", "htop", "man"}
# 输出截断：最大行数
_MAX_OUTPUT_LINES = 200
# 输出截断：每行最大字符数
_MAX_LINE_LENGTH = 2000


class ShellExecTool(BaseTool):
    """执行 shell 命令，捕获 stdout/stderr。"""

    name = "shell_exec"
    description = (
        "在工作区根目录执行 shell 命令。默认超时 30 秒。"
        "输出截断到 200 行，每行最多 2000 字符。"
        "禁止交互式命令（vim/nano/less/more/top/htop/man）。"
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
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时时间（秒），默认 30",
                    "default": 30,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        arguments: dict,
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
    ) -> str:
        """执行 shell 命令。"""
        command = arguments.get("command", "")
        timeout = arguments.get("timeout", 30)
        if not command:
            return "Error: command parameter is required"

        # 额外检查：禁止交互式命令（检查命令开头）
        first_token = command.strip().split()[0] if command.strip() else ""
        if first_token in _FORBIDDEN_INTERACTIVE:
            return f"Error: interactive command '{first_token}' is not allowed"

        # sandbox 命令校验
        if sandbox is not None:
            ok, msg = sandbox.validate_command(command)
            if not ok:
                return f"Error: command validation failed: {msg}"

        # 审批检查
        if approval is not None and "shell_exec" in self.require_approval_for:
            approved = await approval.request_approval(
                "shell_exec", {"command": command}
            )
            if not approved:
                return f"Error: command execution was not approved"

        # 解析工作目录为绝对路径
        cwd = os.path.realpath(self.workspace_root)

        start_time = time.monotonic()
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except Exception as e:
            return f"Error starting command: {e}"

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # 超时杀进程
            try:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass
            duration_ms = int((time.monotonic() - start_time) * 1000)
            return (
                f"Command: {command}\n"
                f"Error: command timed out after {timeout}s\n"
                f"Duration: {duration_ms}ms"
            )

        duration_ms = int((time.monotonic() - start_time) * 1000)
        exit_code = process.returncode if process.returncode is not None else -1

        # 解码输出
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        # 截断输出
        stdout_truncated, stdout_was_truncated = self._truncate_output(stdout_text)
        stderr_truncated, stderr_was_truncated = self._truncate_output(stderr_text)

        result = (
            f"Command: {command}\n"
            f"Exit code: {exit_code}\n"
            f"Duration: {duration_ms}ms\n"
            f"--- stdout ---\n{stdout_truncated}\n"
            f"--- stderr ---\n{stderr_truncated}"
        )
        if stdout_was_truncated or stderr_was_truncated:
            result += "\n[output truncated]"
        return result

    def _truncate_output(self, text: str) -> tuple[str, bool]:
        """截断输出：最多 200 行，每行最多 2000 字符。

        Returns:
            (截断后的文本, 是否发生了截断)
        """
        if not text:
            return "", False

        lines = text.split("\n")
        truncated = False

        # 行数截断
        if len(lines) > _MAX_OUTPUT_LINES:
            lines = lines[:_MAX_OUTPUT_LINES]
            truncated = True

        # 每行字符截断
        lines = [line[:_MAX_LINE_LENGTH] for line in lines]

        return "\n".join(lines), truncated


__all__ = ["ShellExecTool"]

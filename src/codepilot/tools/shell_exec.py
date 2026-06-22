"""Shell 命令执行工具 ShellExecTool。

在工作区根目录执行 shell 命令，支持超时和输出截断，禁止交互式命令。
I/O 异常包装为 ToolError，由 execute 捕获并转为错误字符串。
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from typing import Any

import structlog

from codepilot.tools.registry import ApprovalProtocol, BaseTool, SandboxProtocol

logger = structlog.get_logger(__name__)

# 禁止的交互式命令（命令开头匹配）
_FORBIDDEN_INTERACTIVE: set[str] = {
    "vim",
    "nano",
    "less",
    "more",
    "top",
    "htop",
    "man",
}
# 输出截断：最大行数
_MAX_OUTPUT_LINES = 200
# 输出截断：每行最大字符数
_MAX_LINE_LENGTH = 2000
# 输出截断：最大总字节数
_MAX_OUTPUT_BYTES = 1 * 1024 * 1024  # 1MB


class ShellExecTool(BaseTool):
    """执行 shell 命令，捕获 stdout/stderr。"""

    name = "shell_exec"
    description = (
        "在工作区根目录执行 shell 命令。默认超时 30 秒。"
        "输出截断到 200 行，每行最多 2000 字符。"
        "禁止交互式命令（vim/nano/less/more/top/htop/man）。"
        "操作前需审批确认。"
    )

    # ANSI 转义序列正则
    _ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[a-zA-Z]')

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
        arguments: dict[str, Any],
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
    ) -> str:
        """执行 shell 命令。

        Args:
            arguments: 工具参数，必须包含 command，可选 timeout。
            sandbox: 可选沙箱校验器。
            approval: 可选审批器，shell_exec 在 require_approval_for 中时需审批。

        Returns:
            命令执行结果（含退出码、stdout、stderr）；出错时返回 "Error: ..."。
        """
        command = arguments.get("command", "")
        timeout = arguments.get("timeout", 30)
        if not command:
            return "Error: command parameter is required"

        # 额外检查：禁止交互式命令（检查命令开头）
        first_token = command.strip().split()[0] if command.strip() else ""
        if first_token in _FORBIDDEN_INTERACTIVE:
            logger.warning("拒绝交互式命令", command=command)
            return f"Error: interactive command '{first_token}' is not allowed"

        # sandbox 命令校验
        if sandbox is not None:
            ok, msg = sandbox.validate_command(command)
            if not ok:
                logger.warning("命令校验失败", command=command, reason=msg)
                return f"Error: command validation failed: {msg}"

        # 审批检查
        if approval is not None and "shell_exec" in self.require_approval_for:
            approved = await approval.request_approval(
                "shell_exec", {"command": command}
            )
            if not approved:
                logger.info("命令执行被拒绝", command=command)
                return "Error: command execution was not approved"

        # 解析工作目录为绝对路径
        cwd = os.path.realpath(self.workspace_root)

        start_time = time.monotonic()
        try:
            # Windows 不支持 os.setsid 和 os.killpg，需要条件处理
            if os.name != 'nt':
                preexec_fn = os.setsid
            else:
                preexec_fn = None
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                preexec_fn=preexec_fn,
            )
        except OSError as e:
            logger.error("启动命令失败", command=command, error=str(e))
            return f"Error starting command: {e}"

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            # 超时杀进程：Unix 使用进程组，Windows 使用 process.kill
            if os.name != 'nt' and process.pid:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            else:
                process.kill()
            await process.wait()
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning("命令超时", command=command, timeout=timeout)
            return (
                f"Command: {command}\n"
                f"Error: command timed out after {timeout}s\n"
                f"Duration: {duration_ms}ms"
            )

        duration_ms = int((time.monotonic() - start_time) * 1000)
        exit_code = process.returncode if process.returncode is not None else -1

        # 最大输出大小限制
        total_bytes = len(stdout_bytes) + len(stderr_bytes)
        if total_bytes > _MAX_OUTPUT_BYTES:
            stdout_bytes = stdout_bytes[:_MAX_OUTPUT_BYTES]
            remaining = _MAX_OUTPUT_BYTES - len(stdout_bytes)
            stderr_bytes = stderr_bytes[:max(0, remaining)]

        # 解码输出
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        # 过滤 ANSI 转义序列
        stdout_text = self._strip_ansi(stdout_text)
        stderr_text = self._strip_ansi(stderr_text)

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
        logger.info(
            "命令执行完成",
            command=command,
            exit_code=exit_code,
            duration_ms=duration_ms,
        )
        return result

    def _strip_ansi(self, text: str) -> str:
        """移除 ANSI 转义序列，防止终端污染。"""
        return self._ANSI_ESCAPE.sub('', text)

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

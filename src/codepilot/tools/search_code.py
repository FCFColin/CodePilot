"""代码搜索工具 SearchCodeTool。

递归扫描文件，用正则表达式匹配每行，输出 grep 风格结果。
I/O 异常包装为 ToolError，由 execute 捕获并转为错误字符串。
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from typing import Any

import structlog

from codepilot.exceptions import ToolError
from codepilot.tools.registry import ApprovalProtocol, BaseTool, SandboxProtocol

logger = structlog.get_logger(__name__)

# 默认忽略的目录名
_IGNORE_DIRS: set[str] = {"__pycache__", ".git", "node_modules", ".venv"}
# 二进制检测：读取前 8KB 检查是否含 \x00
_BINARY_CHECK_SIZE = 8192
# 默认包含的文件扩展名
_DEFAULT_INCLUDE = (
    "*.py,*.js,*.ts,*.java,*.go,*.rs,*.c,*.cpp,*.h,*.md,*.txt,*.yml,*.yaml,*.json"
)


class SearchCodeTool(BaseTool):
    """正则搜索文件内容，输出 grep 风格结果。"""

    name = "search_code"
    description = (
        "在指定目录下递归搜索文件内容（正则匹配）。"
        "路径相对于工作区根目录。输出 grep 风格结果："
        "文件路径:行号:行内容。自动跳过二进制文件和常见缓存目录。"
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
                "pattern": {
                    "type": "string",
                    "description": "正则表达式模式",
                },
                "path": {
                    "type": "string",
                    "description": "搜索目录路径，相对于工作区根目录",
                    "default": ".",
                },
                "include": {
                    "type": "string",
                    "description": "逗号分隔的文件名 glob 模式，如 '*.py,*.js'",
                    "default": _DEFAULT_INCLUDE,
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回匹配数",
                    "default": 50,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        arguments: dict[str, Any],
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
    ) -> str:
        """执行代码搜索。

        Args:
            arguments: 工具参数，必须包含 pattern，可选 path/include/max_results。
            sandbox: 可选沙箱校验器。
            approval: 可选审批器（搜索操作默认无需审批）。

        Returns:
            匹配结果列表字符串；出错时返回 "Error: ..."。
        """
        pattern = arguments.get("pattern", "")
        path = arguments.get("path", ".")
        include = arguments.get("include", _DEFAULT_INCLUDE)
        max_results = arguments.get("max_results", 50)

        if not pattern:
            return "Error: pattern parameter is required"

        # sandbox 路径校验
        if sandbox is not None:
            ok, msg = sandbox.validate_path(path, "read")
            if not ok:
                logger.warning("路径校验失败", path=path, reason=msg)
                return f"Error: path validation failed: {msg}"

        # 解析绝对路径
        if os.path.isabs(path):
            abs_path = os.path.realpath(path)
        else:
            abs_path = os.path.realpath(os.path.join(self.workspace_root, path))

        # 编译正则
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Error: invalid regex pattern: {e}"

        # 解析 include 模式
        include_patterns = [p.strip() for p in include.split(",") if p.strip()]

        try:
            matches = await asyncio.to_thread(
                self._search, abs_path, path, regex, include_patterns, max_results
            )
        except ToolError as e:
            return f"Error searching code: {e}"
        except OSError as e:
            logger.error("搜索代码 I/O 失败", path=path, error=str(e))
            return f"Error searching code: {e}"

        if not matches:
            return f"Found 0 matches for '{pattern}' in {path}:"

        total = len(matches)
        # 截断到 max_results
        if total > max_results:
            displayed = matches[:max_results]
            remaining = total - max_results
            result_text = "\n".join(displayed)
            result_text += f"\n[... {remaining} more matches ...]"
        else:
            result_text = "\n".join(matches)

        logger.debug("搜索完成", pattern=pattern, path=path, total=total)
        return f"Found {total} matches for '{pattern}' in {path}:\n{result_text}"

    def _search(
        self,
        abs_path: str,
        rel_path: str,
        regex: re.Pattern[str],
        include_patterns: list[str],
        max_results: int,
    ) -> list[str]:
        """同步递归搜索文件（由 asyncio.to_thread 调用）。

        Raises:
            ToolError: 搜索失败时抛出。
        """
        matches: list[str] = []
        # 收集所有匹配后再截断，确保计数准确
        self._walk_and_search(
            abs_path, rel_path, regex, include_patterns, matches, max_results * 2
        )
        return matches

    def _walk_and_search(
        self,
        abs_dir: str,
        rel_dir: str,
        regex: re.Pattern[str],
        include_patterns: list[str],
        matches: list[str],
        collect_limit: int,
    ) -> None:
        """递归遍历目录并搜索匹配。

        单个文件读取失败时跳过，不影响整体结果。
        """
        if not os.path.isdir(abs_dir):
            return

        try:
            entries = sorted(os.listdir(abs_dir))
        except OSError:
            return

        for entry in entries:
            if len(matches) >= collect_limit:
                return

            entry_abs = os.path.join(abs_dir, entry)
            entry_rel = entry if rel_dir == "." else os.path.join(rel_dir, entry)

            if os.path.isdir(entry_abs):
                # 跳过忽略目录
                if entry in _IGNORE_DIRS:
                    continue
                self._walk_and_search(
                    entry_abs,
                    entry_rel,
                    regex,
                    include_patterns,
                    matches,
                    collect_limit,
                )
            elif os.path.isfile(entry_abs):
                # 检查 include 模式
                if include_patterns:
                    matched = any(
                        fnmatch.fnmatch(entry, pat) for pat in include_patterns
                    )
                    if not matched:
                        continue
                self._search_file(entry_abs, entry_rel, regex, matches, collect_limit)

    def _search_file(
        self,
        abs_file: str,
        rel_file: str,
        regex: re.Pattern[str],
        matches: list[str],
        collect_limit: int,
    ) -> None:
        """搜索单个文件的每一行。

        二进制文件或读取失败时跳过，不抛异常。
        """
        # 二进制检测
        try:
            with open(abs_file, "rb") as f:
                chunk = f.read(_BINARY_CHECK_SIZE)
            if b"\x00" in chunk:
                return
        except OSError:
            return

        try:
            with open(abs_file, encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f, 1):
                    if len(matches) >= collect_limit:
                        return
                    if regex.search(line):
                        # 去掉行尾换行符
                        line_content = line.rstrip("\n\r")
                        matches.append(f"{rel_file}:{line_num}:{line_content}")
        except OSError:
            return


__all__ = ["SearchCodeTool"]
